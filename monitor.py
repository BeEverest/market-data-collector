"""
采集接口运维监控：运行记录持久化、失败追踪、健康统计与重跑任务提取。

设计要点
--------
1. 粒度：每个采集接口的一次调用 = 一条记录。宏观 11 个 akshare 接口、
   ETF 的上交所份额（按交易日）、深交所份额（按区间）、天天基金净值（按基金代码）
   都有独立记录。
2. 字段：运行ID / 运行日期 / 运行类型 / 接口键 / 接口名称 / 参数 / 状态 /
   尝试次数 / 数据行数 / 失败原因 / 耗时。回答「哪天、哪个接口、失败、为什么失败」。
3. 持久化：追加写入 JSONL（output/monitor/interface_runs.jsonl），保留全部历史，
   不覆盖。重跑批次以新的 run_id 记录，形成可追溯链路。
4. 重跑：failed_tasks() 提取最近运行批次的失败接口及其参数，供 main.py 针对性重跑。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from storage import append_interface_runs, read_interface_runs

logger = logging.getLogger(__name__)

RUN_FIELDS = [
    "run_id", "run_type", "run_date", "run_time",
    "category", "interface_key", "interface_name",
    "params", "status", "attempts", "rows", "error", "duration_sec",
]


class Monitor:
    """一次采集运行的接口状态记录器。"""

    def __init__(
        self,
        base_dir: Path,
        run_type: str = "full",
        history_path: str | Path | None = None,
        db_path: str | Path | None = None,
    ):
        now = datetime.now()
        self.run_id = now.strftime("%Y%m%d_%H%M%S") + "_" + run_type
        self.run_date = now.strftime("%Y-%m-%d")
        self.run_time = now.strftime("%Y-%m-%d %H:%M:%S")
        self.run_type = run_type
        self.records: list[dict] = []
        self.history_path = (
            Path(history_path)
            if history_path
            else Path(base_dir) / "output" / "monitor" / "interface_runs.jsonl"
        )
        self.db_path = db_path

    # ---------- 记录 ----------
    def record(
        self,
        *,
        category: str,
        interface_key: str,
        interface_name: str,
        status: str,
        params: dict | None = None,
        attempts: int = 1,
        rows: int = 0,
        error: str = "",
        duration_sec: float = 0.0,
    ) -> dict:
        """记录一次接口调用；status 取 'success' 或 'failed'。"""
        rec = {
            "run_id": self.run_id,
            "run_type": self.run_type,
            "run_date": self.run_date,
            "run_time": self.run_time,
            "category": category,
            "interface_key": interface_key,
            "interface_name": interface_name,
            "params": json.dumps(params or {}, ensure_ascii=False),
            "status": status,
            "attempts": int(attempts),
            "rows": int(rows),
            "error": "" if error is None else str(error),
            "duration_sec": round(float(duration_sec), 3),
        }
        self.records.append(rec)
        if status == "failed":
            logger.error("[监控] %s 失败：%s", interface_name, rec["error"] or "无错误信息")
        else:
            logger.debug("[监控] %s 成功（%d 行，%d 次尝试）", interface_name, rows, attempts)
        return rec

    def save(self) -> Path:
        """把本次记录写入 SQLite（优先）或旧 JSONL，返回存储路径。"""
        if self.db_path:
            append_interface_runs(self.db_path, self.records)
            logger.info("监控记录已保存：%d 条 -> SQLite %s", len(self.records), self.db_path)
            return Path(self.db_path)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a", encoding="utf-8") as handle:
            for rec in self.records:
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info("监控记录已保存：%d 条 -> %s", len(self.records), self.history_path)
        return self.history_path

    # ---------- 历史读取 ----------
    @staticmethod
    def load(history_path: str | Path, db_path: str | Path | None = None) -> pd.DataFrame:
        if db_path:
            return read_interface_runs(db_path)
        path = Path(history_path)
        if not path.exists():
            return pd.DataFrame(columns=RUN_FIELDS)
        rows: list[dict] = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("跳过损坏的监控记录：%s", line[:80])
        return pd.DataFrame(rows, columns=RUN_FIELDS)

    def failed_tasks(self, history: pd.DataFrame | None = None) -> list[dict]:
        """提取最近一个「含失败」批次的失败接口及参数，供重跑使用。

        即使之后又成功跑过一次，仍可重跑上一批次的失败接口（运维常见诉求）。
        """
        hist = self.load(self.history_path, self.db_path) if history is None else history
        if hist.empty:
            return []
        failed_runs = hist[hist["status"] == "failed"]
        if failed_runs.empty:
            return []
        last_run = failed_runs.sort_values("run_time").groupby("run_id")["run_time"].max().idxmax()
        fails = hist[(hist["run_id"] == last_run) & (hist["status"] == "failed")]
        tasks = []
        for _, r in fails.iterrows():
            try:
                params = json.loads(r["params"] or "{}")
            except json.JSONDecodeError:
                params = {}
            tasks.append({
                "interface_key": r["interface_key"],
                "interface_name": r["interface_name"],
                "category": r["category"],
                "params": params,
                "last_error": r["error"],
                "failed_date": r["run_date"],
                "failed_run": r["run_id"],
            })
        return tasks

    # ---------- 报告 ----------
    def build_report(self, history: pd.DataFrame | None = None,
                     stats_df: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
        """生成监控报告：运行总览 / 失败明细 / 接口健康度 / 调用明细 / 采集量统计。"""
        hist = self.load(self.history_path, self.db_path) if history is None else history
        if hist.empty:
            msg = pd.DataFrame([["暂无监控记录，请先运行一次采集。"]], columns=["提示"])
            empty_stat = (stats_df.rename(columns={
                "run_id": "运行ID", "run_type": "运行类型", "run_date": "运行日期",
                "category": "类别口径", "code": "细分口径", "name": "名称",
                "rows": "采集行数", "collected_at": "统计时间",
            }) if stats_df is not None and not stats_df.empty
                else pd.DataFrame([["暂无采集量统计，请先运行一次采集。"]], columns=["提示"]))
            return {"运行总览": msg, "失败明细": msg, "接口健康度": msg,
                    "调用明细": msg, "采集量统计": empty_stat}

        # 1) 运行总览
        overview = []
        for run_id, g in hist.groupby("run_id", sort=False):
            total = len(g)
            failed = int((g["status"] == "failed").sum())
            overview.append({
                "运行ID": run_id,
                "运行日期": g["run_date"].iloc[0],
                "运行类型": g["run_type"].iloc[0],
                "接口调用数": total,
                "成功数": total - failed,
                "失败数": failed,
                "成功率(%)": round((total - failed) / total * 100, 1),
                "总耗时(秒)": round(g["duration_sec"].sum(), 2),
            })
        overview_df = pd.DataFrame(overview).sort_values("运行日期", ascending=False).reset_index(drop=True)

        # 2) 失败明细，并标注是否已在后续批次重跑成功
        fails = hist[hist["status"] == "failed"].copy()
        if fails.empty:
            fail_df = pd.DataFrame([["无失败记录"]], columns=["提示"])
        else:
            def params_equal(a, b) -> bool:
                try:
                    return json.loads(a or "{}") == json.loads(b or "{}")
                except (json.JSONDecodeError, TypeError):
                    return a == b

            outcomes = []
            for _, r in fails.iterrows():
                later = hist[
                    (hist["interface_key"] == r["interface_key"])
                    & hist["params"].apply(lambda p: params_equal(p, r["params"]))
                    & (hist["status"] == "success")
                    & (hist["run_type"] == "retry")
                    & (hist["run_time"] > r["run_time"])
                ]
                if later.empty:
                    outcome = "未重跑成功"
                else:
                    first = later.sort_values("run_time").iloc[0]
                    outcome = f"已重跑成功（{first['run_date']} / {first['run_id']}）"
                outcomes.append(outcome)
            fails["重跑结果"] = outcomes
            fails = fails.sort_values("run_time", ascending=False)
            fail_df = fails[[
                "run_date", "run_id", "category", "interface_name",
                "params", "attempts", "error", "重跑结果",
            ]].rename(columns={
                "run_date": "失败日期", "run_id": "运行ID", "category": "类别",
                "interface_name": "接口名称", "params": "调用参数",
                "attempts": "尝试次数", "error": "失败原因",
            }).reset_index(drop=True)

        # 3) 接口健康度
        health = []
        for key, g in hist.groupby("interface_key"):
            total = len(g)
            failed_g = g[g["status"] == "failed"]
            last_fail = failed_g.sort_values("run_time")
            health.append({
                "接口键": key,
                "接口名称": g["interface_name"].iloc[0],
                "类别": g["category"].iloc[0],
                "累计调用": total,
                "成功数": total - len(failed_g),
                "失败数": len(failed_g),
                "成功率(%)": round((total - len(failed_g)) / total * 100, 1),
                "最近失败日期": last_fail["run_date"].iloc[-1] if not last_fail.empty else "",
                "最近失败原因": last_fail["error"].iloc[-1] if not last_fail.empty else "",
            })
        health_df = pd.DataFrame(health).sort_values("失败数", ascending=False).reset_index(drop=True)

        # 4) 调用明细（全量，便于审计）
        detail_df = hist.sort_values("run_time", ascending=False)[[
            "run_date", "run_time", "run_id", "run_type", "category",
            "interface_key", "interface_name", "params", "status",
            "attempts", "rows", "error", "duration_sec",
        ]].rename(columns={
            "run_date": "运行日期", "run_time": "运行时间", "run_id": "运行ID",
            "run_type": "运行类型", "category": "类别", "interface_key": "接口键",
            "interface_name": "接口名称", "params": "调用参数", "status": "状态",
            "attempts": "尝试次数", "rows": "数据行数", "error": "失败原因",
            "duration_sec": "耗时(秒)",
        }).reset_index(drop=True)

        if stats_df is not None and not stats_df.empty:
            stat_out = stats_df.rename(columns={
                "run_id": "运行ID", "run_type": "运行类型", "run_date": "运行日期",
                "category": "类别口径", "code": "细分口径", "name": "名称",
                "rows": "采集行数", "collected_at": "统计时间",
            })
            stat_out = stat_out[[
                "运行日期", "运行ID", "运行类型", "类别口径", "细分口径", "名称",
                "采集行数", "统计时间",
            ]].reset_index(drop=True)
        else:
            stat_out = pd.DataFrame([["暂无采集量统计，请先运行一次采集。"]], columns=["提示"])

        return {
            "运行总览": overview_df,
            "失败明细": fail_df,
            "接口健康度": health_df,
            "调用明细": detail_df,
            "采集量统计": stat_out,
        }

    def export_report(self, base_dir: Path, run_ts: str | None = None,
                      output_dir: str | Path | None = None) -> Path:
        """导出运维监控报告 Excel，返回文件路径。output_dir 可覆盖输出根目录。"""
        report = self.build_report()
        out_dir = Path(output_dir) if output_dir else Path(base_dir) / "output"
        out_dir = out_dir / "monitor"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = run_ts or datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"运维监控报告_{stamp}.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            for sheet, df in report.items():
                df.to_excel(writer, sheet_name=sheet, index=False)
                ws = writer.sheets[sheet]
                for column in ws.columns:
                    max_len = max(
                        (len(str(cell.value)) for cell in column if cell.value is not None),
                        default=8,
                    )
                    ws.column_dimensions[column[0].column_letter].width = min(max_len + 2, 55)
                ws.freeze_panes = "A2"
        logger.info("运维监控报告已生成：%s", path)
        return path


def describe_failure(
    exc: Exception | None,
    empty: bool = False,
    *,
    url: str = "",
    http_status: int | None = None,
    extra: str = "",
) -> str:
    """生成结构化失败原因：异常类型与消息 + HTTP 状态 + URL + 补充上下文。

    保持向后兼容：不传新增关键字时输出与旧版一致（"类型: 消息" /
    "接口返回空数据（无有效记录）" / "未知原因"），便于既有测试与重跑匹配。
    """
    if exc is not None:
        parts = [f"{type(exc).__name__}: {exc}"]
    elif empty:
        parts = ["接口返回空数据（无有效记录）"]
    else:
        parts = ["未知原因"]
    if http_status is not None:
        parts.append(f"HTTP {http_status}")
    if url:
        parts.append(f"URL={url}")
    if extra:
        parts.append(extra)
    return " | ".join(parts)


def describe_http_failure(
    exc: Exception,
    *,
    url: str = "",
    response_text: str = "",
    max_text: int = 300,
) -> str:
    """从 requests 异常或响应中提取 HTTP 状态与响应片段，生成失败原因。"""
    status = None
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if code is not None:
            status = int(code)
        if not url:
            url = str(getattr(resp, "url", "") or "")
        if not response_text:
            try:
                response_text = str(getattr(resp, "text", "") or "")
            except Exception:
                response_text = ""
    snippet = ""
    if response_text:
        snippet = "响应片段=" + " ".join(response_text.split())[:max_text]
    return describe_failure(exc, url=url, http_status=status, extra=snippet)
