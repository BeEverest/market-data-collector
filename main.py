#!/usr/bin/env python3
"""投研大盘数据采集模块 - 命令行入口（含接口运维监控与失败重跑）。

用法：
    python main.py                       # 采集全部数据，导出 CSV + Excel + 监控报告
    python main.py --macro-only          # 仅采集宏观数据
    python main.py --etf-only            # 仅采集 ETF 数据
    python main.py --config my.json      # 指定配置文件
    python main.py --output-format csv   # 覆盖导出格式（csv / excel / both）
    python main.py --monitor-report      # 仅生成运维监控报告（不采集）
    python main.py --retry-failed        # 重跑最近批次的失败接口，并合并结果
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from collectors import collect_etf, collect_macro
from collectors.etf import fetch_nav, get_trade_dates
from exporter import export_results
from monitor import Monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("market_data")


def load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = BASE_DIR / config_path
    with open(config_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _latest_csv(kind: str, config: dict) -> Path | None:
    """定位上一次导出的宏观 / ETF CSV，作为重跑合并的基准。"""
    output_cfg = config.get("output", {})
    out_dir = BASE_DIR / output_cfg.get("dir", "output")
    prefix = output_cfg.get("filename_prefix", "market_data")
    label = "宏观数据" if kind == "macro" else "ETF规模"
    candidates = sorted(out_dir.glob(f"{prefix}_{label}_*.csv"))
    return candidates[-1] if candidates else None


def _merge_patch(main_df: pd.DataFrame, patch_df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """用重跑补丁的非空字段覆盖主数据同键记录；主数据中没有的键追加。"""
    if patch_df is None or patch_df.empty:
        return main_df
    if main_df is None or main_df.empty:
        return patch_df
    out = main_df.copy()
    for _, prow in patch_df.iterrows():
        mask = True
        for key in keys:
            # CSV 读回的数值键可能与补丁的字符串键类型不同，统一按字符串比较
            mask &= out[key].astype(str) == str(prow[key])
        matched = out[mask].index.tolist()
        if not matched:
            out = pd.concat([out, pd.DataFrame([prow])], ignore_index=True)
            continue
        for col in out.columns:
            if col in keys:
                continue
            val = prow.get(col)
            if val is None:
                continue
            if isinstance(val, float) and pd.isna(val):
                continue
            if isinstance(val, str) and val == "":
                continue
            out.loc[matched[0], col] = val
    return out.reset_index(drop=True)


def _collect(config: dict, monitor: Monitor, *, macro_only=False, etf_only=False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """执行一次完整采集，返回 (宏观_df, ETF_df)。"""
    macro_df = pd.DataFrame()
    etf_df = pd.DataFrame()
    fetch_cfg = config.get("fetch", {})
    retries = fetch_cfg.get("retry", 3)
    retry_interval = fetch_cfg.get("retry_interval_sec", 2)
    timeout = fetch_cfg.get("timeout_sec", 15)

    if not etf_only:
        logger.info("开始采集宏观数据（17 个月度指标）...")
        macro_df = collect_macro(
            start_date=config.get("macro", {}).get("start_date"),
            monitor=monitor,
            retries=retries,
            retry_interval=retry_interval,
        )
        logger.info("宏观数据采集完成：%d 行", len(macro_df))

    if not macro_only:
        etf_cfg = config.get("etf", {})
        codes = etf_cfg.get("codes", [])
        if not codes:
            logger.warning("配置中未填写 ETF 代码（etf.codes），将不采集 ETF 数据")
        else:
            logger.info("开始采集 %d 只 ETF 的日频份额与规模...", len(codes))
            etf_df = collect_etf(
                codes=codes,
                start_date=etf_cfg.get("start_date") or None,
                end_date=etf_cfg.get("end_date") or None,
                default_trade_days=etf_cfg.get("default_trade_days", 20),
                retries=retries,
                retry_interval=retry_interval,
                timeout=timeout,
                monitor=monitor,
            )
            logger.info("ETF 日频数据采集完成：%d 行", len(etf_df))
    return macro_df, etf_df


def _finish(monitor: Monitor, macro_df: pd.DataFrame, etf_df: pd.DataFrame,
            config: dict, run_ts: str) -> list[str]:
    """导出数据、保存监控记录、生成监控报告。"""
    monitor.save()
    files = export_results(macro_df, etf_df, BASE_DIR, config, run_ts,
                           monitor_records=monitor.records)
    out_root = BASE_DIR / config.get("output", {}).get("dir", "output")
    report = monitor.export_report(BASE_DIR, run_ts, output_dir=out_root)
    files.append(str(report))
    failed = sum(1 for r in monitor.records if r["status"] == "failed")
    logger.info("接口监控：%d 次调用，%d 次失败，报告=%s", len(monitor.records), failed, report)
    logger.info("全部完成，生成文件：")
    for file_path in files:
        logger.info("  - %s", file_path)
    return files


def _retry_failed(config: dict, monitor_cfg: dict) -> int:
    """重跑最近批次的失败接口，合并到最新数据并导出。"""
    history_path = BASE_DIR / monitor_cfg.get("history_file", "output/monitor/interface_runs.jsonl")
    history = Monitor.load(history_path)
    probe = Monitor(BASE_DIR, run_type="retry", history_path=history_path)
    tasks = probe.failed_tasks(history)
    if not tasks:
        logger.info("最近批次无失败接口，无需重跑。")
        logger.info("可运行 python main.py --monitor-report 查看历史监控报告。")
        return 0

    logger.info("检测到 %d 个失败接口，开始重跑：", len(tasks))
    for task in tasks:
        logger.info("  - %s（%s，原因：%s）", task["interface_name"],
                    task["failed_date"], task["last_error"] or "未知")

    retry_monitor = Monitor(BASE_DIR, run_type="retry", history_path=history_path)
    fetch_cfg = config.get("fetch", {})
    retries = fetch_cfg.get("retry", 3)
    retry_interval = fetch_cfg.get("retry_interval_sec", 2)
    timeout = fetch_cfg.get("timeout_sec", 15)

    # ---- 宏观失败接口 ----
    macro_keys = {
        t["interface_key"].split(".", 1)[1]
        for t in tasks if t["interface_key"].startswith("macro.")
    }
    macro_patch = pd.DataFrame()
    if macro_keys:
        logger.info("重跑宏观接口：%s", ", ".join(sorted(macro_keys)))
        macro_patch = collect_macro(
            start_date=config.get("macro", {}).get("start_date"),
            monitor=retry_monitor, only=macro_keys,
            retries=retries, retry_interval=retry_interval,
        )

    # ---- ETF 失败接口 ----
    sse_dates: set[str] = set()
    szse_range: tuple[str, str] | None = None
    nav_codes: set[str] = set()
    full_etf_retry = False
    for task in tasks:
        key, params = task["interface_key"], task["params"]
        if key == "etf.trade_calendar":
            full_etf_retry = True
        elif key == "etf.sse_shares":
            if params.get("trade_date"):
                sse_dates.add(str(params["trade_date"]))
        elif key == "etf.szse_shares":
            if params.get("start") and params.get("end"):
                szse_range = (str(params["start"]), str(params["end"]))
        elif key.startswith("etf.nav."):
            code = params.get("code") or key.split(".", 1)[1]
            nav_codes.add(str(code))

    etf_cfg = config.get("etf", {})
    codes = etf_cfg.get("codes", [])
    end = etf_cfg.get("end_date") or datetime.now().strftime("%Y-%m-%d")
    if etf_cfg.get("start_date"):
        start = etf_cfg["start_date"]
    else:
        history = get_trade_dates("2000-01-01", end)
        n = etf_cfg.get("default_trade_days", 20)
        start = history[-n] if len(history) >= n else (history[0] if history else end)

    etf_patch = pd.DataFrame()
    if full_etf_retry or sse_dates or szse_range:
        # 份额类失败：重采指定份额 + 全量净值，保证规模可重算
        logger.info("重跑 ETF 份额接口：sse_dates=%d, szse_range=%s", len(sse_dates), szse_range)
        etf_patch = collect_etf(
            codes=codes, start_date=start, end_date=end,
            default_trade_days=etf_cfg.get("default_trade_days", 20),
            retries=retries, retry_interval=retry_interval, timeout=timeout,
            monitor=retry_monitor,
            sse_dates=None if full_etf_retry else (sse_dates or None),
            szse_range=None if full_etf_retry else szse_range,
            nav_codes=None,
        )
    elif nav_codes:
        # 仅净值失败：只补采净值，份额沿用主数据，避免无谓重采份额引入新故障
        logger.info("重跑 ETF 净值接口：nav_codes=%s", sorted(nav_codes))
        nav_frames = []
        for code in sorted(nav_codes):
            frame = fetch_nav(code, start, end, retries, retry_interval, timeout,
                              monitor=retry_monitor)
            if not frame.empty:
                nav_frames.append(frame)
        nav_patch = pd.concat(nav_frames, ignore_index=True) if nav_frames else pd.DataFrame(
            columns=["基金代码", "交易日期", "单位净值(元)"]
        )
        # 只补采主数据中已存在的「基金×交易日」，避免引入无份额的空行
        base_path = _latest_csv("etf", config)
        if not nav_patch.empty and base_path:
            base_keys = pd.read_csv(base_path, usecols=["基金代码", "交易日期"], dtype={"基金代码": str})
            nav_patch["基金代码"] = nav_patch["基金代码"].astype(str)
            base_keys["基金代码"] = base_keys["基金代码"].astype(str).str.zfill(6)
            nav_patch = nav_patch.merge(base_keys.drop_duplicates(),
                                         on=["基金代码", "交易日期"], how="inner")
        etf_patch = nav_patch[["基金代码", "交易日期", "单位净值(元)"]]

    # ---- 合并到最新数据 ----
    macro_base = pd.DataFrame()
    etf_base = pd.DataFrame()
    base_path = _latest_csv("macro", config)
    if base_path:
        macro_base = pd.read_csv(base_path)
    base_path = _latest_csv("etf", config)
    if base_path:
        etf_base = pd.read_csv(base_path)
    macro_final = _merge_patch(macro_base, macro_patch, ["指标代码", "日期"])
    etf_final = _merge_patch(etf_base, etf_patch, ["基金代码", "交易日期"])
    if not etf_final.empty and {"份额(亿份)", "单位净值(元)"}.issubset(etf_final.columns):
        # 净值/份额补丁合并后，统一重算规模，保证三列一致
        etf_final["规模(亿元)"] = etf_final["份额(亿份)"] * etf_final["单位净值(元)"]
    logger.info("重跑合并完成：宏观 %d 行，ETF %d 行", len(macro_final), len(etf_final))

    _finish(retry_monitor, macro_final, etf_final, config, retry_monitor.run_id)
    succeeded = sum(1 for r in retry_monitor.records if r["status"] == "success")
    failed = sum(1 for r in retry_monitor.records if r["status"] == "failed")
    logger.info("重跑结果：%d 次成功，%d 次仍失败", succeeded, failed)
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集大盘宏观与 ETF 数据，导出 CSV/Excel，附接口运维监控")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--macro-only", action="store_true", help="仅采集宏观数据")
    parser.add_argument("--etf-only", action="store_true", help="仅采集 ETF 数据")
    parser.add_argument(
        "--output-format",
        choices=["csv", "excel", "both"],
        default=None,
        help="覆盖配置中的导出格式",
    )
    parser.add_argument("--monitor-report", action="store_true", help="仅生成运维监控报告，不采集数据")
    parser.add_argument("--retry-failed", action="store_true", help="重跑最近批次的失败接口并合并结果")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.output_format == "csv":
        config["output"]["formats"] = ["csv"]
    elif args.output_format == "excel":
        config["output"]["formats"] = ["excel"]
    elif args.output_format == "both":
        config["output"]["formats"] = ["csv", "excel"]

    monitor_cfg = config.get("monitor", {})
    history_path = BASE_DIR / monitor_cfg.get("history_file", "output/monitor/interface_runs.jsonl")

    if args.monitor_report:
        out_root = BASE_DIR / config.get("output", {}).get("dir", "output")
        report_path = Monitor(BASE_DIR, run_type="report", history_path=history_path).export_report(
            BASE_DIR, output_dir=out_root
        )
        logger.info("监控报告：%s", report_path)
        return 0

    if args.retry_failed:
        return _retry_failed(config, monitor_cfg)

    monitor = Monitor(BASE_DIR, run_type="full", history_path=history_path)
    macro_df, etf_df = _collect(config, monitor, macro_only=args.macro_only, etf_only=args.etf_only)
    _finish(monitor, macro_df, etf_df, config, monitor.run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
