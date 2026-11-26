#!/usr/bin/env python3
"""投研大盘数据采集系统 - Web 看板服务。

启动：
    python web_app.py                # 按 config.json 的 web.host/port 启动
    python web_app.py --port 9000    # 覆盖端口

浏览器访问 http://127.0.0.1:8000 。所有采集/重跑在后台线程执行，前端轮询任务状态。
业务数据、接口监控与 ETF 配置以 SQLite 为主存储；CSV/Excel 为按需导出快照。
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(BASE_DIR))

from main import (
    BASE_DIR as MAIN_BASE, _collect, _finish, _merge_patch,
    collect_interface, load_config, collect_trade_raw, _finish_trade,
)
import storage
from processors import trade as trade_calc
from exporter import export_etf, export_full, export_index, export_macro, export_monitor
from monitor import Monitor, format_failure_detail
from collectors.macro import catalog as macro_catalog
from collectors.etf import get_fund_name_map
from collectors.index import catalog as index_catalog
from collectors.trade_calendar import last_completed_trade_day
from processors.derived import compute_derived
from processors.dashboard import (
    build_etf_charts, build_etf_daily_detail, build_macro_charts, build_trade_charts,
)
from processors.etf_change import compute_etf_changes

ETF_TAG_OPTIONS = ["宽基", "汇金", "黄金"]

# 采集数据·日交易 Tab：原始表中文字段名（不在此表的字段原样透传；run_id 显式剔除）。
# 排序在映射前的英文 trade_date 上完成，此处仅用于预览表头展示。
TRADE_RAW_COLUMNS_CN = {
    "biz_key": "业务键",
    "trade_date": "交易日期",
    "sse_amount": "沪市成交额(万亿)",
    "sse_volume": "沪市成交量(手)",
    "sse_float_mv": "沪市流通市值(万亿)",
    "sse_total_mv": "沪市总市值(万亿)",
    "szse_amount": "深市成交额(万亿)",
    "szse_float_mv": "深市流通市值(万亿)",
    "szse_total_mv": "深市总市值(万亿)",
    "financing_balance": "融资余额(万亿)",
    "securities_balance": "融券余额(万亿)",
    "financing_buy": "融资买入额(万亿)",
    "securities_sell": "融券卖出额(万亿)",
    "margin_ratio": "平均维持担保比例(%)",
    "debt_investors": "有负债投资者数(人)",
    "collateral_value": "担保物总价值(万亿)",
    "active_investors": "参与交易投资者数(人)",
    "individual_investors": "个人投资者数(人)",
    "institutional_investors": "机构投资者数(人)",
    "broker_count": "证券公司数(家)",
    "branch_count": "营业部数(家)",
    "code": "代码",
    "name": "名称",
    "pct_change": "涨跌幅(%)",
    "volume": "成交量(手)",
    "amount": "成交额(百万)",
    "last_price": "最新价",
    "up_count": "上涨家数",
    "down_count": "下跌家数",
    "flat_count": "平盘家数",
    "close_price": "收盘价",
    "top5_amount_yi": "前5%成交额(万亿)",
    "total_amount_yi": "全A成交额(万亿)",
    "concentration5": "成交集中度前5%(%)",
    "source": "数据来源",
    "collected_at": "采集时间",
}

# 日交易预览金额字段换算除数（原值单位 → 目标单位）：
#   亿元 → 万亿：除以 1e4；元 → 万亿：除以 1e12；
#   全A个股快照成交额 amount：元 → 百万，除以 1e6（单只个股量级用百万更直观）。
# 仅金额类字段换算；成交量(手)、投资者/家数、比例(%)、价格等保持原样。
TRADE_RAW_AMOUNT_DIVISOR = {
    # 亿元
    "sse_amount": 1e4, "sse_float_mv": 1e4, "sse_total_mv": 1e4,
    "financing_balance": 1e4, "securities_balance": 1e4,
    "financing_buy": 1e4, "securities_sell": 1e4, "collateral_value": 1e4,
    "top5_amount_yi": 1e4, "total_amount_yi": 1e4,
    # 元
    "szse_amount": 1e12, "szse_float_mv": 1e12, "szse_total_mv": 1e12,
    "amount": 1e6,  # 全A个股快照成交额：元 → 百万（单只个股量级用百万更直观）
}

app = FastAPI(title="投研大盘数据采集系统", version="1.0")

# ---------------- 全局状态 ----------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()          # 采集/重跑互斥，避免并发写同一批文件
logger = logging.getLogger("market_data.web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")

# 全局实时日志环形缓冲：承接所有线程（含定时调度器/后台采集）的日志，供「实时监控」常驻轮询。
# 与 per-job 日志（JobLogHandler，任务结束即固定）互补：这里跨任务保留最近 5000 行。
LIVE_LOG_LIMIT = 5000
_live_logs: deque[dict] = deque(maxlen=LIVE_LOG_LIMIT)
_live_seq = itertools.count(1)
_live_lock = threading.Lock()


class LiveLogHandler(logging.Handler):
    """常驻 root 的 handler：把任意 logger/线程的记录写入全局缓冲。"""

    def emit(self, record: logging.LogRecord):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        entry = {
            "seq": next(_live_seq),
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": record.levelname,
            "message": msg,
        }
        with _live_lock:
            _live_logs.append(entry)


# 挂载一次到 root：所有子 logger 默认向 root 传播，覆盖 JobLogHandler 捕获不到的调度器日志。
_root_logger = logging.getLogger()
if not any(isinstance(h, LiveLogHandler) for h in _root_logger.handlers):
    _root_logger.addHandler(LiveLogHandler())

CONFIG_PATH = BASE_DIR / "config.json"


def _cfg() -> dict:
    return load_config(str(CONFIG_PATH))


def _history_path(config: dict) -> Path:
    return BASE_DIR / config.get("monitor", {}).get(
        "history_file", "output/monitor/interface_runs.jsonl"
    )


def _db_path(config: dict) -> Path | None:
    if config.get("storage", {}).get("backend", "sqlite") != "sqlite":
        return None
    return storage.db_path(config, BASE_DIR)


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _etf_config_view() -> dict:
    cfg = _cfg()
    db_path = _db_path(cfg)
    etf = storage.get_etf_config(db_path, cfg.get("etf", {})) if db_path else cfg.get("etf", {})
    codes = [str(c).strip().zfill(6) for c in etf.get("codes", []) if str(c).strip()]
    name_map = get_fund_name_map()
    listing = {str(k).zfill(6): v for k, v in (etf.get("listing_dates") or {}).items()}
    tag_map = {
        str(k).zfill(6): [t for t in (v if isinstance(v, list) else str(v).split(",")) if t]
        for k, v in (etf.get("tags") or {}).items()
    }
    items = [{
        "code": c,
        "name": name_map.get(c, ""),
        "listing_date": listing.get(c, ""),
        "tags": tag_map.get(c, []),
    } for c in codes]
    return {
        "codes": codes,
        "items": items,
        "tags": tag_map,
        "tag_options": ETF_TAG_OPTIONS,
        "start_date": etf.get("start_date", ""),
        "end_date": etf.get("end_date", ""),
        "default_trade_days": etf.get("default_trade_days", storage.DEFAULT_ETF_CONFIG["default_trade_days"]),
    }


def _etf_tag_map() -> dict[str, str]:
    cfg = _cfg()
    db = _db_path(cfg)
    etf = storage.get_etf_config(db, cfg.get("etf", {})) if db else cfg.get("etf", {})
    out = {}
    for code, tags in (etf.get("tags") or {}).items():
        if isinstance(tags, list):
            out[str(code).zfill(6)] = "、".join(str(t) for t in tags if t)
        elif tags:
            out[str(code).zfill(6)] = str(tags)
    return out


def _attach_etf_tags(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "基金代码" not in df.columns or "标签" in df.columns:
        return df
    out = df.copy()
    tag_map = _etf_tag_map()
    out.insert(2, "标签", out["基金代码"].astype(str).map(lambda c: tag_map.get(c, "")))
    return out


def _index_config_view() -> dict:
    cfg = _cfg()
    db_path = _db_path(cfg)
    idx = storage.get_index_config(db_path, cfg.get("index", {})) if db_path else cfg.get("index", {})
    codes = [str(c).strip().zfill(6) for c in idx.get("codes", []) if str(c).strip()]
    name_map = {it["code"]: it["name"] for it in index_catalog()}
    items = [{"code": c, "name": name_map.get(c, "")} for c in codes]
    return {
        "codes": codes,
        "items": items,
        "start_date": idx.get("start_date", ""),
        "end_date": idx.get("end_date", ""),
        "lookback_days": idx.get("lookback_days", storage.DEFAULT_INDEX_CONFIG["lookback_days"]),
    }


# ---------------- 日志捕获 ----------------
class JobLogHandler(logging.Handler):
    """把采集过程的日志写入对应任务，供前端实时展示。"""

    def __init__(self, job: dict):
        super().__init__()
        self.job = job
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord):
        with _jobs_lock:
            self.job["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": record.getMessage(),
            })
            if len(self.job["logs"]) > 500:
                self.job["logs"] = self.job["logs"][-500:]


# ---------------- 任务执行 ----------------
def _new_job(kind: str) -> tuple[str, dict]:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id, "kind": kind, "status": "running",
        "start": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "end": None, "logs": [], "result": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job_id, job


def _run_job(job_id: str, func):
    job = _jobs[job_id]
    handler = JobLogHandler(job)
    root = logging.getLogger()
    # 所有子 logger 默认向 root 传播，只在 root 挂一次，避免日志重复
    root.addHandler(handler)
    try:
        result = func()
        with _jobs_lock:
            job["result"] = result
            job["status"] = "success"
    except Exception as exc:
        logger.exception("任务执行失败")
        with _jobs_lock:
            job["result"] = f"{type(exc).__name__}: {exc}"
            job["status"] = "failed"
    finally:
        with _jobs_lock:
            job["end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        root.removeHandler(handler)
        _run_lock.release()


def _submit(kind: str, func) -> str:
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有采集/重跑任务正在运行，请稍后再试")
    job_id, _ = _new_job(kind)
    thread = threading.Thread(target=_run_job, args=(job_id, func), daemon=True)
    thread.start()
    return job_id


def _recompute_derived(config: dict, trigger: str = "手动",
                       month: str | None = None, code: str | None = None,
                       run_id: str | None = None) -> int:
    """按现有宏观观测重算派生指标并写入 SQLite。

    三种模式（优先级从高到低）：
      - run_id 给定（定时调度步骤 6/6）：增量模式。只读取本次采集写入的宏观
        变更范围，推导受影响的派生 (指标, 月份)，仅计算这些行并按 biz_key 幂等
        upsert，不删全表、不对全量历史重复计算。派生表为空（首次）时回退全量。
      - month/code 指定（手动兜底）：局部重算，仅计算目标子集按 biz_key 覆盖。
      - 两者皆空（手动全量）：全量重算并整表替换，清理陈旧口径行。
    """
    from processors.derived import incremental_targets

    db = _db_path(config)
    if not db:
        logger.warning("【数据处理】未配置 SQLite，跳过派生指标重算")
        return 0

    # ---- 增量模式：定时调度随采集触发 ----
    if run_id:
        changed_codes, changed_months = storage.read_macro_changes_by_run(db, run_id)
        if not changed_codes or not changed_months:
            logger.info("【数据处理】%s派生增量重算：本次 run_id=%s 无宏观数据写入，跳过",
                        trigger, run_id)
            return 0
        target_codes, target_months = incremental_targets(changed_codes, changed_months)
        if not target_codes or not target_months:
            logger.info("【数据处理】%s派生增量重算：无受影响派生指标，跳过", trigger)
            return 0
        # 派生表为空（如首次部署）时需全量建立基线，否则增量会留下历史空洞
        existing = storage.read_derived(db)
        if existing.empty:
            logger.info("【数据处理】%s派生表为空，增量重算回退为全量建立基线", trigger)
            _, macro_df = storage.read_macro(db, limit=None)
            result = compute_derived(macro_df)
            n = storage.replace_derived(db, result)
            logger.info("【数据处理】%s触发派生指标全量重算（基线）：%d 行", trigger, n)
            return n
        _, macro_df = storage.read_macro(db, limit=None)
        result = compute_derived(macro_df, target_codes=target_codes,
                                 target_months=target_months)
        n = storage.upsert_derived(db, result)
        logger.info(
            "【数据处理】%s触发派生指标【增量】重算：本次变更 %d 个原始指标/%d 个月份，"
            "映射为 %d 项派生指标/%d 个目标月份，写入 %d 行（按业务键覆盖，未删全表）",
            trigger, len(changed_codes), len(changed_months),
            len(target_codes), len(target_months), n,
        )
        return n

    # ---- 手动模式：局部 / 全量（保持原逻辑）----
    _, macro_df = storage.read_macro(db, limit=None)
    partial = bool(month or code)
    result = compute_derived(macro_df, month=month, only_code=code)
    if partial:
        n = storage.upsert_derived(db, result)
        scope = f"月份={month or '全部'}/指标={code or '全部'}"
        logger.info("【数据处理】%s触发派生指标局部重算（%s）：%d 行（按业务键覆盖）", trigger, scope, n)
    else:
        n = storage.replace_derived(db, result)
        logger.info("【数据处理】%s触发派生指标重算：%d 行（全量替换，已清理陈旧口径行）", trigger, n)
    return n


def _recompute_trade(config: dict, trigger: str = "手动", run_id: str | None = None,
                     trade_date: str | None = None, full: bool = False) -> int:
    """从交易原始表重算 10 指标写入 trade_observation。

    - run_id 给定（定时调度）：增量重算本次写入的交易日
    - trade_date 指定（手动按日）：只重算该交易日（upsert 覆盖）
    - 全参省略：全量重算并替换整表
    """
    db = _db_path(config)
    if not db:
        logger.warning("【交易计算】未配置 SQLite，跳过")
        return 0
    if trade_date:
        from datetime import datetime as _dt
        ex = storage.read_trade_raw(db, "exchange", trade_date, trade_date)
        mg = storage.read_trade_raw(db, "margin", trade_date, trade_date)
        snap = storage.read_trade_raw(db, "snapshot", trade_date, trade_date)
        rec = trade_calc.compute_trade(
            ex.iloc[0].to_dict() if not ex.empty else None,
            mg.iloc[0].to_dict() if not mg.empty else None,
            snap, trade_calc._load_policy(config), trade_date,
        )
        rec["computed_at"] = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        n = storage.upsert_trade(db, pd.DataFrame([rec]), "")
        logger.info("【交易计算】%s按日重算 %s：%d 行", trigger, trade_date, n)
        return n
    n = trade_calc.recompute_trade(db, config=config, run_id=run_id, full=full)
    logger.info("【交易计算】%s重算完成：%d 个交易日", trigger, n)
    return n


def _do_collect(macro_only: bool = False, etf_only: bool = False,
                index_only: bool = False, start: str | None = None,
                end: str | None = None, trigger: str = "Web") -> str:
    config = _cfg()
    db_path = _db_path(config)
    monitor = Monitor(MAIN_BASE, run_type="full", history_path=_history_path(config),
                      db_path=db_path)
    tag = f"【{trigger}】"
    label = "仅宏观" if macro_only else ("仅 ETF" if etf_only else ("仅指数" if index_only else "全量"))
    if start or end:
        label += f"（{start or '起始默认'} ~ {end or '结束默认'}）"
    logger.info("%s步骤 3/5 开始采集：%s | 本次 run_id=%s | 存储=%s",
                tag, label, monitor.run_id, db_path or "文件模式（已弃用）")
    macro_df, etf_df, index_df = _collect(
        config, monitor, macro_only=macro_only, etf_only=etf_only, index_only=index_only,
        start_date=start, end_date=end,
        # 定时调度下宏观固定只更新当月一期；手动/接口采集保持原有区间语义
        macro_current_month=(trigger == "定时调度"),
    )
    # SQLite 按业务键 upsert；空 DataFrame 不会覆盖库中已有数据
    logger.info("%s步骤 4/5 写入存储：宏观 %d 行 / ETF %d 行 / 指数 %d 行（按业务键幂等 upsert）",
                tag, len(macro_df), len(etf_df), len(index_df))
    _finish(monitor, macro_df, etf_df, config, monitor.run_id, index_df)
    failed = sum(1 for r in monitor.records if r["status"] == "failed")
    if db_path:
        counts = storage.status_counts(db_path)
        summary = (f"SQLite 已更新：宏观 {counts['macro_rows']} 行 / ETF {counts['etf_rows']} 行 / "
                   f"指数 {counts['index_rows']} 行，接口失败 {failed} 次")
    else:
        summary = (f"宏观 {len(macro_df)} 行 / ETF {len(etf_df)} 行 / "
                   f"指数 {len(index_df)} 行，接口失败 {failed} 次")
    logger.info("%s步骤 5/5 采集完成：%s", tag, summary)
    if trigger == "定时调度":
        # 派生指标随定时任务完成后重算；改为增量模式：只重算本次采集写入数据
        # 所影响的派生指标与月份，不再每次都全量重算并替换整表。
        try:
            n = _recompute_derived(config, trigger="定时调度", run_id=monitor.run_id)
            logger.info("%s步骤 6/6 派生指标定时增量重算完成：%d 行", tag, n)
        except Exception as exc:
            logger.warning("%s派生指标定时增量重算失败：%s", tag, exc)
        # 交易原始数据随定时任务采集并重算（仅全量触发时；四类数据并行接入）
        if not (macro_only or etf_only or index_only):
            try:
                tmon = Monitor(MAIN_BASE, run_type="scheduled", history_path=_history_path(config),
                               db_path=db_path)
                traw = collect_trade_raw(config, tmon)
                _finish_trade(tmon, traw, config)
                tn = _recompute_trade(config, trigger="定时调度", run_id=tmon.run_id, full=True)
                logger.info("%s步骤 6/6 交易原始采集+计算完成：%d 个交易日", tag, tn)
            except Exception as exc:
                logger.warning("%s交易采集/计算失败：%s", tag, exc)
    return summary


def _do_collect_trade(sources: list[str] | None = None, start: str | None = None,
                      end: str | None = None, trigger: str = "Web") -> str:
    """采集交易原始数据（只落库），供 Web 后台任务调用。"""
    config = _cfg()
    db_path = _db_path(config)
    monitor = Monitor(MAIN_BASE, run_type="interface", history_path=_history_path(config),
                      db_path=db_path)
    tag = f"【{trigger}】"
    logger.info("%s交易原始采集开始：源=%s | 区间=%s~%s", tag,
                sources or "全部", start or "默认", end or "默认")
    raw = collect_trade_raw(config, monitor, start_date=start, end_date=end, sources=sources)
    written = _finish_trade(monitor, raw, config)
    failed = sum(1 for r in monitor.records if r["status"] == "failed")
    summary = (f"交易原始落库：交易所 {written.get('exchange', 0)} / 两融 {written.get('margin', 0)} / "
               f"快照 {written.get('snapshot', 0)} 行，接口失败 {failed} 次。请到数据处理·交易 Tab 计算。")
    logger.info("%s交易采集完成：%s", tag, summary)
    return summary


def _do_retry() -> str:
    from main import _retry_failed
    config = _cfg()
    logger.info("Web 触发失败接口重跑")
    code = _retry_failed(config, config.get("monitor", {}))
    return "重跑完成" if code == 0 else "重跑后仍有失败接口"


def _do_retry_task(task_id: str) -> str:
    """按任务ID重跑单条失败任务（来自监控失败明细）。"""
    from main import _retry_failed
    config = _cfg()
    db_path = _db_path(config)
    monitor = Monitor(MAIN_BASE, run_type="retry", history_path=_history_path(config),
                      db_path=db_path)
    history = Monitor.load(_history_path(config), db_path)
    task = monitor.find_failed_task(task_id, history)
    if not task:
        return f"未找到任务ID={task_id} 的失败记录，可能已被清理或已重跑成功"
    logger.info("Web 触发单条失败重跑：%s / %s（%s，%s）",
                task_id, task["interface_name"], task["failed_date"],
                format_failure_detail(task))
    code = _retry_failed(config, config.get("monitor", {}), tasks=[task])
    return "单条重跑完成" if code == 0 else "单条重跑后仍有失败接口"


def _do_collect_interface(category: str, codes: list[str],
                          start: str | None, end: str | None) -> str:
    config = _cfg()
    return collect_interface(config, category, codes, start, end)


# ---------------- 数据读取 ----------------
def _read_csv(kind: str) -> pd.DataFrame:
    """兼容旧调用名：从 SQLite 主存储读取当前全量业务数据。"""
    config = _cfg()
    db_path = _db_path(config)
    if not db_path:
        from main import _latest_csv
        path = _latest_csv(kind, config)
        if not path:
            return pd.DataFrame()
        try:
            return pd.read_csv(path, dtype={"基金代码": str})
        except OSError as exc:
            logger.warning("读取 CSV 失败 %s: %s", path, exc)
            return pd.DataFrame()
    if kind == "macro":
        return storage.read_macro(db_path)[1]
    if kind == "etf":
        return storage.read_etf(db_path)[1]
    if kind == "index":
        return storage.read_index(db_path)[1]
    return pd.DataFrame()


def _frame_to_records(df: pd.DataFrame, limit: int = 5000) -> list[dict]:
    if df.empty:
        return []
    import numpy as np
    head = df.head(limit)
    records = []
    for row in head.itertuples(index=False, name=None):
        record = {}
        for col, val in zip(head.columns, row):
            if val is None:
                record[col] = None
            elif isinstance(val, (float, np.floating)) and not np.isfinite(val):
                record[col] = None  # NaN / ±Inf 非法 JSON
            elif isinstance(val, (np.integer,)):
                record[col] = int(val)
            elif isinstance(val, (np.bool_,)):
                record[col] = bool(val)
            else:
                record[col] = val
        records.append(record)
    return records


# ---------------- 定时调度 ----------------
class Scheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_triggered: str | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        logger.info("定时调度器已启动")

    def _loop(self):
        while not self._stop.wait(15):
            try:
                cfg = _cfg().get("scheduler", {})
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                hhmm = now.strftime("%H:%M")
                target = cfg.get("time", "08:30")
                run_type = cfg.get("run_type", "full")

                if not cfg.get("enabled"):
                    # 仅在同一状态首次出现时说明一次，避免 15 秒轮询刷屏
                    if getattr(self, "_last_decision", None) != "disabled":
                        logger.info("【定时调度】检查 %s %s：调度未启用，跳过（可在看板开启）", today, hhmm)
                        self._last_decision = "disabled"
                    continue
                if hhmm != target:
                    if getattr(self, "_last_decision", None) not in (None, "waiting"):
                        logger.info("【定时调度】检查 %s %s：未到触发时刻（配置 %s，下次 %s）",
                                    today, hhmm, target, self.next_run())
                        self._last_decision = "waiting"
                    continue
                # 已进入触发分钟（HH:MM 匹配），逐步给出判定明细
                if self._last_triggered == today:
                    if getattr(self, "_last_decision", None) != "already":
                        logger.info("【定时调度】%s %s 到达触发时刻：今日已触发过，跳过（每天仅一次）", today, hhmm)
                        self._last_decision = "already"
                    continue
                if _run_lock.locked():
                    if getattr(self, "_last_decision", None) != "busy":
                        logger.info("【定时调度】%s %s 到达触发时刻：已有任务运行中，跳过本轮", today, hhmm)
                        self._last_decision = "busy"
                    continue

                self._last_triggered = today
                self._last_decision = "triggered"
                logger.info("【定时调度】步骤 1/5 触发：日期=%s 时刻=%s 类型=%s（今日首次、无任务占用）",
                            today, hhmm, run_type)
                try:
                    job_id = _submit("scheduled", lambda rt=run_type: _do_collect(
                        macro_only=(rt == "macro"), etf_only=(rt == "etf"),
                        index_only=(rt == "index"), trigger="定时调度",
                    ))
                    logger.info("【定时调度】步骤 2/5 任务已提交：job_id=%s，进入后台采集线程", job_id)
                except HTTPException as exc:
                    logger.warning("【定时调度】任务提交被拒：%s", exc.detail)
            except Exception as exc:
                logger.warning("调度器异常：%s", exc)

    def next_run(self) -> str:
        cfg = _cfg().get("scheduler", {})
        if not cfg.get("enabled"):
            return "未启用"
        now = datetime.now()
        target = datetime.combine(now.date(), datetime.strptime(cfg.get("time", "08:30"), "%H:%M").time())
        if target <= now:
            target += timedelta(days=1)
        return target.strftime("%Y-%m-%d %H:%M")


scheduler = Scheduler()


# ---------------- API ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    """首页：为静态资源注入 mtime 版本号，避免浏览器缓存旧版 CSS/JS 导致改动不生效。"""
    html_path = BASE_DIR / "static" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    for asset in ("style.css", "app.js", "echarts.min.js"):
        asset_path = BASE_DIR / "static" / asset
        try:
            version = int(asset_path.stat().st_mtime)
        except OSError:
            version = 0
        html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={version}\"")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def api_status():
    cfg = _cfg()
    db_path = _db_path(cfg)
    last_run = {}
    last_counts = {}
    failed_count = 0
    today_calls = 0
    today_new = {}
    trade_rows = 0
    trade_raw_rows = {}
    if db_path:
        counts = storage.status_counts(db_path)
        last_run = counts.get("last_run") or {}
        last_counts = counts.get("last_counts") or {}
        failed_count = counts["failed_total"]
        today_calls = counts["today_calls"]
        macro_rows, macro_indicators = counts["macro_rows"], counts["macro_indicators"]
        etf_rows, etf_codes = counts["etf_rows"], counts["etf_codes"]
        index_rows, index_codes = counts["index_rows"], counts["index_codes"]
        today_new = counts.get("today_new", {})
        trade_rows = counts.get("trade_rows", 0)
        trade_raw_rows = counts.get("trade_raw_rows", {})
    else:
        history = Monitor.load(_history_path(cfg))
        if not history.empty:
            last = history.sort_values("run_time").iloc[-1]
            last_run = {
                "id": last["run_id"], "date": last["run_date"], "time": last["run_time"],
                "type": last["run_type"],
            }
            failed_count = int((history["status"] == "failed").sum())
            today_calls = int((history["run_date"] == datetime.now().strftime("%Y-%m-%d")).sum())
        macro, etf, index = _read_csv("macro"), _read_csv("etf"), _read_csv("index")
        macro_rows = len(macro)
        macro_indicators = int(macro["指标代码"].nunique()) if not macro.empty else 0
        etf_rows = len(etf)
        etf_codes = int(etf["基金代码"].nunique()) if not etf.empty else 0
        index_rows = len(index)
        index_codes = int(index["指数代码"].nunique()) if not index.empty else 0
    return {
        "last_run": last_run,
        "last_counts": last_counts,
        "failed_total": failed_count,
        "today_calls": today_calls,
        "macro_rows": macro_rows,
        "macro_indicators": macro_indicators,
        "etf_rows": etf_rows,
        "etf_codes": etf_codes,
        "index_rows": index_rows,
        "index_codes": index_codes,
        "trade_rows": trade_rows,
        "trade_raw_rows": trade_raw_rows,
        "today_new": today_new,
        "scheduler": {
            "enabled": bool(cfg.get("scheduler", {}).get("enabled")),
            "time": cfg.get("scheduler", {}).get("time", "08:30"),
            "run_type": cfg.get("scheduler", {}).get("run_type", "full"),
            "next_run": scheduler.next_run(),
        },
        "running": _run_lock.locked(),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # 最近一个已结束交易日，供前端「精确采集 / 全量采集」日期默认取值
        "last_trade_date": last_completed_trade_day(),
    }


@app.get("/api/monitor/report")
def api_monitor_report():
    cfg = _cfg()
    db_path = _db_path(cfg)
    monitor = Monitor(MAIN_BASE, history_path=_history_path(cfg), db_path=db_path)
    stats_df = storage.read_collection_stats(db_path) if db_path else pd.DataFrame()
    report = monitor.build_report(stats_df=stats_df)
    return {sheet: _frame_to_records(df) for sheet, df in report.items()}


@app.get("/api/monitor/failed")
def api_monitor_failed():
    cfg = _cfg()
    monitor = Monitor(MAIN_BASE, history_path=_history_path(cfg), db_path=_db_path(cfg))
    return {"tasks": monitor.failed_tasks()}


def _paged(df: pd.DataFrame, page: int, page_size: int) -> dict:
    """分页返回 DataFrame，附带 total/pages/page/page_size 元信息。"""
    total = len(df)
    page = max(1, page)
    page_size = min(max(1, page_size), 100)  # 单页上限 100 行
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        page = pages
    chunk = df.iloc[(page - 1) * page_size: page * page_size]
    return {
        "total": total, "pages": pages, "page": page, "page_size": page_size,
        "rows": _frame_to_records(chunk, page_size),
    }


@app.get("/api/data/macro")
def api_data_macro(code: str | None = None, start: str | None = None,
                   end: str | None = None, page: int = 1, page_size: int = 25,
                   order: str = "desc"):
    # 宏观数据按月存储，兼容前端传入的 YYYY-MM-DD
    start = start[:7] if start else None
    end = end[:7] if end else None
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    date_order = "asc" if str(order).lower() == "asc" else "desc"
    db_path = _db_path(_cfg())
    if db_path:
        total, df = storage.read_macro(db_path, code, start, end, limit=page_size,
                                       offset=0, date_order=date_order)
        pages = max(1, (total + page_size - 1) // page_size)
        if page > pages:
            page = pages
        _, df = storage.read_macro(
            db_path, code, start, end, limit=page_size,
            offset=(page - 1) * page_size, date_order=date_order)
        return {
            "total": total, "pages": pages, "page": page, "page_size": page_size,
            "rows": _frame_to_records(df, page_size),
        }
    df = _read_csv("macro")
    if code and not df.empty:
        df = df[df["指标代码"] == code]
    if start and not df.empty:
        df = df[df["日期"] >= start]
    if end and not df.empty:
        df = df[df["日期"] <= end]
    if not df.empty and "日期" in df.columns:
        df = df.sort_values("日期", ascending=(date_order == "asc"))
    return _paged(df, page, page_size)


@app.get("/api/data/etf")
def api_data_etf(code: str | None = None, start: str | None = None,
                 end: str | None = None, page: int = 1, page_size: int = 25,
                 order: str = "desc"):
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    date_order = "asc" if str(order).lower() == "asc" else "desc"
    db_path = _db_path(_cfg())
    if db_path:
        total, _ = storage.read_etf_with_changes(db_path, code, start, end, limit=1)
        pages = max(1, (total + page_size - 1) // page_size)
        if page > pages:
            page = pages
        _, df = storage.read_etf_with_changes(
            db_path, code, start, end, limit=page_size,
            offset=(page - 1) * page_size, date_order=date_order)
        df = _attach_etf_tags(df)
        return {
            "total": total, "pages": pages, "page": page, "page_size": page_size,
            "rows": _frame_to_records(df, page_size),
        }
    df = compute_etf_changes(_read_csv("etf"))
    if code and not df.empty:
        df = df[df["基金代码"] == code]
    if start and not df.empty:
        df = df[df["交易日期"] >= start]
    if end and not df.empty:
        df = df[df["交易日期"] <= end]
    if not df.empty and "交易日期" in df.columns:
        df = df.sort_values("交易日期", ascending=(date_order == "asc"))
    return _paged(_attach_etf_tags(df), page, page_size)


@app.get("/api/data/index")
def api_data_index(code: str | None = None, start: str | None = None,
                   end: str | None = None, page: int = 1, page_size: int = 25,
                   order: str = "desc"):
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    date_order = "asc" if str(order).lower() == "asc" else "desc"
    db_path = _db_path(_cfg())
    if db_path:
        total, _ = storage.read_index(db_path, code, start, end, limit=1)
        pages = max(1, (total + page_size - 1) // page_size)
        if page > pages:
            page = pages
        _, df = storage.read_index(
            db_path, code, start, end, limit=page_size,
            offset=(page - 1) * page_size, date_order=date_order)
        return {
            "total": total, "pages": pages, "page": page, "page_size": page_size,
            "rows": _frame_to_records(df, page_size),
        }
    df = _read_csv("index")
    if code and not df.empty:
        df = df[df["指数代码"] == code]
    if start and not df.empty:
        df = df[df["交易日期"] >= start]
    if end and not df.empty:
        df = df[df["交易日期"] <= end]
    if not df.empty and "交易日期" in df.columns:
        df = df.sort_values("交易日期", ascending=(date_order == "asc"))
    return _paged(df, page, page_size)


@app.get("/api/data/trade")
def api_data_trade(code: str | None = None, start: str | None = None,
                   end: str | None = None, page: int = 1, page_size: int = 25,
                   order: str = "desc"):
    """采集数据·日交易 Tab：分页预览 5 张交易原始表（code 选表）。

    金额字段统一换算为万亿、保留两位小数；其它字段原样。表头中文化，隐藏 run_id。
    """
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    date_order = "asc" if str(order).lower() == "asc" else "desc"
    table = (code or "exchange").strip().lower()
    if table not in ("exchange", "margin", "snapshot", "breadth", "congestion"):
        table = "exchange"
    db_path = _db_path(_cfg())
    if not db_path:
        return {"total": 0, "pages": 1, "page": page, "page_size": page_size, "rows": []}
    df = storage.read_trade_raw(db_path, table)
    if df.empty:
        return {"total": 0, "pages": 1, "page": page, "page_size": page_size, "rows": []}
    if start:
        df = df[df["trade_date"] >= str(start)[:10]]
    if end:
        df = df[df["trade_date"] <= str(end)[:10]]
    df = df.sort_values("trade_date", ascending=(date_order == "asc")).reset_index(drop=True)
    total = len(df)
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        page = pages
    chunk = df.iloc[(page - 1) * page_size: page * page_size]
    rows = []
    for record in _frame_to_records(chunk, page_size):
        item = {}
        for k, v in record.items():
            if k == "run_id":  # 内部采集批次号，不对预览暴露
                continue
            if k in TRADE_RAW_AMOUNT_DIVISOR and isinstance(v, (int, float)):
                # 金额统一换算为万亿并保留两位小数；非金额字段原样
                v = round(v / TRADE_RAW_AMOUNT_DIVISOR[k], 2)
            item[TRADE_RAW_COLUMNS_CN.get(k, k)] = v
        rows.append(item)
    return {
        "total": total, "pages": pages, "page": page, "page_size": page_size,
        "rows": rows,
    }


@app.get("/api/derived")
def api_derived(month: str | None = None):
    """数据处理模块：返回指定月份的派生指标与可选月份列表。"""
    db = _require_db(_cfg())
    months = storage.derived_months(db)
    target = month or (months[0] if months else "")
    if not target:
        _, macro_df = storage.read_macro(db, limit=None)
        if not macro_df.empty:
            target = str(macro_df["日期"].max())[:7]
    rows = storage.read_derived(db, target) if target else pd.DataFrame()
    return {
        "month": target,
        "months": months,
        "rows": rows.to_dict("records"),
        "count": len(rows),
    }


@app.post("/api/derived/recompute")
def api_derived_recompute(payload: dict | None = None):
    """手动兜底重算；主触发方式为定时调度，见 Scheduler/_do_collect。

    可选 body：{month: 'YYYY-MM', code: '指标代码'}。
    两者均省略时全量重算；指定其一/两者则仅重算目标子集（幂等覆盖，不误删其他数据）。
    """
    payload = payload or {}
    month = payload.get("month") or None
    code = payload.get("code") or None
    if month:
        try:
            datetime.strptime(str(month)[:7], "%Y-%m")
        except ValueError:
            raise HTTPException(status_code=400, detail="month 格式应为 YYYY-MM")
        month = str(month)[:7]
    if code:
        from processors.derived import INDICATORS
        code = str(code).strip()
        if code not in INDICATORS:
            raise HTTPException(status_code=400, detail=f"未知指标代码：{code}")
    trigger = "看板手动"
    scope = []
    if month:
        scope.append(month)
    if code:
        scope.append(code)
    if scope:
        trigger += "（" + "/".join(scope) + "）"
    n = _recompute_derived(_cfg(), trigger=trigger, month=month, code=code)
    return {"ok": True, "rows": n, "month": month, "code": code}


@app.get("/api/derived/export")
def api_derived_export(month: str | None = None):
    db = _require_db(_cfg())
    target = month or (storage.derived_months(db) or [""])[0]
    df = storage.read_derived(db, target) if target else pd.DataFrame()
    if df.empty:
        text = "code,name,date,value,unit,formula,grp,source,computed_at\n"
    else:
        text = df.to_csv(index=False)
    return Response(
        content=text.encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="derived_{(target or "all").replace("-", "")}.csv"'},
    )


@app.get("/api/trade/raw")
def api_trade_raw(table: str = "exchange", start: str | None = None,
                  end: str | None = None, limit: int = 200):
    """采集系统·日交易预览 Tab：预览 5 张交易原始表的落库数据（含原始单位）。"""
    cfg = _cfg()
    db = _require_db(cfg)
    if table not in ("exchange", "margin", "snapshot", "breadth", "congestion"):
        raise HTTPException(status_code=400, detail="table 应为 exchange/margin/snapshot/breadth/congestion")
    df = storage.read_trade_raw(db, table, start, end)
    total = len(df)
    return {"table": table, "total": total,
            "rows": _frame_to_records(df.head(limit), limit=limit)}


@app.get("/api/trade/latest")
def api_trade_latest():
    """数据处理·交易 Tab：10 指标最新值 + 原始数据覆盖概况 + 指标定义。"""
    cfg = _cfg()
    db = _require_db(cfg)
    return trade_calc.latest_snapshot(db)


@app.get("/api/trade/data")
def api_trade_data(start: str | None = None, end: str | None = None):
    """交易计算宽表数据（供看板与导出），按交易日升序。"""
    cfg = _cfg()
    db = _require_db(cfg)
    df = storage.read_trade(db, start, end)
    return {"total": len(df), "rows": _frame_to_records(df)}


@app.post("/api/trade/recompute")
def api_trade_recompute(payload: dict | None = None):
    """手动触发交易指标重算；body 可选 {trade_date:'YYYY-MM-DD', full:bool}。"""
    payload = payload or {}
    trade_date = (payload.get("trade_date") or "").strip() or None
    full = bool(payload.get("full", False))
    if trade_date:
        try:
            datetime.strptime(trade_date[:10], "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="trade_date 格式应为 YYYY-MM-DD")
    n = _recompute_trade(_cfg(), trigger="看板手动", trade_date=trade_date, full=full)
    return {"ok": True, "rows": n, "trade_date": trade_date, "full": full}


@app.post("/api/collect/trade")
def api_collect_trade(payload: dict | None = None):
    """精确采集交易原始数据（Web 后台任务）。body 可选 {sources:[], start, end}。"""
    payload = payload or {}
    sources = payload.get("sources") or None
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    if sources:
        sources = [s for s in sources if s in ("exchange", "margin", "snapshot")]
    start = (payload.get("start") or "").strip() or None
    end = (payload.get("end") or "").strip() or None
    job_id = _submit("collect", lambda: _do_collect_trade(sources, start, end))
    return {"job_id": job_id}


@app.get("/api/dashboard")
def api_dashboard(months: int = 12, etf_months: int = 12, days: int | None = None,
                  macro_start: str | None = None, macro_end: str | None = None,
                  etf_start: str | None = None, etf_end: str | None = None,
                  overlay_index: str = "000001", trade_days: int | None = None,
                  trade_months: int = 12, trade_start: str | None = None,
                  trade_end: str | None = None):
    """数据看板：宏观/ETF/交易三组图，共用系统数据源。

    months/etf_months/trade_months 均为月份范围（0=全部），默认近一年；
    days 为兼容旧前端的别名（仅作用于 ETF）。
    macro_start/macro_end、etf_start/etf_end、trade_start/trade_end 为 YYYY-MM-DD 精确日期区间，
    分别优先于 months、etf_months、trade_months。宏观 / ETF / 交易 Tab 日期口径完全一致。
    overlay_index 为汇金图上方叠加的指数代码，默认上证指数 000001。
    """
    cfg = _cfg()
    db = _require_db(cfg)
    months = max(0, min(int(months or 0), 240))
    if days is not None:  # 兼容旧参数（原 days 表示交易日数，仅用于回退）
        etf_months = int(days)
    etf_months = max(0, min(int(etf_months or 0), 240))
    trade_months = max(0, min(int(trade_months or 0), 240))

    derived_df = storage.read_derived(db)
    _, macro_df = storage.read_macro(db, limit=None)
    # 读取全部指数：宏观图5固定用上证指数(000001)，ETF 叠加指数由 overlay_index 指定
    _, index_df = storage.read_index(db, limit=None)
    _, etf_df = storage.read_etf_with_changes(db, limit=None)
    etf_cfg = storage.get_etf_config(db, cfg.get("etf", {}))
    overlay_index = str(overlay_index or "000001").strip().zfill(6)

    # 可用叠加指数：从实际有数据的指数中生成 {code, name}，顺序为常用指数优先
    index_options = []
    if not index_df.empty and "指数代码" in index_df.columns:
        for code, grp in index_df.groupby("指数代码", sort=False):
            code = str(code).strip().zfill(6)
            name = str(grp["指数名称"].iloc[0]) if "指数名称" in grp.columns else code
            index_options.append({"code": code, "name": name})
        order = {"000001": 0, "000300": 1, "000688": 2, "399006": 3}
        index_options.sort(key=lambda x: (order.get(x["code"], 99), x["code"]))

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "months": months,
        "macro_start": macro_start or "",
        "macro_end": macro_end or "",
        "etf_months": etf_months,
        "etf_start": etf_start or "",
        "etf_end": etf_end or "",
        "trade_months": trade_months,
        "trade_start": trade_start or "",
        "trade_end": trade_end or "",
        "overlay_index": overlay_index,
        "index_options": index_options,
        "macro": {"charts": build_macro_charts(derived_df, macro_df, index_df, months,
                                               macro_start, macro_end)},
        "etf": {"charts": build_etf_charts(etf_df, etf_cfg.get("tags", {}), etf_months,
                                           etf_start, etf_end, index_df, overlay_index)},
        "trade": {"charts": build_trade_charts(
            storage.read_trade(db), days=trade_days,
            months=trade_months, start=trade_start, end=trade_end)},
    }


@app.get("/api/dashboard/etf-daily")
def api_dashboard_etf_daily(months: int = 12, date: str | None = None,
                            start: str | None = None, end: str | None = None,
                            view: str = "change"):
    """汇金维度按天明细：不带 date 返回每日汇总+合计；带 date 返回当日各 ETF 明细。
    view=change（默认）为份额变化/净流入口径；view=total 为每日份额存量合计口径。
    start/end 为 YYYY-MM-DD 精确日期区间，优先于 months。
    """
    cfg = _cfg()
    db = _require_db(cfg)
    months = max(0, min(int(months or 0), 240))
    _, etf_df = storage.read_etf_with_changes(db, limit=None)
    etf_cfg = storage.get_etf_config(db, cfg.get("etf", {}))
    return build_etf_daily_detail(etf_df, etf_cfg.get("tags", {}), months, date,
                                  start, end, view=view)


@app.get("/api/files")
def api_files():
    out_dir = BASE_DIR / _cfg().get("output", {}).get("dir", "output")
    files = []
    for path in sorted(out_dir.rglob("*")):
        # 导出文件由「导出」功能自动生成与下载，不在「输出文件」Tab 展示
        if not path.is_file():
            continue
        rel = path.relative_to(out_dir)
        if rel.parts and rel.parts[0] == "exports":
            continue
        files.append({
            "name": str(rel),
            "size": round(path.stat().st_size / 1024, 1),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return {"files": files}


@app.get("/api/download")
def api_download(name: str):
    out_dir = (BASE_DIR / _cfg().get("output", {}).get("dir", "output")).resolve()
    target = (out_dir / name).resolve()
    if not str(target).startswith(str(out_dir)) or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, filename=target.name)


def _export_names(files: list[str], config: dict) -> list[str]:
    out_dir = (BASE_DIR / config.get("output", {}).get("dir", "output")).resolve()
    return [str(Path(f).resolve().relative_to(out_dir)) for f in files]


def _require_db(config: dict) -> Path:
    db_path = _db_path(config)
    if not db_path:
        raise HTTPException(status_code=400, detail="当前存储模式为 file，导出请先切换到 sqlite")
    return db_path


@app.post("/api/export/macro")
def api_export_macro():
    config = _cfg()
    db_path = _require_db(config)
    _, df = storage.read_macro(db_path)
    files = export_macro(df, BASE_DIR, config)
    storage.log_export(db_path, "macro", files[-1], {"rows": len(df)})
    return {"files": _export_names(files, config)}


@app.post("/api/export/etf")
def api_export_etf():
    config = _cfg()
    db_path = _require_db(config)
    _, df = storage.read_etf_with_changes(db_path)
    df = _attach_etf_tags(df)
    files = export_etf(df, BASE_DIR, config)
    storage.log_export(db_path, "etf", files[-1], {"rows": len(df)})
    return {"files": _export_names(files, config)}


@app.post("/api/export/index")
def api_export_index():
    config = _cfg()
    db_path = _require_db(config)
    _, df = storage.read_index(db_path)
    files = export_index(df, BASE_DIR, config)
    storage.log_export(db_path, "index", files[-1], {"rows": len(df)})
    return {"files": _export_names(files, config)}


@app.post("/api/export/monitor")
def api_export_monitor():
    config = _cfg()
    db_path = _require_db(config)
    history = storage.read_interface_runs(db_path)
    files = export_monitor(history, BASE_DIR, config)
    storage.log_export(db_path, "monitor", files[-1], {"rows": len(history)})
    return {"files": _export_names(files, config)}


@app.post("/api/export/full")
def api_export_full():
    config = _cfg()
    db_path = _require_db(config)
    _, macro_df = storage.read_macro(db_path)
    _, etf_df = storage.read_etf_with_changes(db_path)
    etf_df = _attach_etf_tags(etf_df)
    history = storage.read_interface_runs(db_path)
    files = export_full(macro_df, etf_df, history, BASE_DIR, config)
    storage.log_export(db_path, "full", files[-1], {
        "macro": len(macro_df), "etf": len(etf_df), "monitor": len(history),
    })
    return {"files": _export_names(files, config)}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return dict(job)


@app.get("/api/jobs")
def api_jobs():
    with _jobs_lock:
        jobs = list(_jobs.values())[-20:]
    return {"jobs": [{
        "id": j["id"], "kind": j["kind"], "status": j["status"],
        "start": j["start"], "end": j["end"],
    } for j in reversed(jobs)]}


@app.get("/api/live-logs")
def api_live_logs(after: int = 0):
    """实时监控日志：返回序号大于 after 的全局日志（含定时调度），跨任务保留最近 5000 行。"""
    with _live_lock:
        logs = [dict(e) for e in _live_logs if e["seq"] > after]
        latest = _live_logs[-1]["seq"] if _live_logs else 0
        total = len(_live_logs)
    return {"logs": logs, "latest": latest, "total": total, "limit": LIVE_LOG_LIMIT}


@app.post("/api/collect")
def api_collect(payload: dict | None = None):
    payload = payload or {}
    macro_only = bool(payload.get("macro_only", False))
    etf_only = bool(payload.get("etf_only", False))
    index_only = bool(payload.get("index_only", False))
    start = str(payload.get("start") or "").strip() or None
    end = str(payload.get("end") or "").strip() or None
    job_id = _submit("collect", lambda: _do_collect(macro_only, etf_only, index_only, start, end))
    return {"job_id": job_id}


@app.get("/api/catalog")
def api_catalog():
    """返回可精确采集的指标/基金清单，供前端下拉。"""
    cfg = _cfg()
    macro_items = macro_catalog()
    # 用最新数据补充每个指标的数据条数与日期范围，便于用户选择
    macro_df = _read_csv("macro")
    if not macro_df.empty:
        stats = macro_df.groupby("指标代码")["日期"].agg(["count", "min", "max"]).to_dict("index")
        for item in macro_items:
            s = stats.get(item["code"])
            if s:
                item["rows"] = int(s["count"])
                item["last_date"] = str(s["max"])
                item["first_date"] = str(s["min"])
    for item in macro_items:
        item.setdefault("rows", 0)
        item.setdefault("last_date", "")
        item.setdefault("first_date", "")

    etf_view = _etf_config_view()
    etf_codes = etf_view["codes"]
    name_map = {it["code"]: it["name"] for it in etf_view["items"]}
    listing = {it["code"]: it["listing_date"] for it in etf_view["items"]}
    etf_df = _read_csv("etf")
    etf_items = []
    for code in etf_codes:
        item = {
            "code": code, "name": name_map.get(code, ""), "iface": "etf.shares+nav",
            "unit": "亿份 / 亿元", "granularity": "交易日",
            "source": "上交所/深交所份额 + 天天基金净值",
            "listing_date": listing.get(code, ""),
        }
        if not etf_df.empty and "基金代码" in etf_df.columns:
            sub = etf_df[etf_df["基金代码"].astype(str).str.zfill(6) == code]
            if not sub.empty:
                item["rows"] = int(len(sub))
                item["last_date"] = str(sub["交易日期"].max())
                item["first_date"] = str(sub["交易日期"].min())
        item.setdefault("rows", 0)
        item.setdefault("last_date", "")
        item.setdefault("first_date", "")
        etf_items.append(item)

    index_view = _index_config_view()
    index_df = _read_csv("index")
    index_items = []
    for it in index_view["items"]:
        code = it["code"]
        item = {
            "code": code, "name": it.get("name", ""), "iface": f"index.{code}",
            "unit": "点", "granularity": "交易日", "source": "新浪财经（akshare）",
        }
        if not index_df.empty and "指数代码" in index_df.columns:
            sub = index_df[index_df["指数代码"].astype(str).str.zfill(6) == code]
            if not sub.empty:
                item["rows"] = int(len(sub))
                item["last_date"] = str(sub["交易日期"].max())
                item["first_date"] = str(sub["交易日期"].min())
        item.setdefault("rows", 0)
        item.setdefault("last_date", "")
        item.setdefault("first_date", "")
        index_items.append(item)

    # 交易：三类原始数据源
    from collectors.trade import catalog as trade_source_catalog
    trade_items = []
    for it in trade_source_catalog():
        item = dict(it)
        item.setdefault("rows", 0)
        item.setdefault("last_date", "")
        item.setdefault("first_date", "")
        try:
            dates = storage.trade_raw_dates(_db_path(cfg), it["code"])
            if dates:
                item["rows"] = len(dates) if it["code"] != "snapshot" else "—"
                item["first_date"] = dates[0]
                item["last_date"] = dates[-1]
        except Exception:
            pass
        trade_items.append(item)

    return {
        "macro": macro_items,
        "etf": etf_items,
        "index": index_items,
        "trade": trade_items,
        "defaults": {
            "macro_start": cfg.get("macro", {}).get("start_date", ""),
            "etf_start": cfg.get("etf", {}).get("start_date", ""),
            "etf_end": cfg.get("etf", {}).get("end_date", ""),
            "index_start": cfg.get("index", {}).get("start_date", ""),
            "index_end": cfg.get("index", {}).get("end_date", ""),
            "index_lookback": cfg.get("index", {}).get("lookback_days", 1),
        },
        "keep_snapshots": cfg.get("output", {}).get("keep_snapshots", 3),
    }


@app.post("/api/import/etf")
async def api_import_etf(files: list[UploadFile] = File(...)):
    """导入 Choice「份额与规模_<代码>.xlsx」文件，作为 ETF 采集数据写入 SQLite。"""
    from importer import check_file_limit, import_choice_file

    try:
        check_file_limit(files)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cfg = _cfg()
    db_path = _db_path(cfg)
    if not db_path:
        raise HTTPException(status_code=400, detail="当前为文件模式，导入需要启用 SQLite 存储")

    results: list[dict] = []
    errors: list[dict] = []
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_import")
    # 导入计入接口监控：每个文件一条记录（成功/失败、行数、耗时、真实运行时刻）
    monitor = Monitor(MAIN_BASE, run_type="import",
                      history_path=_history_path(cfg), db_path=db_path)
    monitor.run_id = run_id  # 与 ETF 数据行 run_id 保持一致，便于追溯
    logger.info("ETF 文件导入开始：run_id=%s，共 %d 个文件", run_id, len(files))
    # 文件名是标的代码的来源（份额与规模_<代码>.xlsx），临时目录内必须保留原名
    tmp_dir = Path(tempfile.mkdtemp(prefix="etf_import_"))
    try:
        for upload in files:
            name = Path(upload.filename or "").name
            if not name.lower().endswith((".xlsx", ".xlsm")):
                msg = "仅支持 .xlsx/.xlsm 文件"
                logger.warning("ETF 文件导入跳过 [%s]：%s", name, msg)
                errors.append({"file": name, "error": msg})
                continue
            target = tmp_dir / name
            try:
                with target.open("wb") as handle:
                    handle.write(await upload.read())
                summary = import_choice_file(target, db_path, run_id=run_id,
                                             display_name=name, monitor=monitor)
                results.append(summary)
                logger.info("ETF 文件导入成功 [%s]：标的 %s，%d 行（%s ~ %s）",
                            name, summary["code"], summary["rows"], summary["start"], summary["end"])
            except Exception as exc:
                logger.warning("ETF 文件导入失败 [%s]：%s", name, exc)
                errors.append({"file": name, "error": str(exc)})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    # 非 xlsx 文件（扩展名校验失败）也补一条监控记录，保证文件数统计完整
    for err in errors:
        if err["error"] == "仅支持 .xlsx/.xlsm 文件" and not any(
            rec["interface_name"].endswith(f"({err['file']})") for rec in monitor.records
        ):
            monitor.record(
                category="ETF", interface_key="etf.import",
                interface_name=f"Choice份额规模导入({err['file']})",
                status="failed", attempts=1, rows=0, error=err["error"],
                params={"file": err["file"]}, duration_sec=0.0,
            )
    monitor.save()
    logger.info("ETF 文件导入结束：run_id=%s，成功 %d / 失败 %d / 共 %d 行",
                run_id, len(results), len(errors), sum(r["rows"] for r in results))

    return {
        "ok": not errors,
        "run_id": run_id,
        "imported": results,
        "errors": errors,
        "total_rows": sum(r["rows"] for r in results),
    }


@app.get("/api/macro/config")
def api_macro_config_get():
    """返回当前宏观采集配置（月度区间）。"""
    macro = _cfg().get("macro", {})
    return {
        "start_date": macro.get("start_date", ""),
        "end_date": macro.get("end_date", ""),
    }


@app.post("/api/macro/config")
def api_macro_config_set(payload: dict):
    """更新宏观采集默认区间；保留 YYYY-MM / YYYY-MM-DD 两种输入。"""
    cfg = _cfg()
    macro = cfg.setdefault("macro", {})
    updated = False
    for key in ("start_date", "end_date"):
        if key not in payload:
            continue
        val = str(payload[key] or "").strip()
        if val:
            try:
                datetime.strptime(val[:7], "%Y-%m")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"{key} 格式应为 YYYY-MM 或 YYYY-MM-DD") from exc
        macro[key] = val
        updated = True
    if not updated:
        raise HTTPException(status_code=400, detail="无有效配置项")
    cfg["macro"] = macro
    _save_config(cfg)
    return {
        "ok": True,
        "config": {
            "start_date": macro.get("start_date", ""),
            "end_date": macro.get("end_date", ""),
        },
    }


@app.get("/api/etf/config")
def api_etf_config_get():
    """返回当前 ETF 标的配置（代码、名称、上市日期、区间）。"""
    return _etf_config_view()


@app.post("/api/etf/config")
def api_etf_config_set(payload: dict):
    """灵活配置 ETF：增删改代码清单、日期区间、默认交易日数；SQLite 为权威源。"""
    cfg = _cfg()
    db_path = _db_path(cfg)
    etf = storage.get_etf_config(db_path, cfg.get("etf", {})) if db_path else cfg.setdefault("etf", {})

    updated = False
    if "codes" in payload:
        raw = payload["codes"]
        if isinstance(raw, str):
            raw = [c.strip() for c in raw.split(",") if c.strip()]
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="codes 应为数组或逗号分隔字符串")
        codes: list[str] = []
        for c in raw:
            s = str(c).strip()
            if not s:
                continue
            if not s.isdigit() or len(s) > 6:
                raise HTTPException(status_code=400, detail=f"无效的基金代码：{c}")
            s = s.zfill(6)
            if s not in codes:
                codes.append(s)
        if len(codes) > 50:
            raise HTTPException(status_code=400, detail="ETF 标的数量上限 50 只")
        etf["codes"] = codes
        listing = etf.get("listing_dates") or {}
        etf["listing_dates"] = {k: v for k, v in listing.items() if k in codes}
        tags = etf.get("tags") or {}
        etf["tags"] = {k: v for k, v in tags.items() if k in codes}
        updated = True

    for key in ("start_date", "end_date"):
        if key in payload:
            val = str(payload[key] or "").strip()
            if val:
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"{key} 格式应为 YYYY-MM-DD")
            etf[key] = val
            updated = True

    if "default_trade_days" in payload:
        try:
            n = int(payload["default_trade_days"])
            if not (1 <= n <= 500):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="default_trade_days 应为 1~500 的整数")
        etf["default_trade_days"] = n
        updated = True

    if "tags" in payload:
        raw = payload["tags"]
        if isinstance(raw, list):
            raw = {
                str(item.get("code", "")).strip().zfill(6): item.get("tags", [])
                for item in raw if isinstance(item, dict)
            }
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="tags 应为 {代码: [标签]} 或 [{code, tags}]")
        allowed = set(ETF_TAG_OPTIONS)
        cleaned: dict[str, list[str]] = {}
        for key, val in raw.items():
            code = str(key).strip().zfill(6)
            if code not in etf.get("codes", []):
                raise HTTPException(status_code=400, detail=f"标签对应的 ETF 未配置：{code}")
            tags = val if isinstance(val, list) else str(val).split(",")
            tags = [str(t).strip() for t in tags if str(t).strip()]
            bad = [t for t in tags if t not in allowed]
            if bad:
                raise HTTPException(
                    status_code=400,
                    detail=f"ETF {code} 标签无效：{'、'.join(bad)}；可选 {'/'.join(ETF_TAG_OPTIONS)}",
                )
            ordered = [t for t in ETF_TAG_OPTIONS if t in tags]
            if ordered:
                cleaned[code] = ordered
        etf["tags"] = cleaned
        updated = True

    if not updated:
        raise HTTPException(status_code=400, detail="无有效配置项")

    if db_path:
        storage.save_etf_config(db_path, etf)
        cfg["etf"] = etf
    _save_config(cfg)  # config.json 仅作镜像，便于回滚
    logger.info("ETF 配置已更新：%d 只标的", len(etf.get("codes", [])))
    return {"ok": True, "config": _etf_config_view()}


@app.get("/api/index/config")
def api_index_config_get():
    """返回当前指数标的配置（代码、名称、区间、回看天数）。"""
    return _index_config_view()


@app.post("/api/index/config")
def api_index_config_set(payload: dict):
    """灵活配置指数标的：增删改代码清单、日期区间、回看天数；SQLite 为权威源。"""
    cfg = _cfg()
    db_path = _db_path(cfg)
    idx = storage.get_index_config(db_path, cfg.get("index", {})) if db_path else cfg.setdefault("index", {})

    known = {it["code"] for it in index_catalog()}
    updated = False
    if "codes" in payload:
        raw = payload["codes"]
        if isinstance(raw, str):
            raw = [c.strip() for c in raw.split(",") if c.strip()]
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="codes 应为数组或逗号分隔字符串")
        codes: list[str] = []
        for c in raw:
            s = str(c).strip()
            if not s.isdigit() or len(s) > 6:
                raise HTTPException(status_code=400, detail=f"无效的指数代码：{c}")
            s = s.zfill(6)
            if s not in known:
                raise HTTPException(status_code=400, detail=f"不支持的指数代码：{s}")
            if s not in codes:
                codes.append(s)
        if len(codes) > 20:
            raise HTTPException(status_code=400, detail="指数标的数量上限 20 个")
        idx["codes"] = codes
        updated = True

    for key in ("start_date", "end_date"):
        if key in payload:
            val = str(payload[key] or "").strip()
            if val:
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"{key} 格式应为 YYYY-MM-DD")
            idx[key] = val
            updated = True

    if "lookback_days" in payload:
        try:
            n = int(payload["lookback_days"])
            if not (1 <= n <= 3650):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="lookback_days 应为 1~3650 的整数")
        idx["lookback_days"] = n
        updated = True

    if not updated:
        raise HTTPException(status_code=400, detail="无有效配置项")

    if db_path:
        storage.save_index_config(db_path, idx)
        cfg["index"] = idx
    _save_config(cfg)  # config.json 仅作镜像，便于回滚
    logger.info("指数配置已更新：%d 个标的", len(idx.get("codes", [])))
    return {"ok": True, "config": _index_config_view()}


@app.post("/api/collect/interface")
def api_collect_interface(payload: dict):
    """按类别 + 指标/基金/指数代码 + 时间范围精确采集。"""
    category = payload.get("category")
    if category == "trade":
        sources = payload.get("codes") or payload.get("sources") or []
        if isinstance(sources, str):
            sources = [s.strip() for s in sources.split(",") if s.strip()]
        sources = [s for s in sources if s in ("exchange", "margin", "snapshot")]
        start = (payload.get("start") or "").strip() or None
        end = (payload.get("end") or "").strip() or None
        job_id = _submit("interface", lambda: _do_collect_trade(sources or None, start, end))
        return {"job_id": job_id}
    if category not in ("macro", "etf", "index"):
        raise HTTPException(status_code=400, detail="category 应为 macro、etf、index 或 trade")
    raw_codes = payload.get("codes") or []
    if isinstance(raw_codes, str):
        raw_codes = [c.strip() for c in raw_codes.split(",") if c.strip()]
    codes = [str(c).strip() for c in raw_codes if str(c).strip()]
    start = (payload.get("start") or "").strip() or None
    end = (payload.get("end") or "").strip() or None
    job_id = _submit("interface", lambda: _do_collect_interface(category, codes, start, end))
    return {"job_id": job_id}


@app.post("/api/clean/preview")
def api_clean_preview(payload: dict | None = None):
    """预览按类别/细分口径/时间范围将删除的行数（只读，不修改数据）。"""
    payload = payload or {}
    category = payload.get("category")
    if category not in storage.CLEAN_TARGETS:
        raise HTTPException(status_code=400, detail="category 应为 macro、etf 或 index")
    code = str(payload.get("code") or "").strip() or None
    start = str(payload.get("start") or "").strip() or None
    end = str(payload.get("end") or "").strip() or None
    db = _db_path(_cfg())
    if not db:
        raise HTTPException(status_code=400, detail="未配置 SQLite 主存储，无法清理")
    try:
        return storage.preview_clean(db, category, code, start, end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/clean")
def api_clean(payload: dict | None = None):
    """执行清理：必须显式 confirm=true；不触发采集，不影响配置与监控数据。"""
    payload = payload or {}
    if payload.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="缺少 confirm=true，拒绝清理")
    category = payload.get("category")
    if category not in storage.CLEAN_TARGETS:
        raise HTTPException(status_code=400, detail="category 应为 macro、etf 或 index")
    code = str(payload.get("code") or "").strip() or None
    start = str(payload.get("start") or "").strip() or None
    end = str(payload.get("end") or "").strip() or None
    db = _db_path(_cfg())
    if not db:
        raise HTTPException(status_code=400, detail="未配置 SQLite 主存储，无法清理")
    try:
        result = storage.clean_data(db, category, code, start, end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.warning("Web 触发数据清理：%s", result)
    return result


@app.post("/api/monitor/clean/preview")
def api_monitor_clean_preview(payload: dict | None = None):
    """预览按口径将删除的接口监控记录数（只读）。"""
    payload = payload or {}
    db = _db_path(_cfg())
    if not db:
        raise HTTPException(status_code=400, detail="未配置 SQLite 主存储，无法清理")
    return storage.preview_monitor_clean(
        db,
        start=(str(payload.get("start") or "").strip() or None),
        end=(str(payload.get("end") or "").strip() or None),
        status=(str(payload.get("status") or "").strip() or None),
        run_type=(str(payload.get("run_type") or "").strip() or None),
        category=(str(payload.get("category") or "").strip() or None),
        keyword=(str(payload.get("keyword") or "").strip() or None),
    )


@app.post("/api/monitor/clean")
def api_monitor_clean(payload: dict | None = None):
    """执行接口监控清理：必须 confirm=true；仅删 interface_run。"""
    payload = payload or {}
    if payload.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="缺少 confirm=true，拒绝清理")
    db = _db_path(_cfg())
    if not db:
        raise HTTPException(status_code=400, detail="未配置 SQLite 主存储，无法清理")
    result = storage.clean_monitor_data(
        db,
        start=(str(payload.get("start") or "").strip() or None),
        end=(str(payload.get("end") or "").strip() or None),
        status=(str(payload.get("status") or "").strip() or None),
        run_type=(str(payload.get("run_type") or "").strip() or None),
        category=(str(payload.get("category") or "").strip() or None),
        keyword=(str(payload.get("keyword") or "").strip() or None),
    )
    logger.warning("Web 触发接口监控清理：%s", result)
    return result


@app.post("/api/retry")
def api_retry():
    job_id = _submit("retry", _do_retry)
    return {"job_id": job_id}


@app.post("/api/retry/task")
def api_retry_task(payload: dict):
    """监控失败明细：按任务ID重跑单条失败任务。"""
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        raise HTTPException(status_code=400, detail="缺少 task_id")
    job_id = _submit("retry", lambda: _do_retry_task(task_id))
    return {"job_id": job_id}


@app.get("/api/scheduler")
def api_scheduler_get():
    cfg = _cfg().get("scheduler", {})
    return {
        "enabled": bool(cfg.get("enabled")),
        "time": cfg.get("time", "08:30"),
        "run_type": cfg.get("run_type", "full"),
        "next_run": scheduler.next_run(),
    }


@app.post("/api/scheduler")
def api_scheduler_set(payload: dict):
    allowed = {"enabled", "time", "run_type"}
    if not any(k in payload for k in allowed):
        raise HTTPException(status_code=400, detail="无有效配置项")
    cfg = _cfg()
    sched = cfg.setdefault("scheduler", {})
    if "enabled" in payload:
        sched["enabled"] = bool(payload["enabled"])
    if "time" in payload:
        try:
            datetime.strptime(payload["time"], "%H:%M")
        except ValueError:
            raise HTTPException(status_code=400, detail="时间格式应为 HH:MM")
        sched["time"] = payload["time"]
    if "run_type" in payload:
        if payload["run_type"] not in ("full", "macro", "etf", "index"):
            raise HTTPException(status_code=400, detail="run_type 应为 full/macro/etf/index")
        sched["run_type"] = payload["run_type"]
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "config": api_scheduler_get()}


@app.get("/api/health")
def api_health():
    return {"status": "ok", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- 静态文件 ----------------
class _NoCacheStaticFiles(StaticFiles):
    """静态资源禁用强缓存：配合 index 的 mtime 版本号，保证每次发版即时生效。"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/static", _NoCacheStaticFiles(directory=BASE_DIR / "static"), name="static")


def main():
    parser = argparse.ArgumentParser(description="投研大盘数据采集系统 Web 看板")
    parser.add_argument("--host", default=None, help="监听地址（覆盖配置）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（覆盖配置）")
    args = parser.parse_args()
    cfg = _cfg().get("web", {})
    host = args.host or cfg.get("host", "0.0.0.0")
    env_port = os.environ.get("PORT")
    port = args.port or (int(env_port) if env_port else cfg.get("port", 8000))

    # 服务启动 / 重启为纯只读动作：不清空数据，也不触发任何采集。
    # 数据仅在看板手动采集或定时调度到点时更新。
    scheduler.start()
    logger.info("投研大盘数据采集系统启动：http://127.0.0.1:%d", port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
