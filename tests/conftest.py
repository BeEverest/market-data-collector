"""公共测试夹具：全部使用临时目录，绝不污染真实 output/ 数据。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """隔离的项目根目录。"""
    (tmp_path / "output").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def config(base: Path) -> dict:
    """测试配置：SQLite 指向临时目录，关闭自动导出。"""
    return {
        "storage": {
            "backend": "sqlite",
            "db_path": str(base / "output" / "test.db"),
            "auto_export_on_collect": False,
        },
        "output": {
            "dir": "output",
            "formats": ["csv", "excel"],
            "filename_prefix": "market_data",
            "keep_snapshots": 3,
        },
        "etf": {
            "codes": ["510050", "159915"],
            "listing_dates": {"510050": "2004-12-30", "159915": "2011-09-20"},
            "start_date": "",
            "end_date": "",
            "default_trade_days": 20,
        },
        "macro": {"start_date": "2024-01"},
        "monitor": {"history_file": str(base / "output" / "monitor" / "interface_runs.jsonl")},
        "scheduler": {"enabled": False, "time": "08:30", "run_type": "full"},
        "web": {"host": "127.0.0.1", "port": 8000},
    }


@pytest.fixture
def config_file(base: Path, config: dict) -> Path:
    path = base / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def db_path(config: dict) -> Path:
    return Path(config["storage"]["db_path"])


@pytest.fixture
def macro_df() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "指标代码": "M2", "指标名称": "广义货币(M2)", "日期": "2026-10",
            "数值": 3150000.0, "单位": "亿元", "同比(%)": 8.2, "环比(%)": 0.4,
            "数据源": "PBC", "采集时间": "2026-11-01 09:00:00",
        },
        {
            "指标代码": "M2", "指标名称": "广义货币(M2)", "日期": "2026-09",
            "数值": 3135000.0, "单位": "亿元", "同比(%)": 8.0, "环比(%)": 0.3,
            "数据源": "PBC", "采集时间": "2026-10-01 09:00:00",
        },
        {
            "指标代码": "CPI", "指标名称": "居民消费价格指数CPI", "日期": "2026-10",
            "数值": 100.2, "单位": "上年同月=100", "同比(%)": 0.2, "环比(%)": None,
            "数据源": "NBS", "采集时间": "2026-11-01 09:00:00",
        },
    ])


@pytest.fixture
def etf_df() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "基金代码": "510050", "基金简称": "50ETF", "交易市场": "SH",
            "交易日期": "2026-10-20", "份额(亿份)": 100.0, "单位净值(元)": 3.5,
            "规模(亿元)": 350.0, "数据源": "SSE+天天基金",
            "采集时间": "2026-10-21 08:00:00",
        },
        {
            "基金代码": "159915", "基金简称": "创业板ETF", "交易市场": "SZ",
            "交易日期": "2026-10-20", "份额(亿份)": 50.0, "单位净值(元)": 2.4,
            "规模(亿元)": 120.0, "数据源": "SZSE+天天基金",
            "采集时间": "2026-10-21 08:00:00",
        },
        {
            "基金代码": "510050", "基金简称": "50ETF", "交易市场": "SH",
            "交易日期": "2026-10-19", "份额(亿份)": 98.0, "单位净值(元)": 3.48,
            "规模(亿元)": 341.04, "数据源": "SSE+天天基金",
            "采集时间": "2026-10-20 08:00:00",
        },
    ])


@pytest.fixture
def run_records() -> list[dict]:
    """两条成功 + 两条失败，覆盖失败批次与重试成功场景。"""
    return [
        {
            "run_id": "20261101_090000_full", "run_type": "full", "run_date": "2026-11-01",
            "run_time": "2026-11-01 09:00:00", "category": "宏观",
            "interface_key": "macro.macro_china_money_supply", "interface_name": "货币供应量",
            "params": '{"code": "M2"}', "status": "success", "attempts": 1,
            "rows": 24, "error": "", "duration_sec": 1.2,
        },
        {
            "run_id": "20261101_090000_full", "run_type": "full", "run_date": "2026-11-01",
            "run_time": "2026-11-01 09:00:10", "category": "宏观",
            "interface_key": "macro.macro_china_cpi", "interface_name": "CPI",
            "params": '{"code": "CPI"}', "status": "failed", "attempts": 3,
            "rows": 0, "error": "TimeoutError: 读超时", "duration_sec": 6.0,
        },
        {
            "run_id": "20261102_090000_full", "run_type": "full", "run_date": "2026-11-02",
            "run_time": "2026-11-02 09:00:00", "category": "ETF",
            "interface_key": "etf.sse_shares", "interface_name": "上交所份额",
            "params": '{"date": "2026-11-01"}', "status": "failed", "attempts": 2,
            "rows": 0, "error": "ValueError: 无数据", "duration_sec": 3.0,
        },
        {
            "run_id": "20261102_100000_retry", "run_type": "retry", "run_date": "2026-11-02",
            "run_time": "2026-11-02 10:00:00", "category": "ETF",
            "interface_key": "etf.sse_shares", "interface_name": "上交所份额",
            "params": '{"date": "2026-11-01"}', "status": "success", "attempts": 1,
            "rows": 5, "error": "", "duration_sec": 1.0,
        },
    ]
