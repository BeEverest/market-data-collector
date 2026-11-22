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

from main import BASE_DIR as MAIN_BASE, _collect, _finish, _latest_csv, _merge_patch, load_config
from monitor import Monitor

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
    return df.head(limit).where(pd.notna(df.head(limit)), None).to_dict("records")


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


@app.get("/api/data/macro")
def api_data_macro(code: str | None = None, start: str | None = None,
                   end: str | None = None, limit: int = 5000):
    df = _read_csv("macro")
    if code and not df.empty:
        df = df[df["指标代码"] == code]
    if start and not df.empty:
        df = df[df["日期"] >= start]
    if end and not df.empty:
        df = df[df["日期"] <= end]
    return {"total": len(df), "rows": _frame_to_records(df, limit)}


@app.get("/api/data/etf")
def api_data_etf(code: str | None = None, start: str | None = None,
                 end: str | None = None, limit: int = 5000):
    df = _read_csv("etf")
    if code and not df.empty:
        df = df[df["基金代码"] == code]
    if start and not df.empty:
        df = df[df["交易日期"] >= start]
    if end and not df.empty:
        df = df[df["交易日期"] <= end]
    return {"total": len(df), "rows": _frame_to_records(df, limit)}


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
    port = args.port or cfg.get("port", 8000)
    scheduler.start()
    logger.info("投研大盘数据采集系统启动：http://127.0.0.1:%d", port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
