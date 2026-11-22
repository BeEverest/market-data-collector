"""SQLite 主存储层：业务数据、接口监控、ETF 配置与元数据。

设计约定：
- 宏观业务主键：{指标代码}_{YYYYMM}，例如 M2_202610
- ETF 业务主键：{6位基金代码}_{YYYYMMDD}，例如 510050_20261020
- 监控业务主键：{run_id}_{接口键}_{运行时间}_{序号}
- 所有写入使用 ON CONFLICT(biz_key) DO UPDATE，保证重跑/精确采集不产生重复行。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
DEFAULT_DB_PATH = "output/market_data.db"

MACRO_COLUMNS = [
    "指标代码", "指标名称", "日期", "数值", "单位",
    "同比(%)", "环比(%)", "数据源", "采集时间",
]
ETF_COLUMNS = [
    "基金代码", "基金简称", "交易市场", "交易日期",
    "份额(亿份)", "单位净值(元)", "规模(亿元)", "数据源", "采集时间",
]
INDEX_COLUMNS = [
    "指数代码", "指数名称", "交易日期",
    "开盘价", "最高价", "最低价", "收盘价", "成交量", "涨跌幅(%)",
    "数据源", "采集时间",
]
RUN_FIELDS = [
    "run_id", "run_type", "run_date", "run_time",
    "category", "interface_key", "interface_name",
    "params", "status", "attempts", "rows", "error", "duration_sec", "biz_key",
]


def db_path(config: dict, base_dir: Path) -> Path:
    raw = config.get("storage", {}).get("db_path", DEFAULT_DB_PATH)
    path = Path(raw)
    return path if path.is_absolute() else Path(base_dir) / path


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(path: str | Path) -> None:
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS macro_observation (
                biz_key TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                date TEXT NOT NULL,
                value REAL,
                unit TEXT,
                yoy_pct REAL,
                mom_pct REAL,
                source TEXT,
                collected_at TEXT,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_macro_code_date
                ON macro_observation(code, date DESC);

            CREATE TABLE IF NOT EXISTS etf_observation (
                biz_key TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT,
                market TEXT,
                trade_date TEXT NOT NULL,
                shares REAL,
                nav REAL,
                market_cap REAL,
                source TEXT,
                collected_at TEXT,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_etf_code_date
                ON etf_observation(code, trade_date DESC);

            CREATE TABLE IF NOT EXISTS index_observation (
                biz_key TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                pct_change REAL,
                source TEXT,
                collected_at TEXT,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_index_code_date
                ON index_observation(code, trade_date DESC);

            CREATE TABLE IF NOT EXISTS interface_run (
                biz_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                run_type TEXT,
                run_date TEXT,
                run_time TEXT,
                category TEXT,
                interface_key TEXT,
                interface_name TEXT,
                params TEXT,
                status TEXT,
                attempts INTEGER,
                rows INTEGER,
                error TEXT,
                duration_sec REAL
            );
            CREATE INDEX IF NOT EXISTS idx_run_date ON interface_run(run_date DESC);
            CREATE INDEX IF NOT EXISTS idx_run_interface ON interface_run(interface_key, run_time);
            CREATE INDEX IF NOT EXISTS idx_run_status ON interface_run(status);

            CREATE TABLE IF NOT EXISTS etf_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS index_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS export_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                export_ts TEXT NOT NULL,
                scope TEXT NOT NULL,
                file_path TEXT NOT NULL,
                row_counts TEXT
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )


def set_meta(path: str | Path, key: str, value: str) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_meta(path: str | Path, key: str, default: str = "") -> str:
    with connect(path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# ---------- 业务主键 ----------
def macro_biz_key(code: str, date: str) -> str:
    return f"{code}_{str(date)[:7].replace('-', '')}"


def etf_biz_key(code: str, trade_date: str) -> str:
    return f"{str(code).zfill(6)}_{str(trade_date).replace('-', '')}"


def index_biz_key(code: str, trade_date: str) -> str:
    return f"{str(code).zfill(6)}_{str(trade_date).replace('-', '')}"


def run_biz_key(run_id: str, interface_key: str, run_time: str, seq: int) -> str:
    stamp = str(run_time).replace("-", "").replace(":", "").replace(" ", "_")
    return f"{run_id}_{interface_key}_{stamp}_{seq:03d}"


def _clean(value):
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if pd.isna(value):
        return None
    return value


# ---------- 写入 ----------
def upsert_macro(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        code = str(r.get("指标代码", "")).strip()
        date = str(r.get("日期", ""))[:7]
        if not code or not date:
            continue
        records.append((
            macro_biz_key(code, date), code, str(r.get("指标名称", "")), date,
            _clean(r.get("数值")), str(r.get("单位", "") or ""),
            _clean(r.get("同比(%)")), _clean(r.get("环比(%)")),
            str(r.get("数据源", "") or ""), str(r.get("采集时间", "") or ""), run_id,
        ))
    with connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO macro_observation(
                biz_key, code, name, date, value, unit, yoy_pct, mom_pct,
                source, collected_at, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                name=excluded.name, value=excluded.value, unit=excluded.unit,
                yoy_pct=excluded.yoy_pct, mom_pct=excluded.mom_pct,
                source=excluded.source, collected_at=excluded.collected_at,
                run_id=excluded.run_id
            """,
            records,
        )
    return len(records)


