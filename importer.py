"""Choice「份额与规模_<6位代码>.xlsx」文件导入器。

此类文件由妙想 Choice 导出，典型结构为单 Sheet：
    截止日期 | 流通份额(份) | 流通规模(元) | 公告日期
    ...
    数据来源：妙想Choice            <- 脚注行，需跳过

基金代码来自文件名（如「份额与规模_159629.xlsx」-> 159629），而非表内列。
系统主存储口径为 份额(亿份) / 单位净值(元) / 规模(亿元)，故导入时：
    份额(亿份) = 流通份额(份) / 1e8
    规模(亿元) = 流通规模(元) / 1e8
    单位净值(元) = 流通规模(元) / 流通份额(份)   （示例无净值列，反推）
交易日期取「截止日期」；「公告日期」为披露日，不入库。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

import storage
from storage import ETF_COLUMNS

CODE_RE = re.compile(r"份额与规模[_\-\s]?(\d{6})")
FOOTNOTE_MARKERS = ("数据来源", "来源：", "来源:", "备注", "说明：")
DEFAULT_SOURCE = "妙想Choice"
MAX_FILES_PER_IMPORT = 10  # 单次导入文件数上限（命令行/Web 统一）

# 基金代码 -> 简称 的进程内缓存（一次进程只拉取一次外部映射）
_name_map_cache: dict[str, str] | None = None


def get_fund_name_map() -> dict[str, str]:
    """代码 -> 基金简称，来源 akshare 东财 ETF 列表；失败返回空字典（不影响导入）。"""
    global _name_map_cache
    if _name_map_cache is not None:
        return _name_map_cache
    mapping: dict[str, str] = {}
    try:
        import akshare as ak
        df = ak.fund_etf_fund_daily_em()
        if {"基金代码", "基金简称"}.issubset(df.columns):
            mapping = dict(zip(
                df["基金代码"].astype(str).str.zfill(6),
                df["基金简称"].astype(str),
            ))
    except Exception:
        # 外部接口不可用时静默降级：名称留空，不影响数据导入
        pass
    _name_map_cache = mapping
    return mapping


def _resolve_fund_name(code: str, db_path: str | Path) -> str:
    """按代码补全基金简称：优先 SQLite 已有名称，其次 akshare 映射。"""
    code = str(code).zfill(6)
    try:
        _, existing = storage.read_etf(db_path, code)
        if not existing.empty and "基金简称" in existing.columns:
            for value in existing["基金简称"].dropna():
                text = str(value).strip()
                if text:
                    return text
    except Exception:
        pass
    return get_fund_name_map().get(code, "")


def check_file_limit(items) -> None:
    """校验单次导入文件数不超过上限，超限抛 ValueError。"""
    count = len(list(items))
    if count > MAX_FILES_PER_IMPORT:
        raise ValueError(
            f"单次最多导入 {MAX_FILES_PER_IMPORT} 个文件，实际收到 {count} 个，请分批导入"
        )


def code_from_filename(path: str | Path) -> str:
    """从「份额与规模_159629.xlsx」提取 159629；不符合命名规则时抛错。"""
    stem = Path(path).stem
    match = CODE_RE.search(stem)
    if not match:
        raise ValueError(f"文件名需符合「份额与规模_<6位ETF代码>，实际为：{stem}")
    return match.group(1)


def infer_market(code: str) -> str:
    """按 ETF 代码前缀推断交易市场（15/16/18 深市，5 开头沪市）。"""
    code = str(code).zfill(6)
    if code[:2] in {"15", "16", "18"}:
        return "SZ"
    if code[:2] in {"50", "51", "52", "56", "58"}:
        return "SH"
    return ""


def _pick_column(columns, candidates: tuple[str, ...]):
    """按候选关键词顺序做包含匹配，返回原始列名。"""
    for candidate in candidates:
        for col in columns:
            if candidate in str(col).strip():
                return col
    return None


def _to_float(value) -> float | None:
    """把带千分位/空值的单元格转为 float，失败返回 None。"""
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text or text in {"-", "--", "—"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_choice_file(
    path: str | Path,
    *,
    code: str | None = None,
    source: str = DEFAULT_SOURCE,
    name: str = "",
    collected_at: str | None = None,
) -> pd.DataFrame:
    """解析单个 Choice 份额规模文件，返回系统口径的 ETF DataFrame。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到导入文件：{path}")
    code = code or code_from_filename(path)
    frame = pd.read_excel(path, dtype=str)
    if frame.empty:
        raise ValueError(f"文件无数据行：{path.name}")

    col_date = _pick_column(frame.columns, ("截止日期", "交易日期", "日期"))
    col_shares = _pick_column(frame.columns, ("流通份额", "份额"))
    col_cap = _pick_column(frame.columns, ("流通规模", "规模"))
    missing = [label for label, col in (
        ("截止日期", col_date), ("流通份额", col_shares), ("流通规模", col_cap)) if col is None]
    if missing:
        raise ValueError(f"{path.name} 缺少必需列：{'、'.join(missing)}；实际表头：{list(frame.columns)}")

    stamp = collected_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    market = infer_market(code)
    records: list[dict] = []
    for _, row in frame.iterrows():
        raw_date = row[col_date]
        if pd.isna(raw_date):
            continue
        text = str(raw_date).strip()
        if any(marker in text for marker in FOOTNOTE_MARKERS):
            continue
        parsed = pd.to_datetime(text, errors="coerce")
        if pd.isna(parsed):
            continue
        trade_date = parsed.strftime("%Y-%m-%d")

        shares = _to_float(row[col_shares])       # 份
        cap_yuan = _to_float(row[col_cap])        # 元
        shares_yi = round(shares / 1e8, 8) if shares is not None else None
        cap_yi = round(cap_yuan / 1e8, 8) if cap_yuan is not None else None
        nav = round(cap_yuan / shares, 4) if shares and shares > 0 and cap_yuan is not None else None

        records.append({
            "基金代码": code,
            "基金简称": name,
            "交易市场": market,
            "交易日期": trade_date,
            "份额(亿份)": shares_yi,
            "单位净值(元)": nav,
            "规模(亿元)": cap_yi,
            "数据源": source,
            "采集时间": stamp,
        })

    if not records:
        raise ValueError(f"{path.name} 未解析到有效数据行")
    return pd.DataFrame(records, columns=ETF_COLUMNS)


