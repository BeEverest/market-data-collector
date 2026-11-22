#!/usr/bin/env python3
"""投研大盘数据采集系统 - Web 看板服务。

启动：
    python web_app.py                # 按 config.json 的 web.host/port 启动
    python web_app.py --port 9000    # 覆盖端口

浏览器访问 http://127.0.0.1:8000 。所有采集/重跑在后台线程执行，前端轮询任务状态。
历史数据仍沿用 CSV/Excel/JSONL 文件，不引入数据库。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(BASE_DIR))

from main import (
    BASE_DIR as MAIN_BASE, _collect, _finish, _latest_csv, _merge_patch,
    collect_interface, load_config,
)
from monitor import Monitor
from collectors.macro import catalog as macro_catalog
from collectors.etf import get_fund_name_map

app = FastAPI(title="投研大盘数据采集系统", version="1.0")

# ---------------- 全局状态 ----------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()          # 采集/重跑互斥，避免并发写同一批文件
logger = logging.getLogger("market_data.web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")

CONFIG_PATH = BASE_DIR / "config.json"


def _cfg() -> dict:
    return load_config(str(CONFIG_PATH))


def _history_path(config: dict) -> Path:
    return BASE_DIR / config.get("monitor", {}).get(
        "history_file", "output/monitor/interface_runs.jsonl"
    )


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _etf_config_view() -> dict:
    cfg = _cfg()
    etf = cfg.get("etf", {})
    codes = [str(c).strip().zfill(6) for c in etf.get("codes", []) if str(c).strip()]
    name_map = get_fund_name_map()
    listing = {str(k).zfill(6): v for k, v in (etf.get("listing_dates") or {}).items()}
    items = [{
        "code": c,
        "name": name_map.get(c, ""),
        "listing_date": listing.get(c, ""),
    } for c in codes]
    return {
        "codes": codes,
        "items": items,
        "start_date": etf.get("start_date", ""),
        "end_date": etf.get("end_date", ""),
        "default_trade_days": etf.get("default_trade_days", 20),
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


def _do_collect(macro_only: bool = False, etf_only: bool = False) -> str:
    config = _cfg()
    monitor = Monitor(MAIN_BASE, run_type="full", history_path=_history_path(config))
    logger.info("Web 触发采集：%s", "仅宏观" if macro_only else ("仅 ETF" if etf_only else "全量"))
    macro_df, etf_df = _collect(config, monitor, macro_only=macro_only, etf_only=etf_only)
    # 单项采集时，另一项沿用上次结果，避免空 DataFrame 覆盖掉已有数据文件
    if macro_only and etf_df.empty:
        prev = _latest_csv("etf", config)
        if prev:
            etf_df = pd.read_csv(prev, dtype={"基金代码": str})
            logger.info("仅宏观采集：ETF 沿用上次结果 %d 行", len(etf_df))
    if etf_only and macro_df.empty:
        prev = _latest_csv("macro", config)
        if prev:
            macro_df = pd.read_csv(prev)
            logger.info("仅 ETF 采集：宏观沿用上次结果 %d 行", len(macro_df))
    _finish(monitor, macro_df, etf_df, config, monitor.run_id)
    failed = sum(1 for r in monitor.records if r["status"] == "failed")
    logger.info("采集完成：宏观 %d 行 / ETF %d 行，接口失败 %d 次",
                len(macro_df), len(etf_df), failed)
    return f"宏观 {len(macro_df)} 行 / ETF {len(etf_df)} 行，接口失败 {failed} 次"


def _do_retry() -> str:
    from main import _retry_failed
    config = _cfg()
    logger.info("Web 触发失败接口重跑")
    code = _retry_failed(config, config.get("monitor", {}))
    return "重跑完成" if code == 0 else "重跑后仍有失败接口"


def _do_collect_interface(category: str, codes: list[str],
                          start: str | None, end: str | None) -> str:
    config = _cfg()
    return collect_interface(config, category, codes, start, end)


# ---------------- 数据读取 ----------------
def _read_csv(kind: str) -> pd.DataFrame:
    path = _latest_csv(kind, _cfg())
    if not path:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype={"基金代码": str})
    except Exception as exc:
        logger.warning("读取 CSV 失败 %s: %s", path, exc)
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
                if not cfg.get("enabled"):
                    continue
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                if now.strftime("%H:%M") != cfg.get("time", "08:30"):
                    continue
                if self._last_triggered == today:
                    continue
                if _run_lock.locked():
                    continue
                self._last_triggered = today
                run_type = cfg.get("run_type", "full")
                logger.info("定时调度触发：%s %s", today, run_type)
                _submit("scheduled", lambda rt=run_type: _do_collect(
                    macro_only=(rt == "macro"), etf_only=(rt == "etf")
                ))
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
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/status")
def api_status():
    history = Monitor.load(_history_path(_cfg()))
    cfg = _cfg()
    last_run = {}
    failed_count = 0
    today = datetime.now().strftime("%Y-%m-%d")
    today_calls = 0
    if not history.empty:
        last = history.sort_values("run_time").iloc[-1]
        last_run = {
            "id": last["run_id"], "date": last["run_date"], "time": last["run_time"],
            "type": last["run_type"],
        }
        failed_count = int((history["status"] == "failed").sum())
        today_calls = int((history["run_date"] == today).sum())
    macro = _read_csv("macro")
    etf = _read_csv("etf")
    return {
        "last_run": last_run,
        "failed_total": failed_count,
        "today_calls": today_calls,
        "macro_rows": len(macro),
        "macro_indicators": int(macro["指标代码"].nunique()) if not macro.empty else 0,
        "etf_rows": len(etf),
        "etf_codes": int(etf["基金代码"].nunique()) if not etf.empty else 0,
        "scheduler": {
            "enabled": bool(cfg.get("scheduler", {}).get("enabled")),
            "time": cfg.get("scheduler", {}).get("time", "08:30"),
            "run_type": cfg.get("scheduler", {}).get("run_type", "full"),
            "next_run": scheduler.next_run(),
        },
        "running": _run_lock.locked(),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/monitor/report")
def api_monitor_report():
    monitor = Monitor(MAIN_BASE, history_path=_history_path(_cfg()))
    report = monitor.build_report()
    return {sheet: _frame_to_records(df) for sheet, df in report.items()}


@app.get("/api/monitor/failed")
def api_monitor_failed():
    monitor = Monitor(MAIN_BASE, history_path=_history_path(_cfg()))
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
                   end: str | None = None, page: int = 1, page_size: int = 25):
    # 宏观数据按月存储，兼容前端传入的 YYYY-MM-DD
    start = start[:7] if start else None
    end = end[:7] if end else None
    df = _read_csv("macro")
    if code and not df.empty:
        df = df[df["指标代码"] == code]
    if start and not df.empty:
        df = df[df["日期"] >= start]
    if end and not df.empty:
        df = df[df["日期"] <= end]
    return _paged(df, page, page_size)


@app.get("/api/data/etf")
def api_data_etf(code: str | None = None, start: str | None = None,
                 end: str | None = None, page: int = 1, page_size: int = 25):
    df = _read_csv("etf")
    if code and not df.empty:
        df = df[df["基金代码"] == code]
    if start and not df.empty:
        df = df[df["交易日期"] >= start]
    if end and not df.empty:
        df = df[df["交易日期"] <= end]
    return _paged(df, page, page_size)


@app.get("/api/files")
def api_files():
    out_dir = BASE_DIR / _cfg().get("output", {}).get("dir", "output")
    files = []
    for path in sorted(out_dir.rglob("*")):
        if path.is_file():
            files.append({
                "name": str(path.relative_to(out_dir)),
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


@app.post("/api/collect")
def api_collect(payload: dict | None = None):
    payload = payload or {}
    macro_only = bool(payload.get("macro_only", False))
    etf_only = bool(payload.get("etf_only", False))
    job_id = _submit("collect", lambda: _do_collect(macro_only, etf_only))
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

    etf_codes = [str(c).strip().zfill(6) for c in cfg.get("etf", {}).get("codes", [])]
    name_map = get_fund_name_map()
    listing = {str(k).zfill(6): v for k, v in (cfg.get("etf", {}).get("listing_dates") or {}).items()}
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

    return {
        "macro": macro_items,
        "etf": etf_items,
        "defaults": {
            "macro_start": cfg.get("macro", {}).get("start_date", ""),
            "etf_start": cfg.get("etf", {}).get("start_date", ""),
            "etf_end": cfg.get("etf", {}).get("end_date", ""),
        },
        "keep_snapshots": cfg.get("output", {}).get("keep_snapshots", 3),
    }


@app.get("/api/etf/config")
def api_etf_config_get():
    """返回当前 ETF 标的配置（代码、名称、上市日期、区间）。"""
    return _etf_config_view()


@app.post("/api/etf/config")
def api_etf_config_set(payload: dict):
    """灵活配置 ETF：增删改代码清单、日期区间、默认交易日数。"""
    cfg = _cfg()
    etf = cfg.setdefault("etf", {})

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
        # 清理已移除代码的上市日期缓存
        listing = etf.get("listing_dates") or {}
        etf["listing_dates"] = {k: v for k, v in listing.items() if k in codes}
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

    if not updated:
        raise HTTPException(status_code=400, detail="无有效配置项")

    _save_config(cfg)
    logger.info("ETF 配置已更新：%d 只标的", len(etf.get("codes", [])))
    return {"ok": True, "config": _etf_config_view()}


@app.post("/api/collect/interface")
def api_collect_interface(payload: dict):
    """按类别 + 指标/基金代码 + 时间范围精确采集。"""
    category = payload.get("category")
    if category not in ("macro", "etf"):
        raise HTTPException(status_code=400, detail="category 应为 macro 或 etf")
    raw_codes = payload.get("codes") or []
    if isinstance(raw_codes, str):
        raw_codes = [c.strip() for c in raw_codes.split(",") if c.strip()]
    codes = [str(c).strip() for c in raw_codes if str(c).strip()]
    start = (payload.get("start") or "").strip() or None
    end = (payload.get("end") or "").strip() or None
    job_id = _submit("interface", lambda: _do_collect_interface(category, codes, start, end))
    return {"job_id": job_id}


@app.post("/api/retry")
def api_retry():
    job_id = _submit("retry", _do_retry)
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
        if payload["run_type"] not in ("full", "macro", "etf"):
            raise HTTPException(status_code=400, detail="run_type 应为 full/macro/etf")
        sched["run_type"] = payload["run_type"]
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "config": api_scheduler_get()}


@app.get("/api/health")
def api_health():
    return {"status": "ok", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- 静态文件 ----------------
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def main():
    parser = argparse.ArgumentParser(description="投研大盘数据采集系统 Web 看板")
    parser.add_argument("--host", default=None, help="监听地址（覆盖配置）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（覆盖配置）")
    args = parser.parse_args()
    cfg = _cfg().get("web", {})
    host = args.host or cfg.get("host", "0.0.0.0")
    env_port = os.environ.get("PORT")
    port = args.port or (int(env_port) if env_port else cfg.get("port", 8000))
    scheduler.start()
    logger.info("投研大盘数据采集系统启动：http://127.0.0.1:%d", port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
