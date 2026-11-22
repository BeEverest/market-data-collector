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

import storage

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from collectors import collect_etf, collect_index, collect_macro
from collectors.etf import fetch_nav, get_trade_dates
from collectors.index import INDEX_CATALOG
from collectors.macro import CODE_TO_IFACE
from collectors.rate_limit import configure as configure_rate_limit
from exporter import export_results, prune_snapshots
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
        config = json.load(handle)
    if config.get("storage", {}).get("backend", "sqlite") == "sqlite":
        db_path = storage.db_path(config, BASE_DIR)
        storage.init_schema(db_path)
        # SQLite 为 ETF/指数配置权威源；config.json 保留为镜像与回滚基线
        config["etf"] = storage.get_etf_config(db_path, config.get("etf", {}))
        config["index"] = storage.get_index_config(db_path, config.get("index", {}))
    return config


def _db_path(config: dict) -> Path | None:
    if config.get("storage", {}).get("backend", "sqlite") != "sqlite":
        return None
    return storage.db_path(config, BASE_DIR)


def _latest_csv(kind: str, config: dict) -> Path | None:
    """定位上一次导出的宏观 / ETF CSV，作为重跑合并的基准。"""
    output_cfg = config.get("output", {})
    out_dir = BASE_DIR / output_cfg.get("dir", "output")
    prefix = output_cfg.get("filename_prefix", "market_data")
    label = "宏观数据" if kind == "macro" else "ETF规模"
    candidates = sorted(out_dir.glob(f"{prefix}_{label}_*.csv"))
    return candidates[-1] if candidates else None


def _read_base(kind: str, config: dict) -> pd.DataFrame:
    """读取当前主数据：SQLite 优先，旧模式回退到最新 CSV。"""
    db_path = _db_path(config)
    if db_path:
        if kind == "macro":
            return storage.read_macro(db_path)[1]
        if kind == "etf":
            return storage.read_etf(db_path)[1]
        if kind == "index":
            return storage.read_index(db_path)[1]
    path = _latest_csv(kind, config)
    if not path:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype={"基金代码": str})
    except OSError as exc:
        logger.warning("读取基准 CSV 失败 %s: %s", path, exc)
        return pd.DataFrame()


def _save_listing_dates(config: dict, config_path: str = "config.json") -> None:
    """仅把 ETF 上市日期回写配置文件，保留文件当前其它字段。"""
    cfg_path = Path(config_path)
    if not cfg_path.is_absolute():
        cfg_path = BASE_DIR / cfg_path
    try:
        with open(cfg_path, "r", encoding="utf-8") as handle:
            disk = json.load(handle)
    except (OSError, json.JSONDecodeError):
        disk = config
    disk.setdefault("etf", {})["listing_dates"] = config.get("etf", {}).get("listing_dates", {})
    db_path = _db_path(config)
    if db_path:
        etf_cfg = config.get("etf", {}) or {}
        storage.save_etf_config(db_path, {
            "codes": etf_cfg.get("codes", []),
            "listing_dates": etf_cfg.get("listing_dates", {}),
            "start_date": etf_cfg.get("start_date", ""),
            "end_date": etf_cfg.get("end_date", ""),
            "default_trade_days": etf_cfg.get("default_trade_days", 20),
        })
    try:
        cfg_path.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("上市日期回写配置失败：%s", exc)


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


def _apply_rate_limit(fetch_cfg: dict) -> None:
    """按配置启用请求节流（最小间隔 + 抖动），默认关闭以兼容测试。"""
    configure_rate_limit(
        min_interval=fetch_cfg.get("min_interval_sec", 0.0),
        jitter=fetch_cfg.get("jitter_sec", 0.0),
    )