def import_choice_file(
    path: str | Path,
    db_path: str | Path,
    *,
    run_id: str = "",
    source: str = DEFAULT_SOURCE,
    name: str = "",
    display_name: str = "",
    monitor=None,
) -> dict:
    """解析并写入 SQLite；返回本次导入的统计信息。

    display_name 仅用于日志/回执展示（如 Web 上传时的原始文件名）。
    monitor 传入时，每个文件的导入结果都会写入接口监控（统计文件数与行数）。
    """
    shown = display_name or Path(path).name
    started = time.monotonic()
    try:
        df = parse_choice_file(path, source=source, name=name)
        code = df["基金代码"].iloc[0]
        db_path = Path(db_path)
        storage.init_schema(db_path)
        # 补充基金简称：显式 name > SQLite 已有名称 > akshare 映射
        if not str(df["基金简称"].iloc[0] or "").strip():
            filled = _resolve_fund_name(code, db_path)
            if filled:
                df["基金简称"] = filled
        written = storage.upsert_etf(db_path, df, run_id)
    except Exception as exc:
        if monitor is not None:
            monitor.record(
                category="ETF", interface_key="etf.import",
                interface_name=f"Choice份额规模导入({shown})",
                status="failed", attempts=1, rows=0, error=str(exc),
                params={"file": shown, "source": source},
                duration_sec=time.monotonic() - started,
            )
        raise
    summary = {
        "file": shown,
        "code": code,
        "rows": written,
        "start": df["交易日期"].min(),
        "end": df["交易日期"].max(),
        "source": source,
    }
    if monitor is not None:
        monitor.record(
            category="ETF", interface_key="etf.import",
            interface_name=f"Choice份额规模导入({shown})",
            status="success", attempts=1, rows=written,
            params={"file": shown, "code": code, "source": source,
                    "start": summary["start"], "end": summary["end"]},
            duration_sec=time.monotonic() - started,
        )
    # 逐文件日志由调用方（命令行 / Web）记录，统一带原始文件名，避免重复输出
    return summary


def import_choice_paths(
    paths,
    db_path: str | Path,
    *,
    run_id: str = "",
    source: str = DEFAULT_SOURCE,
    monitor=None,
) -> list[dict]:
    """导入一个或多个路径（文件或目录）；目录内匹配「份额与规模_*.xlsx」。"""
    files: list[Path] = []
    for raw in paths:
        item = Path(raw)
        if item.is_dir():
            files.extend(sorted(item.glob("份额与规模_*.xlsx")))
        elif item.is_file():
            files.append(item)
        else:
            raise FileNotFoundError(f"找不到文件或目录：{item}")
    if not files:
        raise ValueError("未找到可导入的「份额与规模_*.xlsx」文件")
    check_file_limit(files)
    results: list[dict] = []
    for f in files:
        try:
            results.append(import_choice_file(f, db_path, run_id=run_id, source=source,
                                              monitor=monitor))
        except Exception as exc:
            # 失败信息带上文件名，便于批量导入时定位到具体文件
            raise ValueError(f"文件 [{f.name}] 导入失败：{exc}") from exc
    return results
