"""SQLite 主存储层：业务数据、接口监控、ETF 配置与元数据。

设计约定：
- 宏观业务主键：{指标代码}_{YYYYMM}，例如 M2_202610
- ETF 业务主键：{6位基金代码}_{YYYYMMDD}，例如 510050_20261020
- 监控业务主键：{run_id}_{接口键}_{运行时间}_{序号}
- 正常写入使用 ON CONFLICT(biz_key) DO UPDATE，保证重跑/精确采集不产生重复行；
  深交所失败兜底行例外，仅对已存在业务键 DO NOTHING，禁止覆盖或凭空新增采集原数据。
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
# 深交所接口断连时用于展示的兜底行标记：已存在业务键必须 DO NOTHING，
# 禁止把兜底行当作新采集写回覆盖原数据，也不计入采集量统计。
ETF_FALLBACK_MARK = "沿用已落库"
ETF_FALLBACK_SOURCE = "深交所接口失败-沿用已落库（不计新增）"
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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """为旧库补列；表名/列名均为本模块内部常量，不接受外部输入。"""
    cols = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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

            CREATE TABLE IF NOT EXISTS collection_stat (
                biz_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                run_type TEXT,
                run_date TEXT,
                category TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                rows INTEGER NOT NULL DEFAULT 0,
                collected_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cstat_run ON collection_stat(run_id, category);
            CREATE INDEX IF NOT EXISTS idx_cstat_date ON collection_stat(run_date DESC);

            CREATE TABLE IF NOT EXISTS derived_indicator (
                biz_key TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                date TEXT NOT NULL,
                value REAL,
                unit TEXT,
                formula TEXT,
                grp TEXT,
                source TEXT,
                computed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_derived_date ON derived_indicator(date DESC, code);
            CREATE INDEX IF NOT EXISTS idx_derived_code ON derived_indicator(code, date DESC);

            CREATE TABLE IF NOT EXISTS trade_exchange_raw (
                biz_key TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                sse_amount REAL,
                sse_volume REAL,
                sse_float_mv REAL,
                sse_total_mv REAL,
                szse_amount REAL,
                szse_float_mv REAL,
                szse_total_mv REAL,
                source TEXT,
                collected_at TEXT,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tex_date ON trade_exchange_raw(trade_date DESC);

            CREATE TABLE IF NOT EXISTS trade_margin_raw (
                biz_key TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                financing_balance REAL,
                securities_balance REAL,
                financing_buy REAL,
                securities_sell REAL,
                margin_ratio REAL,
                debt_investors INTEGER,
                collateral_value REAL,
                active_investors INTEGER,
                individual_investors INTEGER,
                institutional_investors INTEGER,
                broker_count INTEGER,
                branch_count INTEGER,
                source TEXT,
                collected_at TEXT,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tmargin_date ON trade_margin_raw(trade_date DESC);

            CREATE TABLE IF NOT EXISTS trade_snapshot_raw (
                biz_key TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                pct_change REAL,
                volume REAL,
                amount REAL,
                last_price REAL,
                source TEXT,
                collected_at TEXT,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tsnap_date ON trade_snapshot_raw(trade_date DESC);
            CREATE INDEX IF NOT EXISTS idx_tsnap_code_date ON trade_snapshot_raw(code, trade_date DESC);

            CREATE TABLE IF NOT EXISTS trade_breadth_raw (
                biz_key TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                up_count INTEGER,
                down_count INTEGER,
                flat_count INTEGER,
                source TEXT,
                collected_at TEXT,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tbreadth_date ON trade_breadth_raw(trade_date DESC);

            CREATE TABLE IF NOT EXISTS trade_congestion_raw (
                biz_key TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                close_price REAL,
                top5_amount_yi REAL,
                total_amount_yi REAL,
                concentration5 REAL,
                source TEXT,
                collected_at TEXT,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tcongestion_date ON trade_congestion_raw(trade_date DESC);

            -- 沪深300风险溢价率：韭圈儿/投研工作台 FED 差值口径，数据源已计算好指标。
            -- 采集层原样落库，不在本项目二次计算。
            CREATE TABLE IF NOT EXISTS trade_risk_premium_raw (
                biz_key TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                risk_premium REAL,
                ratio REAL,
                close REAL,
                ma70 REAL,
                avg REAL,
                upper1std REAL,
                lower1std REAL,
                upper2std REAL,
                lower2std REAL,
                source TEXT,
                collected_at TEXT,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trisk_date ON trade_risk_premium_raw(trade_date DESC);

            CREATE TABLE IF NOT EXISTS trade_observation (
                biz_key TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                total_amount REAL,
                concentration5 REAL,
                risk_premium REAL,
                margin_ratio REAL,
                margin_financing_ratio REAL,
                margin_balance REAL,
                financing_balance REAL,
                securities_balance REAL,
                financing_to_float_mv REAL,
                financing_buy_pct REAL,
                debt_investors REAL,
                up_count INTEGER,
                down_count INTEGER,
                flat_count INTEGER,
                source_set TEXT,
                computed_at TEXT,
                run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tobs_date ON trade_observation(trade_date DESC);

            -- 真实新增入库行流水：仅 AFTER INSERT 触发，幂等 upsert 的 UPDATE 分支不计入。
            -- 用于「当日净新增」统计，区别于 collection_stat 的接口返回行数累计。
            CREATE TABLE IF NOT EXISTS new_row_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                run_date TEXT,
                category TEXT NOT NULL,
                code TEXT NOT NULL,
                collected_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_newlog_date ON new_row_log(run_date, category);
            CREATE TRIGGER IF NOT EXISTS trg_macro_new_row AFTER INSERT ON macro_observation BEGIN
                INSERT INTO new_row_log(run_id, run_date, category, code, collected_at)
                VALUES (NEW.run_id, COALESCE(NULLIF(substr(NEW.collected_at,1,10),''), date('now')), '宏观', NEW.code, NEW.collected_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_etf_new_row AFTER INSERT ON etf_observation BEGIN
                INSERT INTO new_row_log(run_id, run_date, category, code, collected_at)
                VALUES (NEW.run_id, COALESCE(NULLIF(substr(NEW.collected_at,1,10),''), date('now')), 'ETF', NEW.code, NEW.collected_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_index_new_row AFTER INSERT ON index_observation BEGIN
                INSERT INTO new_row_log(run_id, run_date, category, code, collected_at)
                VALUES (NEW.run_id, COALESCE(NULLIF(substr(NEW.collected_at,1,10),''), date('now')), '指数', NEW.code, NEW.collected_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_tex_new_row AFTER INSERT ON trade_exchange_raw BEGIN
                INSERT INTO new_row_log(run_id, run_date, category, code, collected_at)
                VALUES (NEW.run_id, COALESCE(NULLIF(substr(NEW.collected_at,1,10),''), date('now')), '交易', 'exchange', NEW.collected_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_tmargin_new_row AFTER INSERT ON trade_margin_raw BEGIN
                INSERT INTO new_row_log(run_id, run_date, category, code, collected_at)
                VALUES (NEW.run_id, COALESCE(NULLIF(substr(NEW.collected_at,1,10),''), date('now')), '交易', 'margin', NEW.collected_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_tsnap_new_row AFTER INSERT ON trade_snapshot_raw BEGIN
                INSERT INTO new_row_log(run_id, run_date, category, code, collected_at)
                VALUES (NEW.run_id, COALESCE(NULLIF(substr(NEW.collected_at,1,10),''), date('now')), '交易', 'snapshot', NEW.collected_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_tbreadth_new_row AFTER INSERT ON trade_breadth_raw BEGIN
                INSERT INTO new_row_log(run_id, run_date, category, code, collected_at)
                VALUES (NEW.run_id, COALESCE(NULLIF(substr(NEW.collected_at,1,10),''), date('now')), '交易', 'breadth', NEW.collected_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_tcongestion_new_row AFTER INSERT ON trade_congestion_raw BEGIN
                INSERT INTO new_row_log(run_id, run_date, category, code, collected_at)
                VALUES (NEW.run_id, COALESCE(NULLIF(substr(NEW.collected_at,1,10),''), date('now')), '交易', 'congestion', NEW.collected_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_trisk_new_row AFTER INSERT ON trade_risk_premium_raw BEGIN
                INSERT INTO new_row_log(run_id, run_date, category, code, collected_at)
                VALUES (NEW.run_id, COALESCE(NULLIF(substr(NEW.collected_at,1,10),''), date('now')), '交易', 'risk_premium', NEW.collected_at);
            END;
            """
        )
        # 旧库在线升级：只补列，不重建/不清理任何采集原数据。
        _ensure_column(conn, "trade_observation", "risk_premium", "REAL")
        for _col in ("risk_premium", "ratio", "close", "ma70", "avg",
                     "upper1std", "lower1std", "upper2std", "lower2std"):
            _ensure_column(conn, "trade_risk_premium_raw", _col, "REAL")
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