def _collect(config: dict, monitor: Monitor, *, macro_only=False, etf_only=False,
             index_only=False, start_date=None, end_date=None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """执行一次完整采集，返回 (宏观_df, ETF_df, 指数_df)。

    start_date / end_date 为全量采集的日期范围覆盖值；留空时回退到各类别的
    配置默认值。这样一次采集即可让宏观、ETF、指数三类统一按指定范围抓取。
    """
    macro_df = pd.DataFrame()
    etf_df = pd.DataFrame()
    index_df = pd.DataFrame()
    fetch_cfg = config.get("fetch", {})
    _apply_rate_limit(fetch_cfg)
    retries = fetch_cfg.get("retry", 3)
    retry_interval = fetch_cfg.get("retry_interval_sec", 2)
    timeout = fetch_cfg.get("timeout_sec", 15)

    if not etf_only and not index_only:
        logger.info("开始采集宏观数据（25 项指标：17 项原指标 + 中国GDP + 7 项美国指标）...")
        macro_df = collect_macro(
            start_date=start_date or config.get("macro", {}).get("start_date"),
            end_date=end_date or config.get("macro", {}).get("end_date"),
            monitor=monitor,
            retries=retries,
            retry_interval=retry_interval,
        )
        logger.info("宏观数据采集完成：%d 行", len(macro_df))

    if not macro_only and not index_only:
        etf_cfg = config.get("etf", {})
        codes = etf_cfg.get("codes", [])
        if not codes:
            logger.warning("配置中未填写 ETF 代码（etf.codes），将不采集 ETF 数据")
        else:
            logger.info("开始采集 %d 只 ETF 的日频份额与规模...", len(codes))
            listing_dates = etf_cfg.get("listing_dates", {}) or {}
            etf_df = collect_etf(
                codes=codes,
                start_date=start_date or etf_cfg.get("start_date") or None,
                end_date=end_date or etf_cfg.get("end_date") or None,
                default_trade_days=etf_cfg.get("default_trade_days", 20),
                retries=retries,
                retry_interval=retry_interval,
                timeout=timeout,
                monitor=monitor,
                listing_dates=listing_dates,
            )
            # 回写新解析的上市日期到配置，避免每次重新请求档案页
            etf_cfg["listing_dates"] = listing_dates
            _save_listing_dates(config)
            logger.info("ETF 日频数据采集完成：%d 行", len(etf_df))

    if not macro_only and not etf_only:
        index_cfg = config.get("index", {})
        index_codes = index_cfg.get("codes", [])
        if not index_codes:
            logger.warning("配置中未填写指数代码（index.codes），将不采集指数数据")
        else:
            logger.info("开始采集 %d 个主要指数的日频行情（近一年）...", len(index_codes))
            index_df = collect_index(
                codes=index_codes,
                start_date=start_date or index_cfg.get("start_date") or None,
                end_date=end_date or index_cfg.get("end_date") or None,
                lookback_days=index_cfg.get("lookback_days", 365),
                retries=retries,
                retry_interval=retry_interval,
                monitor=monitor,
            )
            logger.info("指数日频数据采集完成：%d 行", len(index_df))
    return macro_df, etf_df, index_df


def _collect_stat_records(monitor: Monitor, macro_df: pd.DataFrame,
                          etf_df: pd.DataFrame, index_df: pd.DataFrame) -> list[dict]:
    """按类别 + 细分口径汇总本次采集的入库行数，供监控看板统计。"""
    specs = [
        ("宏观", macro_df, "指标代码", "指标名称"),
        ("ETF", etf_df, "基金代码", "基金简称"),
        ("指数", index_df, "指数代码", "指数名称"),
    ]
    records: list[dict] = []
    for category, df, code_col, name_col in specs:
        if df is None or df.empty or code_col not in df.columns:
            continue
        grouped = df.groupby(code_col, dropna=False)
        for code, group in grouped:
            code = str(code)
            names = group[name_col].dropna().unique().tolist() if name_col in group.columns else []
            records.append({
                "run_id": monitor.run_id,
                "run_type": monitor.run_type,
                "run_date": monitor.run_date,
                "category": category,
                "code": code,
                "name": names[0] if names else "",
                "rows": int(len(group)),
                "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
    return records


def _finish(monitor: Monitor, macro_df: pd.DataFrame, etf_df: pd.DataFrame,
            config: dict, run_ts: str, index_df: pd.DataFrame | None = None) -> list[str]:
    """写入 SQLite 主存储；按配置决定是否自动生成文件快照。"""
    if index_df is None:
        index_df = pd.DataFrame()
    db_path = _db_path(config)
    if db_path:
        monitor.db_path = db_path
        monitor.save()
        storage.upsert_macro(db_path, macro_df, run_ts)
        storage.upsert_etf(db_path, etf_df, run_ts)
        storage.upsert_index(db_path, index_df, run_ts)
        stat_records = _collect_stat_records(monitor, macro_df, etf_df, index_df)
        written = storage.append_collection_stats(db_path, stat_records)
        logger.info("采集量统计已写入：%d 条（类别/细分口径）", written)
        macro_rows, _ = storage.read_macro(db_path)
        etf_rows, _ = storage.read_etf(db_path)
        index_rows, _ = storage.read_index(db_path)
        failed = sum(1 for r in monitor.records if r["status"] == "failed")
        logger.info("SQLite 主存储已更新：宏观 %d 行 / ETF %d 行 / 指数 %d 行；接口调用 %d 次，失败 %d 次",
                    macro_rows, etf_rows, index_rows, len(monitor.records), failed)
        files: list[str] = []
        if config.get("storage", {}).get("auto_export_on_collect", False):
            files = export_results(macro_df, etf_df, BASE_DIR, config, run_ts,
                                   monitor_records=monitor.records)
            out_root = BASE_DIR / config.get("output", {}).get("dir", "output")
            files.append(str(monitor.export_report(BASE_DIR, run_ts, output_dir=out_root)))
            prune_snapshots(BASE_DIR, config)
        for file_path in files:
            logger.info("  - %s", file_path)
        return files

    # 旧文件模式（仅回滚用）
    monitor.save()
    files = export_results(macro_df, etf_df, BASE_DIR, config, run_ts,
                           monitor_records=monitor.records)
    out_root = BASE_DIR / config.get("output", {}).get("dir", "output")
    report = monitor.export_report(BASE_DIR, run_ts, output_dir=out_root)
    files.append(str(report))
    prune_snapshots(BASE_DIR, config)
    failed = sum(1 for r in monitor.records if r["status"] == "failed")
    logger.info("接口监控：%d 次调用，%d 次失败，报告=%s", len(monitor.records), failed, report)
    return files


def _retry_failed(config: dict, monitor_cfg: dict) -> int:
    """重跑最近批次的失败接口，合并到最新数据并导出。"""
    history_path = BASE_DIR / monitor_cfg.get("history_file", "output/monitor/interface_runs.jsonl")
    db_path = _db_path(config)
    history = Monitor.load(history_path, db_path)
    probe = Monitor(BASE_DIR, run_type="retry", history_path=history_path, db_path=db_path)
    tasks = probe.failed_tasks(history)
    if not tasks:
        logger.info("最近批次无失败接口，无需重跑。")
        logger.info("可运行 python main.py --monitor-report 查看历史监控报告。")
        return 0

    logger.info("检测到 %d 个失败接口，开始重跑：", len(tasks))
    for task in tasks:
        logger.info("  - %s（%s，原因：%s）", task["interface_name"],
                    task["failed_date"], task["last_error"] or "未知")

    retry_monitor = Monitor(BASE_DIR, run_type="retry", history_path=history_path, db_path=db_path)
    fetch_cfg = config.get("fetch", {})
    _apply_rate_limit(fetch_cfg)
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
        base_keys = _read_base("etf", config)
        if not nav_patch.empty and not base_keys.empty:
            base_keys = base_keys[["基金代码", "交易日期"]].copy()
            nav_patch["基金代码"] = nav_patch["基金代码"].astype(str)
            base_keys["基金代码"] = base_keys["基金代码"].astype(str).str.zfill(6)
            nav_patch = nav_patch.merge(base_keys.drop_duplicates(),
                                         on=["基金代码", "交易日期"], how="inner")
        etf_patch = nav_patch[["基金代码", "交易日期", "单位净值(元)"]]

    # ---- 指数失败接口 ----
    symbol_to_code = {meta["symbol"]: code for code, meta in INDEX_CATALOG.items()}
    index_codes: set[str] = set()
    for task in tasks:
        key = task["interface_key"]
        if key.startswith("index."):
            symbol = key.split(".", 1)[1]
            if symbol in symbol_to_code:
                index_codes.add(symbol_to_code[symbol])
            elif symbol.isdigit():
                index_codes.add(str(symbol).zfill(6))
    index_patch = pd.DataFrame()
    if index_codes:
        logger.info("重跑指数接口：%s", sorted(index_codes))
        index_cfg = config.get("index", {})
        index_patch = collect_index(
            codes=sorted(index_codes),
            start_date=index_cfg.get("start_date") or None,
            end_date=index_cfg.get("end_date") or None,
            lookback_days=index_cfg.get("lookback_days", 365),
            retries=retries, retry_interval=retry_interval, monitor=retry_monitor,
        )

    # ---- 合并到最新数据 ----
    macro_base = _read_base("macro", config)
    etf_base = _read_base("etf", config)
    index_base = _read_base("index", config)
    macro_final = _merge_patch(macro_base, macro_patch, ["指标代码", "日期"])
    etf_final = _merge_patch(etf_base, etf_patch, ["基金代码", "交易日期"])
    if not etf_final.empty and {"份额(亿份)", "单位净值(元)"}.issubset(etf_final.columns):
        # 净值/份额补丁合并后，统一重算规模，保证三列一致
        etf_final["规模(亿元)"] = etf_final["份额(亿份)"] * etf_final["单位净值(元)"]
    if not index_patch.empty:
        index_patch["指数代码"] = index_patch["指数代码"].astype(str).str.zfill(6)
    if not index_base.empty:
        index_base["指数代码"] = index_base["指数代码"].astype(str).str.zfill(6)
    index_final = _merge_patch(index_base, index_patch, ["指数代码", "交易日期"])
    logger.info("重跑合并完成：宏观 %d 行，ETF %d 行，指数 %d 行",
                len(macro_final), len(etf_final), len(index_final))

    _finish(retry_monitor, macro_final, etf_final, config, retry_monitor.run_id, index_final)
    succeeded = sum(1 for r in retry_monitor.records if r["status"] == "success")
    failed = sum(1 for r in retry_monitor.records if r["status"] == "failed")
    logger.info("重跑结果：%d 次成功，%d 次仍失败", succeeded, failed)
    return 0 if failed == 0 else 1


def collect_interface(
    config: dict,
    category: str,
    codes: list[str],
    start: str | None = None,
    end: str | None = None,
) -> str:
    """精确采集：按类别 + 指标/基金代码 + 时间范围采集，合并进最新数据。

    category : 'macro'（宏观指标代码）/ 'etf'（基金代码）/ 'index'（指数代码）
    codes    : 指标代码 / 基金代码 / 指数代码列表；为空时该类别全部目标
    start/end: 宏观为 YYYY-MM，ETF/指数为 YYYY-MM-DD；留空表示按配置默认区间
    返回执行摘要文本，供 Web 任务展示。
    """
    monitor_cfg = config.get("monitor", {})
    history_path = BASE_DIR / monitor_cfg.get("history_file", "output/monitor/interface_runs.jsonl")
    db_path = _db_path(config)
    monitor = Monitor(BASE_DIR, run_type="interface", history_path=history_path, db_path=db_path)
    fetch_cfg = config.get("fetch", {})
    retries = fetch_cfg.get("retry", 3)
    retry_interval = fetch_cfg.get("retry_interval_sec", 2)
    timeout = fetch_cfg.get("timeout_sec", 15)

    scope = f"类别={category}, 代码={','.join(codes) if codes else '全部'}, 区间={start or '默认'}~{end or '默认'}"
    logger.info("精确采集开始：%s", scope)

    # 读取最新主数据作为合并基底，避免单项采集覆盖其它数据
    macro_base = _read_base("macro", config)
    etf_base = _read_base("etf", config)
    index_base = _read_base("index", config)

    added_macro = added_etf = added_index = 0
    if category == "macro":
        code_set = set(codes) if codes else None
        # 由指标代码推导需要调用的底层接口，避免无谓调用
        only = None
        if code_set:
            only = {CODE_TO_IFACE[c] for c in code_set if c in CODE_TO_IFACE} or None
        s_date = start or config.get("macro", {}).get("start_date") or None
        patch = collect_macro(
            start_date=s_date, end_date=end or None, monitor=monitor,
            codes=code_set, only=only, retries=retries, retry_interval=retry_interval,
        )
        macro_final = _merge_patch(macro_base, patch, ["指标代码", "日期"])
        added_macro = len(patch)
        etf_final = etf_base
        index_final = index_base
        logger.info("精确采集宏观：补丁 %d 行，合并后 %d 行", added_macro, len(macro_final))
    elif category == "etf":
        etf_cfg = config.get("etf", {})
        target_codes = [str(c).strip().zfill(6) for c in codes] if codes else list(etf_cfg.get("codes", []))
        s_date = start or etf_cfg.get("start_date") or None
        e_date = end or etf_cfg.get("end_date") or None
        listing_dates = etf_cfg.get("listing_dates", {}) or {}
        patch = collect_etf(
            codes=target_codes, start_date=s_date, end_date=e_date,
            default_trade_days=etf_cfg.get("default_trade_days", 20),
            retries=retries, retry_interval=retry_interval, timeout=timeout,
            monitor=monitor, listing_dates=listing_dates,
        )
        etf_cfg["listing_dates"] = listing_dates
        _save_listing_dates(config)
        # 统一基金代码类型，确保合并键一致
        if not patch.empty:
            patch["基金代码"] = patch["基金代码"].astype(str).str.zfill(6)
        if not etf_base.empty:
            etf_base["基金代码"] = etf_base["基金代码"].astype(str).str.zfill(6)
        etf_final = _merge_patch(etf_base, patch, ["基金代码", "交易日期"])
        if not etf_final.empty and {"份额(亿份)", "单位净值(元)"}.issubset(etf_final.columns):
            etf_final["规模(亿元)"] = etf_final["份额(亿份)"] * etf_final["单位净值(元)"]
        added_etf = len(patch)
        macro_final = macro_base
        index_final = index_base
        logger.info("精确采集 ETF：补丁 %d 行，合并后 %d 行", added_etf, len(etf_final))
    elif category == "index":
        index_cfg = config.get("index", {})
        target_codes = [str(c).strip().zfill(6) for c in codes] if codes else list(index_cfg.get("codes", []))
        s_date = start or index_cfg.get("start_date") or None
        e_date = end or index_cfg.get("end_date") or None
        patch = collect_index(
            codes=target_codes, start_date=s_date, end_date=e_date,
            lookback_days=index_cfg.get("lookback_days", 365),
            retries=retries, retry_interval=retry_interval, monitor=monitor,
        )
        if not patch.empty:
            patch["指数代码"] = patch["指数代码"].astype(str).str.zfill(6)
        if not index_base.empty:
            index_base["指数代码"] = index_base["指数代码"].astype(str).str.zfill(6)
        index_final = _merge_patch(index_base, patch, ["指数代码", "交易日期"])
        added_index = len(patch)
        macro_final = macro_base
        etf_final = etf_base
        logger.info("精确采集指数：补丁 %d 行，合并后 %d 行", added_index, len(index_final))
    else:
        raise ValueError(f"未知采集类别：{category}")

    _finish(monitor, macro_final, etf_final, config, monitor.run_id, index_final)
    failed = sum(1 for r in monitor.records if r["status"] == "failed")
    summary = (f"精确采集完成（{scope}）：新增/更新宏观 {added_macro} 行、ETF {added_etf} 行、"
               f"指数 {added_index} 行，接口失败 {failed} 次")
    logger.info(summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集大盘宏观与 ETF 数据，导出 CSV/Excel，附接口运维监控")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--macro-only", action="store_true", help="仅采集宏观数据")
    parser.add_argument("--etf-only", action="store_true", help="仅采集 ETF 数据")
    parser.add_argument("--index-only", action="store_true", help="仅采集指数数据（近一年）")
    parser.add_argument(
        "--output-format",
        choices=["csv", "excel", "both"],
        default=None,
        help="覆盖配置中的导出格式",
    )
    parser.add_argument("--monitor-report", action="store_true", help="仅生成运维监控报告，不采集数据")
    parser.add_argument("--retry-failed", action="store_true", help="重跑最近批次的失败接口并合并结果")
    parser.add_argument("--collect-interface", action="store_true", help="按接口/指标+时间范围精确采集")
    parser.add_argument("--category", choices=["macro", "etf", "index"], help="精确采集类别")
    parser.add_argument("--codes", help="指标代码/基金代码，逗号分隔；留空为该类别全部")
    parser.add_argument("--start", help="起始时间（全量/精确采集通用）：宏观 YYYY-MM / ETF、指数 YYYY-MM-DD")
    parser.add_argument("--end", help="结束时间（全量/精确采集通用）：宏观 YYYY-MM / ETF、指数 YYYY-MM-DD")
    parser.add_argument(
        "--import-files", nargs="+", metavar="PATH",
        help="导入 Choice「份额与规模_<代码>.xlsx」文件/目录到 SQLite（作为 ETF 采集数据）",
    )
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
    db_path = _db_path(config)

    if args.monitor_report:
        out_root = BASE_DIR / config.get("output", {}).get("dir", "output")
        report_path = Monitor(
            BASE_DIR, run_type="report", history_path=history_path, db_path=db_path
        ).export_report(BASE_DIR, output_dir=out_root)
        logger.info("监控报告：%s", report_path)
        return 0

    if args.retry_failed:
        return _retry_failed(config, monitor_cfg)

    if args.import_files:
        from importer import import_choice_paths
        if not db_path:
            logger.error("导入需要 SQLite 存储，请在 config 中启用 storage.backend=sqlite")
            return 2
        run_ts = datetime.now().strftime("%Y%m%d_%H%M%S_import")
        # 导入计入接口监控（成功/失败文件、行数、耗时、运行时刻均记录）
        import_monitor = Monitor(BASE_DIR, run_type="import",
                                 history_path=history_path, db_path=db_path)
        import_monitor.run_id = run_ts
        try:
            results = import_choice_paths(args.import_files, db_path, run_id=run_ts,
                                          monitor=import_monitor)
        except ValueError as exc:
            import_monitor.save()
            logger.error("导入失败：%s", exc)
            return 2
        import_monitor.save()
        logger.info("导入完成：%d 个文件，共 %d 行", len(results), sum(r["rows"] for r in results))
        for item in results:
            logger.info("  - %s [%s] %d 行（%s ~ %s）",
                        item["file"], item["code"], item["rows"], item["start"], item["end"])
        return 0

    if args.collect_interface:
        if not args.category:
            logger.error("精确采集需指定 --category macro|etf|index")
            return 2
        codes = [c.strip() for c in (args.codes or "").split(",") if c.strip()]
        collect_interface(config, args.category, codes, args.start, args.end)
        return 0

    monitor = Monitor(BASE_DIR, run_type="full", history_path=history_path, db_path=db_path)
    macro_df, etf_df, index_df = _collect(
        config, monitor, macro_only=args.macro_only,
        etf_only=args.etf_only, index_only=args.index_only,
        start_date=args.start, end_date=args.end,
    )
    _finish(monitor, macro_df, etf_df, config, monitor.run_id, index_df)
    return 0


if __name__ == "__main__":
    sys.exit(main())