def upsert_etf(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        code = str(r.get("基金代码", "")).strip().zfill(6)
        date = str(r.get("交易日期", ""))[:10]
        if not code or not date:
            continue
        records.append((
            etf_biz_key(code, date), code, str(r.get("基金简称", "") or ""),
            str(r.get("交易市场", "") or ""), date,
            _clean(r.get("份额(亿份)")), _clean(r.get("单位净值(元)")),
            _clean(r.get("规模(亿元)")), str(r.get("数据源", "") or ""),
            str(r.get("采集时间", "") or ""), run_id,
        ))
    with connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO etf_observation(
                biz_key, code, name, market, trade_date, shares, nav, market_cap,
                source, collected_at, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                name=excluded.name, market=excluded.market,
                shares=excluded.shares, nav=excluded.nav, market_cap=excluded.market_cap,
                source=excluded.source, collected_at=excluded.collected_at,
                run_id=excluded.run_id
            """,
            records,
        )
    return len(records)


def upsert_index(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        code = str(r.get("指数代码", "")).strip().zfill(6)
        date = str(r.get("交易日期", ""))[:10]
        if not code or not date:
            continue
        records.append((
            index_biz_key(code, date), code, str(r.get("指数名称", "") or ""), date,
            _clean(r.get("开盘价")), _clean(r.get("最高价")), _clean(r.get("最低价")),
            _clean(r.get("收盘价")), _clean(r.get("成交量")), _clean(r.get("涨跌幅(%)")),
            str(r.get("数据源", "") or ""), str(r.get("采集时间", "") or ""), run_id,
        ))
    with connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO index_observation(
                biz_key, code, name, trade_date, open, high, low, close, volume,
                pct_change, source, collected_at, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                name=excluded.name, open=excluded.open, high=excluded.high,
                low=excluded.low, close=excluded.close, volume=excluded.volume,
                pct_change=excluded.pct_change, source=excluded.source,
                collected_at=excluded.collected_at, run_id=excluded.run_id
            """,
            records,
        )
    return len(records)


def append_interface_runs(path: str | Path, records: list[dict]) -> int:
    if not records:
        return 0
    init_schema(path)
    seq_map: dict[tuple[str, str, str], int] = {}
    rows = []
    for rec in records:
        run_id = str(rec.get("run_id", ""))
        interface_key = str(rec.get("interface_key", ""))
        run_time = str(rec.get("run_time", ""))
        seq_key = (run_id, interface_key, run_time)
        seq = seq_map.get(seq_key, 0) + 1
        seq_map[seq_key] = seq
        attempts = rec.get("attempts", 0)
        row_count = rec.get("rows", 0)
        duration = rec.get("duration_sec", 0)
        attempts = 0 if pd.isna(attempts) else int(attempts)
        row_count = 0 if pd.isna(row_count) else int(row_count)
        duration = 0.0 if pd.isna(duration) else float(duration)
        rows.append((
            rec.get("biz_key") or run_biz_key(run_id, interface_key, run_time, seq),
            run_id, str(rec.get("run_type", "")), str(rec.get("run_date", "")),
            run_time, str(rec.get("category", "")), interface_key,
            str(rec.get("interface_name", "")), str(rec.get("params", "") or "{}"),
            str(rec.get("status", "")), attempts, row_count,
            str(rec.get("error", "") or ""), duration,
        ))
    with connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO interface_run(
                biz_key, run_id, run_type, run_date, run_time, category,
                interface_key, interface_name, params, status, attempts, rows,
                error, duration_sec
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                status=excluded.status, attempts=excluded.attempts, rows=excluded.rows,
                error=excluded.error, duration_sec=excluded.duration_sec
            """,
            rows,
        )
    return len(rows)


# ---------- 读取 ----------
def read_macro(path: str | Path, code: str | None = None,
               start: str | None = None, end: str | None = None,
               limit: int | None = None, offset: int = 0) -> tuple[int, pd.DataFrame]:
    init_schema(path)
    clauses, args = [], []
    if code:
        clauses.append("code=?")
        args.append(str(code).strip())
    if start:
        clauses.append("date>=?")
        args.append(str(start)[:7])
    if end:
        clauses.append("date<=?")
        args.append(str(end)[:7])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect(path) as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM macro_observation{where}", args).fetchone()["n"]
        sql = f"""
            SELECT code, name, date, value, unit, yoy_pct, mom_pct, source, collected_at
            FROM macro_observation{where}
            ORDER BY code ASC, date DESC
        """
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = args + [int(limit), int(offset)]
        rows = conn.execute(sql, args).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return int(total), pd.DataFrame(columns=MACRO_COLUMNS)
    df = df.rename(columns={
        "code": "指标代码", "name": "指标名称", "date": "日期", "value": "数值",
        "unit": "单位", "yoy_pct": "同比(%)", "mom_pct": "环比(%)",
        "source": "数据源", "collected_at": "采集时间",
    })
    return int(total), df[MACRO_COLUMNS]


def read_etf(path: str | Path, code: str | None = None,
             start: str | None = None, end: str | None = None,
             limit: int | None = None, offset: int = 0) -> tuple[int, pd.DataFrame]:
    init_schema(path)
    clauses, args = [], []
    if code:
        clauses.append("code=?")
        args.append(str(code).strip().zfill(6))
    if start:
        clauses.append("trade_date>=?")
        args.append(str(start)[:10])
    if end:
        clauses.append("trade_date<=?")
        args.append(str(end)[:10])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect(path) as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM etf_observation{where}", args).fetchone()["n"]
        sql = f"""
            SELECT code, name, market, trade_date, shares, nav, market_cap, source, collected_at
            FROM etf_observation{where}
            ORDER BY code ASC, trade_date DESC
        """
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = args + [int(limit), int(offset)]
        rows = conn.execute(sql, args).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return int(total), pd.DataFrame(columns=ETF_COLUMNS)
    df = df.rename(columns={
        "code": "基金代码", "name": "基金简称", "market": "交易市场",
        "trade_date": "交易日期", "shares": "份额(亿份)", "nav": "单位净值(元)",
        "market_cap": "规模(亿元)", "source": "数据源", "collected_at": "采集时间",
    })
    return int(total), df[ETF_COLUMNS]


def read_index(path: str | Path, code: str | None = None,
               start: str | None = None, end: str | None = None,
               limit: int | None = None, offset: int = 0) -> tuple[int, pd.DataFrame]:
    init_schema(path)
    clauses, args = [], []
    if code:
        clauses.append("code=?")
        args.append(str(code).strip().zfill(6))
    if start:
        clauses.append("trade_date>=?")
        args.append(str(start)[:10])
    if end:
        clauses.append("trade_date<=?")
        args.append(str(end)[:10])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect(path) as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM index_observation{where}", args).fetchone()["n"]
        sql = f"""
            SELECT code, name, trade_date, open, high, low, close, volume, pct_change,
                   source, collected_at
            FROM index_observation{where}
            ORDER BY code ASC, trade_date DESC
        """
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = args + [int(limit), int(offset)]
        rows = conn.execute(sql, args).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return int(total), pd.DataFrame(columns=INDEX_COLUMNS)
    df = df.rename(columns={
        "code": "指数代码", "name": "指数名称", "trade_date": "交易日期",
        "open": "开盘价", "high": "最高价", "low": "最低价", "close": "收盘价",
        "volume": "成交量", "pct_change": "涨跌幅(%)",
        "source": "数据源", "collected_at": "采集时间",
    })
    return int(total), df[INDEX_COLUMNS]


def read_interface_runs(path: str | Path) -> pd.DataFrame:
    init_schema(path)
    with connect(path) as conn:
        rows = conn.execute(
            """SELECT run_id, run_type, run_date, run_time, category, interface_key,
                      interface_name, params, status, attempts, rows, error, duration_sec
               FROM interface_run ORDER BY run_time ASC"""
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows], columns=[c for c in RUN_FIELDS if c != "biz_key"])


def macro_stats(path: str | Path) -> dict[str, dict]:
    init_schema(path)
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT code, COUNT(*) AS n, MIN(date) AS first_date, MAX(date) AS last_date "
            "FROM macro_observation GROUP BY code"
        ).fetchall()
    return {r["code"]: {
        "rows": int(r["n"]), "first_date": r["first_date"], "last_date": r["last_date"]
    } for r in rows}


def etf_stats(path: str | Path) -> dict[str, dict]:
    init_schema(path)
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT code, COUNT(*) AS n, MIN(trade_date) AS first_date, "
            "MAX(trade_date) AS last_date FROM etf_observation GROUP BY code"
        ).fetchall()
    return {r["code"]: {
        "rows": int(r["n"]), "first_date": r["first_date"], "last_date": r["last_date"]
    } for r in rows}


def index_stats(path: str | Path) -> dict[str, dict]:
    init_schema(path)
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT code, COUNT(*) AS n, MIN(trade_date) AS first_date, "
            "MAX(trade_date) AS last_date FROM index_observation GROUP BY code"
        ).fetchall()
    return {r["code"]: {
        "rows": int(r["n"]), "first_date": r["first_date"], "last_date": r["last_date"]
    } for r in rows}


def status_counts(path: str | Path) -> dict:
    init_schema(path)
    today = datetime.now().strftime("%Y-%m-%d")
    with connect(path) as conn:
        macro = conn.execute(
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT code) AS codes FROM macro_observation"
        ).fetchone()
        etf = conn.execute(
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT code) AS codes FROM etf_observation"
        ).fetchone()
        index = conn.execute(
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT code) AS codes FROM index_observation"
        ).fetchone()
        failed = conn.execute(
            "SELECT COUNT(*) AS n FROM interface_run WHERE status='failed'"
        ).fetchone()["n"]
        today_calls = conn.execute(
            "SELECT COUNT(*) AS n FROM interface_run WHERE run_date=?", (today,)
        ).fetchone()["n"]
        last = conn.execute(
            "SELECT run_id, run_date, run_time, run_type FROM interface_run "
            "ORDER BY run_time DESC LIMIT 1"
        ).fetchone()
    return {
        "macro_rows": int(macro["rows"]), "macro_indicators": int(macro["codes"]),
        "etf_rows": int(etf["rows"]), "etf_codes": int(etf["codes"]),
        "index_rows": int(index["rows"]), "index_codes": int(index["codes"]),
        "failed_total": int(failed), "today_calls": int(today_calls),
        "last_run": dict(last) if last else {},
    }


# ---------- ETF 配置 ----------
DEFAULT_ETF_CONFIG = {
    "codes": [], "listing_dates": {}, "start_date": "",
    "end_date": "", "default_trade_days": 20,
}


def get_etf_config(path: str | Path, default: dict | None = None) -> dict:
    init_schema(path)
    with connect(path) as conn:
        row = conn.execute("SELECT value FROM etf_config WHERE key='main'").fetchone()
    if not row:
        return json.loads(json.dumps(default or DEFAULT_ETF_CONFIG))
    try:
        stored = json.loads(row["value"])
    except json.JSONDecodeError:
        stored = {}
    result = json.loads(json.dumps(default or DEFAULT_ETF_CONFIG))
    result.update(stored)
    return result


def save_etf_config(path: str | Path, cfg: dict) -> dict:
    init_schema(path)
    value = json.dumps(cfg, ensure_ascii=False)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO etf_config(key, value, updated_at) VALUES('main', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (value, now),
        )
    return cfg


# ---------- 指数配置 ----------
DEFAULT_INDEX_CONFIG = {
    "codes": [], "start_date": "", "end_date": "", "lookback_days": 365,
}


def get_index_config(path: str | Path, default: dict | None = None) -> dict:
    init_schema(path)
    with connect(path) as conn:
        row = conn.execute("SELECT value FROM index_config WHERE key='main'").fetchone()
    if not row:
        return json.loads(json.dumps(default or DEFAULT_INDEX_CONFIG))
    try:
        stored = json.loads(row["value"])
    except json.JSONDecodeError:
        stored = {}
    result = json.loads(json.dumps(default or DEFAULT_INDEX_CONFIG))
    result.update(stored)
    return result


def save_index_config(path: str | Path, cfg: dict) -> dict:
    init_schema(path)
    value = json.dumps(cfg, ensure_ascii=False)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO index_config(key, value, updated_at) VALUES('main', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (value, now),
        )
    return cfg


def log_export(path: str | Path, scope: str, file_path: str, row_counts: dict) -> None:
    init_schema(path)
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO export_log(export_ts, scope, file_path, row_counts) VALUES(?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), scope, file_path,
             json.dumps(row_counts, ensure_ascii=False)),
        )