def derived_biz_key(code: str, date: str) -> str:
    return f"derived_{code}_{str(date)[:7].replace('-', '')}"


def _date_order_sql(order: str) -> str:
    """把外部传入的排序方向映射为安全的 SQL 关键字（默认降序）。"""
    return "ASC" if str(order).lower() == "asc" else "DESC"


def etf_biz_key(code: str, trade_date: str) -> str:
    return f"{str(code).zfill(6)}_{str(trade_date).replace('-', '')}"


def index_biz_key(code: str, trade_date: str) -> str:
    return f"{str(code).zfill(6)}_{str(trade_date).replace('-', '')}"


def trade_raw_biz_key(prefix: str, trade_date: str) -> str:
    """交易原始表主键：EX/MG/BR/CG_{YYYYMMDD}。"""
    return f"{prefix}_{str(trade_date).replace('-', '')}"


def trade_snapshot_biz_key(code: str, trade_date: str) -> str:
    """个股快照原始表主键：{6位代码}_{YYYYMMDD}。"""
    return f"{str(code).zfill(6)}_{str(trade_date).replace('-', '')}"


def trade_biz_key(trade_date: str) -> str:
    """交易计算宽表主键：{YYYYMMDD}（一交易日一行）。"""
    return str(trade_date).replace('-', '')


def run_biz_key(run_id: str, interface_key: str, run_time: str, seq: int) -> str:
    stamp = str(run_time).replace("-", "").replace(":", "").replace(" ", "_")
    return f"{run_id}_{interface_key}_{stamp}_{seq:03d}"


def collection_stat_biz_key(run_id: str, category: str, code: str) -> str:
    return f"{run_id}|{category}|{str(code).zfill(6) if category in ('ETF', '指数') else code}"


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


def upsert_derived(path: str | Path, df: pd.DataFrame) -> int:
    """幂等写入派生指标；df 需含 code/name/date/value/unit/formula/grp/source/computed_at。"""
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        code = str(r.get("code", "")).strip()
        date = str(r.get("date", ""))[:7]
        if not code or not date:
            continue
        records.append((
            derived_biz_key(code, date), code, str(r.get("name", "") or ""), date,
            _clean(r.get("value")), str(r.get("unit", "") or ""),
            str(r.get("formula", "") or ""), str(r.get("grp", "") or ""),
            str(r.get("source", "") or ""), str(r.get("computed_at", "") or ""),
        ))
    with connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO derived_indicator(
                biz_key, code, name, date, value, unit, formula, grp, source, computed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                name=excluded.name, value=excluded.value, unit=excluded.unit,
                formula=excluded.formula, grp=excluded.grp, source=excluded.source,
                computed_at=excluded.computed_at
            """,
            records,
        )
    return len(records)


def replace_derived(path: str | Path, df: pd.DataFrame) -> int:
    """全量替换派生指标表（用于规则变更后重算，清理不再产出的陈旧行）。"""
    init_schema(path)
    with connect(path) as conn:
        conn.execute("DELETE FROM derived_indicator")
    return upsert_derived(path, df)


def read_derived(path: str | Path, month: str | None = None) -> pd.DataFrame:
    """读取派生指标；month 为 YYYY-MM，留空返回全部。"""
    init_schema(path)
    where = " WHERE date=?" if month else ""
    args = [str(month)[:7]] if month else []
    with connect(path) as conn:
        rows = conn.execute(
            f"SELECT code, name, date, value, unit, formula, grp, source, computed_at "
            f"FROM derived_indicator{where} ORDER BY grp, code, date DESC",
            args,
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def derived_months(path: str | Path) -> list[str]:
    init_schema(path)
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM derived_indicator ORDER BY date DESC"
        ).fetchall()
    return [r["date"] for r in rows]


def upsert_etf(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    if df is None or df.empty:
        return 0
    init_schema(path)
    records: list[tuple] = []
    fallback_records: list[tuple] = []
    fallback_mask = (
        df["数据源"].astype(str).str.contains(ETF_FALLBACK_MARK, na=False)
        if "数据源" in df.columns else pd.Series(False, index=df.index)
    )
    for idx, (_, r) in enumerate(df.iterrows()):
        code = str(r.get("基金代码", "")).strip().zfill(6)
        date = str(r.get("交易日期", ""))[:10]
        if not code or not date:
            continue
        record = (
            etf_biz_key(code, date), code, str(r.get("基金简称", "") or ""),
            str(r.get("交易市场", "") or ""), date,
            _clean(r.get("份额(亿份)")), _clean(r.get("单位净值(元)")),
            _clean(r.get("规模(亿元)")), str(r.get("数据源", "") or ""),
            str(r.get("采集时间", "") or ""), run_id,
        )
        if bool(fallback_mask.iloc[idx]):
            fallback_records.append(record)
        else:
            records.append(record)
    with connect(path) as conn:
        if records:
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
        if fallback_records:
            # 兜底行只用于本次展示/计算：先确认业务键已存在，再原样跳过。
            # 这样既不会更新原行，也不会凭空插入库里没有的历史日。
            keys = [rec[0] for rec in fallback_records]
            existing: set[str] = set()
            for offset in range(0, len(keys), 500):
                chunk = keys[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                existing.update(
                    row["biz_key"] for row in conn.execute(
                        f"SELECT biz_key FROM etf_observation WHERE biz_key IN ({placeholders})",
                        chunk,
                    )
                )
            existing_records = [rec for rec in fallback_records if rec[0] in existing]
            if existing_records:
                conn.executemany(
                    """
                    INSERT INTO etf_observation(
                        biz_key, code, name, market, trade_date, shares, nav, market_cap,
                        source, collected_at, run_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(biz_key) DO NOTHING
                    """,
                    existing_records,
                )
    # 返回真实采集写入行数，兜底行不计入本次新增/采集量。
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


# ---------- 交易原始数据层（采集只落库，不计算） ----------
def upsert_trade_exchange(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    """幂等写入交易所每日概况原始表（保留接口原单位：沪亿元/深元）。"""
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        date = str(r.get("trade_date", ""))[:10]
        if not date:
            continue
        records.append((
            trade_raw_biz_key("EX", date), date,
            _clean(r.get("sse_amount")), _clean(r.get("sse_volume")),
            _clean(r.get("sse_float_mv")), _clean(r.get("sse_total_mv")),
            _clean(r.get("szse_amount")), _clean(r.get("szse_float_mv")),
            _clean(r.get("szse_total_mv")),
            str(r.get("source", "") or ""), str(r.get("collected_at", "") or ""), run_id,
        ))
    with connect(path) as conn:
        conn.executemany(
            """INSERT INTO trade_exchange_raw(
                biz_key, trade_date, sse_amount, sse_volume, sse_float_mv, sse_total_mv,
                szse_amount, szse_float_mv, szse_total_mv, source, collected_at, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                sse_amount=excluded.sse_amount, sse_volume=excluded.sse_volume,
                sse_float_mv=excluded.sse_float_mv, sse_total_mv=excluded.sse_total_mv,
                szse_amount=excluded.szse_amount, szse_float_mv=excluded.szse_float_mv,
                szse_total_mv=excluded.szse_total_mv, source=excluded.source,
                collected_at=excluded.collected_at, run_id=excluded.run_id""",
            records,
        )
    return len(records)


def upsert_trade_margin(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    """幂等写入两融账户原始表（13 列原样，单位由计算层处理）。"""
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        date = str(r.get("trade_date", ""))[:10]
        if not date:
            continue
        records.append((
            trade_raw_biz_key("MG", date), date,
            _clean(r.get("financing_balance")), _clean(r.get("securities_balance")),
            _clean(r.get("financing_buy")), _clean(r.get("securities_sell")),
            _clean(r.get("margin_ratio")), _clean(r.get("debt_investors")),
            _clean(r.get("collateral_value")), _clean(r.get("active_investors")),
            _clean(r.get("individual_investors")), _clean(r.get("institutional_investors")),
            _clean(r.get("broker_count")), _clean(r.get("branch_count")),
            str(r.get("source", "") or ""), str(r.get("collected_at", "") or ""), run_id,
        ))
    with connect(path) as conn:
        conn.executemany(
            """INSERT INTO trade_margin_raw(
                biz_key, trade_date, financing_balance, securities_balance,
                financing_buy, securities_sell, margin_ratio, debt_investors,
                collateral_value, active_investors, individual_investors,
                institutional_investors, broker_count, branch_count,
                source, collected_at, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                financing_balance=excluded.financing_balance,
                securities_balance=excluded.securities_balance,
                financing_buy=excluded.financing_buy,
                securities_sell=excluded.securities_sell,
                margin_ratio=excluded.margin_ratio,
                debt_investors=excluded.debt_investors,
                collateral_value=excluded.collateral_value,
                active_investors=excluded.active_investors,
                individual_investors=excluded.individual_investors,
                institutional_investors=excluded.institutional_investors,
                broker_count=excluded.broker_count, branch_count=excluded.branch_count,
                source=excluded.source, collected_at=excluded.collected_at,
                run_id=excluded.run_id""",
            records,
        )
    return len(records)


def upsert_trade_snapshot(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    """幂等写入全A个股快照原始表（逐股一行）。"""
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        code = str(r.get("code", "")).strip().zfill(6)
        date = str(r.get("trade_date", ""))[:10]
        if not code or not date:
            continue
        records.append((
            trade_snapshot_biz_key(code, date), date, code,
            str(r.get("name", "") or ""),
            _clean(r.get("pct_change")), _clean(r.get("volume")),
            _clean(r.get("amount")), _clean(r.get("last_price")),
            str(r.get("source", "") or ""), str(r.get("collected_at", "") or ""), run_id,
        ))
    with connect(path) as conn:
        conn.executemany(
            """INSERT INTO trade_snapshot_raw(
                biz_key, trade_date, code, name, pct_change, volume, amount,
                last_price, source, collected_at, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                name=excluded.name, pct_change=excluded.pct_change,
                volume=excluded.volume, amount=excluded.amount,
                last_price=excluded.last_price, source=excluded.source,
                collected_at=excluded.collected_at, run_id=excluded.run_id""",
            records,
        )
    return len(records)


def upsert_trade_breadth(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    """幂等写入东方财富 MCP 市场宽度原始表（每日涨跌家数）。"""
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        date = str(r.get("trade_date", ""))[:10]
        if not date:
            continue
        records.append((
            trade_raw_biz_key("BR", date), date,
            _clean(r.get("up_count")), _clean(r.get("down_count")),
            _clean(r.get("flat_count")),
            str(r.get("source", "") or ""), str(r.get("collected_at", "") or ""), run_id,
        ))
    with connect(path) as conn:
        conn.executemany(
            """INSERT INTO trade_breadth_raw(
                biz_key, trade_date, up_count, down_count, flat_count,
                source, collected_at, run_id
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                up_count=excluded.up_count, down_count=excluded.down_count,
                flat_count=excluded.flat_count, source=excluded.source,
                collected_at=excluded.collected_at, run_id=excluded.run_id""",
            records,
        )
    return len(records)


def upsert_trade_congestion(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    """幂等写入乐咕大盘拥挤度原始表（前5%个股成交额占比）。"""
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        date = str(r.get("trade_date", ""))[:10]
        if not date:
            continue
        records.append((
            trade_raw_biz_key("CG", date), date,
            _clean(r.get("close_price")), _clean(r.get("top5_amount_yi")),
            _clean(r.get("total_amount_yi")), _clean(r.get("concentration5")),
            str(r.get("source", "") or ""), str(r.get("collected_at", "") or ""), run_id,
        ))
    with connect(path) as conn:
        conn.executemany(
            """INSERT INTO trade_congestion_raw(
                biz_key, trade_date, close_price, top5_amount_yi, total_amount_yi,
                concentration5, source, collected_at, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                close_price=excluded.close_price,
                top5_amount_yi=excluded.top5_amount_yi,
                total_amount_yi=excluded.total_amount_yi,
                concentration5=excluded.concentration5,
                source=excluded.source, collected_at=excluded.collected_at,
                run_id=excluded.run_id""",
            records,
        )
    return len(records)


def upsert_trade_risk_premium(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    """幂等写入沪深300风险溢价率原始表（韭圈儿 FED 差值，数据源直采值原样落库）。"""
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        date = str(r.get("trade_date", ""))[:10]
        if not date:
            continue
        records.append((
            trade_raw_biz_key("RP", date), date,
            _clean(r.get("risk_premium")), _clean(r.get("ratio")), _clean(r.get("close")),
            _clean(r.get("ma70")), _clean(r.get("avg")),
            _clean(r.get("upper1std")), _clean(r.get("lower1std")),
            _clean(r.get("upper2std")), _clean(r.get("lower2std")),
            str(r.get("source", "") or ""), str(r.get("collected_at", "") or ""), run_id,
        ))
    with connect(path) as conn:
        conn.executemany(
            """INSERT INTO trade_risk_premium_raw(
                biz_key, trade_date, risk_premium, ratio, close, ma70, avg,
                upper1std, lower1std, upper2std, lower2std,
                source, collected_at, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                risk_premium=excluded.risk_premium, ratio=excluded.ratio,
                close=excluded.close, ma70=excluded.ma70, avg=excluded.avg,
                upper1std=excluded.upper1std, lower1std=excluded.lower1std,
                upper2std=excluded.upper2std, lower2std=excluded.lower2std,
                source=excluded.source, collected_at=excluded.collected_at,
                run_id=excluded.run_id""",
            records,
        )
    return len(records)


def read_trade_raw(path: str | Path, table: str, start: str | None = None,
                   end: str | None = None) -> pd.DataFrame:
    """读取交易原始表（含 risk_premium），可按日期范围过滤。"""
    init_schema(path)
    allowed = {"exchange": "trade_exchange_raw", "margin": "trade_margin_raw",
               "snapshot": "trade_snapshot_raw", "breadth": "trade_breadth_raw",
               "congestion": "trade_congestion_raw", "risk_premium": "trade_risk_premium_raw"}
    if table not in allowed:
        return pd.DataFrame()
    sql = f"SELECT * FROM {allowed[table]}"
    args: list = []
    where = []
    if start:
        where.append("trade_date>=?"); args.append(str(start)[:10])
    if end:
        where.append("trade_date<=?"); args.append(str(end)[:10])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY trade_date DESC"
    with connect(path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def trade_raw_dates(path: str | Path, table: str) -> list[str]:
    """返回某原始表已落库的交易日列表（升序）。"""
    init_schema(path)
    allowed = {"exchange": "trade_exchange_raw", "margin": "trade_margin_raw",
               "snapshot": "trade_snapshot_raw", "breadth": "trade_breadth_raw",
               "congestion": "trade_congestion_raw", "risk_premium": "trade_risk_premium_raw"}
    if table not in allowed:
        return []
    with connect(path) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT trade_date FROM {allowed[table]} ORDER BY trade_date"
        ).fetchall()
    return [r["trade_date"] for r in rows]


# ---------- 交易计算宽表 ----------
def upsert_trade(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    """幂等写入交易计算宽表；df 每行为一交易日的 11 指标 + 涨跌家数。"""
    if df is None or df.empty:
        return 0
    init_schema(path)
    records = []
    for _, r in df.iterrows():
        date = str(r.get("trade_date", ""))[:10]
        if not date:
            continue
        records.append((
            trade_biz_key(date), date,
            _clean(r.get("total_amount")), _clean(r.get("concentration5")),
            _clean(r.get("risk_premium")),
            _clean(r.get("margin_ratio")), _clean(r.get("margin_financing_ratio")),
            _clean(r.get("margin_balance")), _clean(r.get("financing_balance")),
            _clean(r.get("securities_balance")), _clean(r.get("financing_to_float_mv")),
            _clean(r.get("financing_buy_pct")), _clean(r.get("debt_investors")),
            _clean(r.get("up_count")), _clean(r.get("down_count")), _clean(r.get("flat_count")),
            str(r.get("source_set", "") or ""), str(r.get("computed_at", "") or ""), run_id,
        ))
    with connect(path) as conn:
        conn.executemany(
            """INSERT INTO trade_observation(
                biz_key, trade_date, total_amount, concentration5, risk_premium, margin_ratio,
                margin_financing_ratio, margin_balance, financing_balance,
                securities_balance, financing_to_float_mv, financing_buy_pct,
                debt_investors, up_count, down_count, flat_count,
                source_set, computed_at, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                total_amount=excluded.total_amount, concentration5=excluded.concentration5,
                risk_premium=excluded.risk_premium,
                margin_ratio=excluded.margin_ratio,
                margin_financing_ratio=excluded.margin_financing_ratio,
                margin_balance=excluded.margin_balance,
                financing_balance=excluded.financing_balance,
                securities_balance=excluded.securities_balance,
                financing_to_float_mv=excluded.financing_to_float_mv,
                financing_buy_pct=excluded.financing_buy_pct,
                debt_investors=excluded.debt_investors,
                up_count=excluded.up_count, down_count=excluded.down_count,
                flat_count=excluded.flat_count, source_set=excluded.source_set,
                computed_at=excluded.computed_at, run_id=excluded.run_id""",
            records,
        )
    return len(records)


def replace_trade(path: str | Path, df: pd.DataFrame, run_id: str = "") -> int:
    """全量替换交易计算宽表（手动全量重算时清理陈旧行）。"""
    init_schema(path)
    with connect(path) as conn:
        conn.execute("DELETE FROM trade_observation")
    return upsert_trade(path, df, run_id)


def read_trade(path: str | Path, start: str | None = None,
               end: str | None = None) -> pd.DataFrame:
    """读取交易计算宽表，按交易日升序，可按日期范围过滤。"""
    init_schema(path)
    sql = "SELECT * FROM trade_observation"
    args: list = []
    where = []
    if start:
        where.append("trade_date>=?"); args.append(str(start)[:10])
    if end:
        where.append("trade_date<=?"); args.append(str(end)[:10])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY trade_date"
    with connect(path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


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


def append_collection_stats(
    path: str | Path,
    records: list[dict],
) -> int:
    """写入每次采集按类别/细分口径的入库行数（同 run_id+类别+代码覆盖）。

    records 每项：run_id / run_type / run_date / category / code / name / rows / collected_at。
    """
    if not records:
        return 0
    init_schema(path)
    rows = []
    for rec in records:
        run_id = str(rec.get("run_id", ""))
        category = str(rec.get("category", ""))
        code = str(rec.get("code", ""))
        rows.append((
            collection_stat_biz_key(run_id, category, code),
            run_id, str(rec.get("run_type", "")), str(rec.get("run_date", "")),
            category, code, str(rec.get("name", "") or ""),
            int(rec.get("rows", 0) or 0), str(rec.get("collected_at", "") or ""),
        ))
    with connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO collection_stat(
                biz_key, run_id, run_type, run_date, category, code, name, rows, collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(biz_key) DO UPDATE SET
                name=excluded.name, rows=excluded.rows, collected_at=excluded.collected_at
            """,
            rows,
        )
    return len(rows)


def read_collection_stats(path: str | Path) -> pd.DataFrame:
    """读取全部采集量统计，按运行时间倒序。"""
    init_schema(path)
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT run_id, run_type, run_date, category, code, name, rows, collected_at "
            "FROM collection_stat ORDER BY run_date DESC, run_id DESC, category, code"
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


# ---------- 读取 ----------
def read_macro(path: str | Path, code: str | None = None,
               start: str | None = None, end: str | None = None,
               limit: int | None = None, offset: int = 0,
               date_order: str = "desc") -> tuple[int, pd.DataFrame]:
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
            ORDER BY code ASC, date {_date_order_sql(date_order)}
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


def read_macro_changes_by_run(path: str | Path, run_id: str) -> tuple[set[str], set[str]]:
    """读取本次采集 run_id 写入的宏观观测，返回 (代码集合, 月份集合)。

    供派生指标增量重算识别"本次变更范围"：只重算依赖了这些代码、且落在这些
    月份（及其差分/同比偏移月）的派生行，避免每次定时调度都全量重算。
    run_id 为空时返回空集合，调用方据此回退全量。
    """
    init_schema(path)
    if not run_id:
        return set(), set()
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT code, substr(date, 1, 7) AS ym FROM macro_observation "
            "WHERE run_id=?",
            (str(run_id),),
        ).fetchall()
    codes = {str(r["code"]).strip() for r in rows if r["code"]}
    months = {str(r["ym"])[:7] for r in rows if r["ym"]}
    return codes, months


def read_etf(path: str | Path, code: str | None = None,
             start: str | None = None, end: str | None = None,
             limit: int | None = None, offset: int = 0,
             date_order: str = "desc") -> tuple[int, pd.DataFrame]:
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
            ORDER BY code ASC, trade_date {_date_order_sql(date_order)}
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


def read_etf_with_changes(
    path: str | Path,
    code: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    date_order: str = "desc",
) -> tuple[int, pd.DataFrame]:
    """读取 ETF 并追加较上一交易日的份额/规模变化指标。

    先在全量数据上按代码计算环比，再按 code/start/end 过滤，避免起始日被截断后
    把窗口内第一行误当成“无上一交易日”。
    """
    from processors.etf_change import ETF_CHANGE_COLUMNS, compute_etf_changes

    init_schema(path)
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT code, name, market, trade_date, shares, nav, market_cap, source, collected_at "
            "FROM etf_observation"
        ).fetchall()
    if not rows:
        cols = ETF_COLUMNS + ETF_CHANGE_COLUMNS
        return 0, pd.DataFrame(columns=cols)
    df = pd.DataFrame([dict(r) for r in rows]).rename(columns={
        "code": "基金代码", "name": "基金简称", "market": "交易市场",
        "trade_date": "交易日期", "shares": "份额(亿份)", "nav": "单位净值(元)",
        "market_cap": "规模(亿元)", "source": "数据源", "collected_at": "采集时间",
    })[ETF_COLUMNS]
    df = compute_etf_changes(df)
    if code:
        df = df[df["基金代码"] == str(code).strip().zfill(6)]
    if start:
        df = df[df["交易日期"] >= str(start)[:10]]
    if end:
        df = df[df["交易日期"] <= str(end)[:10]]
    total = len(df)
    df = df.sort_values(
        ["基金代码", "交易日期"],
        ascending=[True, str(date_order).lower() == "asc"],
    ).reset_index(drop=True)
    if limit is not None:
        df = df.iloc[int(offset):int(offset) + int(limit)]
    return int(total), df[ETF_COLUMNS + ETF_CHANGE_COLUMNS]


def read_index(path: str | Path, code: str | None = None,
               start: str | None = None, end: str | None = None,
               limit: int | None = None, offset: int = 0,
               date_order: str = "desc") -> tuple[int, pd.DataFrame]:
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
            ORDER BY code ASC, trade_date {_date_order_sql(date_order)}
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
        trade = conn.execute(
            "SELECT COUNT(*) AS rows FROM trade_observation"
        ).fetchone()
        raw_rows = {}
        for tname, tkey in [("exchange", "trade_exchange_raw"), ("margin", "trade_margin_raw"),
                            ("snapshot", "trade_snapshot_raw"), ("breadth", "trade_breadth_raw"),
                            ("congestion", "trade_congestion_raw"),
                            ("risk_premium", "trade_risk_premium_raw")]:
            raw_rows[tname] = int(conn.execute(
                f"SELECT COUNT(*) AS n FROM {tkey}"
            ).fetchone()["n"])
        failed = conn.execute(
            "SELECT COUNT(*) AS n FROM interface_run WHERE status='failed'"
        ).fetchone()["n"]
        today_calls = conn.execute(
            "SELECT COUNT(*) AS n FROM interface_run WHERE run_date=?", (today,)
        ).fetchone()["n"]
        # 当日净新增：new_row_log 仅记录真正 INSERT 的业务行；幂等 upsert 的 UPDATE 分支不计入。
        today_new: dict[str, int] = {}
        for row in conn.execute(
            "SELECT category, COUNT(*) AS n FROM new_row_log "
            "WHERE run_date=? GROUP BY category", (today,)
        ):
            today_new[row["category"]] = int(row["n"] or 0)
        # 当日接口写入行数累计（含重复采集/全历史接口返回），仅用于对账，不再作为「新增」展示。
        today_written: dict[str, int] = {}
        for row in conn.execute(
            "SELECT category, SUM(rows) AS rows FROM collection_stat "
            "WHERE run_date=? GROUP BY category", (today,)
        ):
            today_written[row["category"]] = int(row["rows"] or 0)
        last = conn.execute(
            "SELECT run_id, run_date, run_time, run_type FROM interface_run "
            "ORDER BY run_time DESC LIMIT 1"
        ).fetchone()
        last_run = dict(last) if last else {}
        # 最近一次运行的各项指标采集数量（按 run_id 从 collection_stat 汇总）。
        # 兼容：Choice 导入等 run 不写 collection_stat，此时 last_counts 为空。
        last_counts: dict[str, dict[str, int]] = {}
        if last_run.get("run_id"):
            for row in conn.execute(
                "SELECT category, COUNT(DISTINCT code) AS codes, "
                "SUM(rows) AS rows FROM collection_stat WHERE run_id=? "
                "GROUP BY category",
                (last_run["run_id"],),
            ):
                last_counts[row["category"]] = {
                    "codes": int(row["codes"]), "rows": int(row["rows"]),
                }
    return {
        "macro_rows": int(macro["rows"]), "macro_indicators": int(macro["codes"]),
        "etf_rows": int(etf["rows"]), "etf_codes": int(etf["codes"]),
        "index_rows": int(index["rows"]), "index_codes": int(index["codes"]),
        "trade_rows": int(trade["rows"]), "trade_raw_rows": raw_rows,
        "today_new": today_new, "today_written": today_written,
        "failed_total": int(failed), "today_calls": int(today_calls),
        "last_run": last_run,
        "last_counts": last_counts,
    }


# ---------- ETF 配置 ----------
DEFAULT_ETF_CONFIG = {
    "codes": [], "names": {}, "listing_dates": {}, "start_date": "",
    "end_date": "", "default_trade_days": 1,
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
    "codes": [], "start_date": "", "end_date": "", "lookback_days": 1,
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


# ---------- 按口径/时间范围清理业务数据 ----------
# 仅覆盖三类观测表，不触碰配置、监控、meta 与导出记录。
CLEAN_TARGETS = {
    "macro": {"table": "macro_observation", "date_col": "date", "date_len": 7, "zfill": 0},
    "etf": {"table": "etf_observation", "date_col": "trade_date", "date_len": 10, "zfill": 6},
    "index": {"table": "index_observation", "date_col": "trade_date", "date_len": 10, "zfill": 6},
}

# 系统硬规则：采集原数据是权威资产，不得进入任何自动/批量清理范围。
# 交易 6 张 *_raw 表尤其不可删除；观测表只能在用户显式确认后按口径清理。
IMMUTABLE_RAW_TABLES = (
    "trade_exchange_raw", "trade_margin_raw", "trade_snapshot_raw",
    "trade_breadth_raw", "trade_congestion_raw", "trade_risk_premium_raw",
)
COLLECTED_DATA_TABLES = IMMUTABLE_RAW_TABLES + tuple(
    spec["table"] for spec in CLEAN_TARGETS.values()
)


def _clean_scope(category: str, code: str | None, start: str | None,
                 end: str | None) -> tuple[str, str, list]:
    if category not in CLEAN_TARGETS:
        raise ValueError(f"category 应为 {', '.join(CLEAN_TARGETS)}")
    spec = CLEAN_TARGETS[category]
    if spec["table"] in IMMUTABLE_RAW_TABLES:
        raise ValueError(f"系统规则禁止清理采集原数据表：{spec['table']}")
    clauses: list[str] = []
    args: list = []
    if code:
        c = str(code).strip()
        if spec["zfill"]:
            c = c.zfill(spec["zfill"])
        clauses.append("code=?")
        args.append(c)
    if start:
        clauses.append(f"{spec['date_col']}>=?")
        args.append(str(start)[:spec["date_len"]])
    if end:
        clauses.append(f"{spec['date_col']}<=?")
        args.append(str(end)[:spec["date_len"]])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return spec["table"], where, args


def preview_clean(path: str | Path, category: str, code: str | None = None,
                  start: str | None = None, end: str | None = None) -> dict:
    """预览按类别/代码/时间范围将删除的行数，不修改任何数据。"""
    init_schema(path)
    table, where, args = _clean_scope(category, code, start, end)
    with connect(path) as conn:
        matched = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}{where}", args
        ).fetchone()["n"]
        total = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        sample = conn.execute(
            f"SELECT code, {CLEAN_TARGETS[category]['date_col']} AS d "
            f"FROM {table}{where} ORDER BY d DESC LIMIT 3", args
        ).fetchall()
    return {
        "category": category,
        "code": str(code).strip() if code else "",
        "start": str(start) if start else "",
        "end": str(end) if end else "",
        "matched": int(matched),
        "total": int(total),
        "sample": [{"code": r["code"], "date": r["d"]} for r in sample],
    }


def clean_data(path: str | Path, category: str, code: str | None = None,
               start: str | None = None, end: str | None = None) -> dict:
    """按类别/代码/时间范围删除观测数据，返回删除行数与剩余行数。"""
    init_schema(path)
    table, where, args = _clean_scope(category, code, start, end)
    with connect(path) as conn:
        before = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        cur = conn.execute(f"DELETE FROM {table}{where}", args)
        deleted = cur.rowcount
        after = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    result = {
        "category": category,
        "code": str(code).strip() if code else "",
        "start": str(start) if start else "",
        "end": str(end) if end else "",
        "deleted": int(deleted),
        "before": int(before),
        "after": int(after),
    }
    logger.warning("按口径清理数据：%s", result)
    return result


# ---------- 接口监控记录清理（仅 interface_run） ----------
def _monitor_scope(start: str | None, end: str | None, status: str | None,
                   run_type: str | None, category: str | None,
                   keyword: str | None) -> tuple[str, list]:
    clauses: list[str] = []
    args: list = []
    if start:
        clauses.append("run_date>=?")
        args.append(str(start)[:10])
    if end:
        clauses.append("run_date<=?")
        args.append(str(end)[:10])
    if status:
        clauses.append("status=?")
        args.append(str(status).strip())
    if run_type:
        clauses.append("run_type=?")
        args.append(str(run_type).strip())
    if category:
        clauses.append("category=?")
        args.append(str(category).strip())
    if keyword:
        kw = str(keyword).strip()
        if kw:
            clauses.append(
                "(interface_key LIKE ? OR interface_name LIKE ? OR error LIKE ?)"
            )
            args.extend([f"%{kw}%"] * 3)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, args


def preview_monitor_clean(path: str | Path, start: str | None = None,
                          end: str | None = None, status: str | None = None,
                          run_type: str | None = None, category: str | None = None,
                          keyword: str | None = None) -> dict:
    """预览将删除的接口监控记录数，不修改数据。"""
    init_schema(path)
    where, args = _monitor_scope(start, end, status, run_type, category, keyword)
    with connect(path) as conn:
        matched = conn.execute(
            f"SELECT COUNT(*) AS n FROM interface_run{where}", args
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM interface_run").fetchone()["n"]
        sample = conn.execute(
            "SELECT interface_name, run_date, status FROM interface_run"
            f"{where} ORDER BY run_time DESC LIMIT 3", args
        ).fetchall()
    return {
        "matched": int(matched), "total": int(total),
        "sample": [{"name": r["interface_name"], "date": r["run_date"],
                    "status": r["status"]} for r in sample],
    }


def clean_monitor_data(path: str | Path, start: str | None = None,
                       end: str | None = None, status: str | None = None,
                       run_type: str | None = None, category: str | None = None,
                       keyword: str | None = None) -> dict:
    """按口径删除接口监控记录，返回删除与剩余行数；不影响业务数据表。"""
    init_schema(path)
    where, args = _monitor_scope(start, end, status, run_type, category, keyword)
    with connect(path) as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM interface_run").fetchone()["n"]
        cur = conn.execute(f"DELETE FROM interface_run{where}", args)
        deleted = cur.rowcount
        after = conn.execute("SELECT COUNT(*) AS n FROM interface_run").fetchone()["n"]
    result = {"deleted": int(deleted), "before": int(before), "after": int(after)}
    logger.warning("按口径清理接口监控记录：%s", result)
    return result


# ---------- 可清理数据（按需调用，服务启动不会触发） ----------
# 采集原数据（观测表 + 交易 *_raw 表）永不进入 clear_all；
# 观测表的按口径删除只走 clean_data()，且必须由用户在危险操作中显式确认。
CLEAR_TABLES = [
    "interface_run", "etf_config", "index_config", "meta", "export_log",
    "collection_stat", "new_row_log",
]


def clear_all(path: str | Path) -> dict[str, int]:
    """清空可清理数据，仅保留表结构；采集原数据永不删除。

    清空范围仅限监控、配置、元信息、导出记录与采集统计。
    `macro_observation` / `etf_observation` / `index_observation` 与 6 张交易
    `*_raw` 表属于采集原数据，任何自动或批量清理都不得触碰。
    config.json 作为配置镜像/回滚基线不在此清除（其 etf/index 段仍作为默认配置来源）。
    """
    conflict = set(CLEAR_TABLES) & set(COLLECTED_DATA_TABLES)
    if conflict:
        raise RuntimeError(f"系统规则冲突：采集原数据不得进入清理范围：{sorted(conflict)}")

    init_schema(path)
    removed: dict[str, int] = {}
    with connect(path) as conn:
        for table in CLEAR_TABLES:
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            conn.execute(f"DELETE FROM {table}")
            removed[table] = int(n)
        # 自增计数归零（export_log.id），使库完全回到初始状态
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('export_log')"
        )
    logger.info("已清空 SQLite 可清理数据（采集原数据已保护）：%s", removed)
    return removed
