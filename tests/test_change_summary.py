"""变更展示与重跑上市日期复用测试。

约束：入库日志只展示本次新增/更新/未变，更新需给出业务字段原因；
失败重跑必须复用数据库/配置中已有的 ETF 上市日期，不重复联网。
"""
import pandas as pd
import pytest

import main
import storage


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "market_data.db"
    storage.init_schema(path)
    return path


def _etf_row(code="510050", date="2026-11-26", shares=100.0, nav=2.0, source="akshare"):
    return {
        "基金代码": code,
        "基金简称": "50ETF",
        "交易市场": "SH",
        "交易日期": date,
        "份额(亿份)": shares,
        "单位净值(元)": nav,
        "规模(亿元)": shares * nav,
        "数据源": source,
        "采集时间": "2026-11-26 10:00:00",
    }


class TestWriteChangeSummary:
    def test_counts_new_updated_and_unchanged(self, db_path):
        storage.upsert_etf(db_path, pd.DataFrame([_etf_row()]), run_id="old")
        incoming = pd.DataFrame([
            _etf_row(),                                   # 业务字段完全一致：未变
            _etf_row(shares=101.0),                       # 份额变化：更新
            _etf_row(date="2026-11-27", shares=5.0),      # 新业务键：新增
        ])
        changed = main._write_change_summary(db_path, pd.DataFrame(), incoming)
        assert changed["ETF"]["新增"] == 1
        assert changed["ETF"]["更新"] == 1
        assert changed["ETF"]["未变"] == 1
        assert changed["ETF"]["原因"] == {"份额(亿份)": 1, "规模(亿元)": 1}

    def test_collect_time_and_run_id_do_not_count_as_update(self, db_path):
        old = _etf_row()
        storage.upsert_etf(db_path, pd.DataFrame([old]), run_id="old-run")
        incoming = pd.DataFrame([dict(old, **{
            "采集时间": "2026-11-27 12:00:00",
        })])
        changed = main._write_change_summary(db_path, pd.DataFrame(), incoming)
        assert changed["ETF"]["更新"] == 0
        assert changed["ETF"]["未变"] == 1

    def test_fallback_rows_are_excluded(self, db_path):
        storage.upsert_etf(db_path, pd.DataFrame([_etf_row()]), run_id="old")
        incoming = pd.DataFrame([
            _etf_row(source=storage.ETF_FALLBACK_MARK + "（不计新增）", shares=999.0),
        ])
        changed = main._write_change_summary(db_path, pd.DataFrame(), incoming)
        assert changed["ETF"]["新增"] == 0
        assert changed["ETF"]["更新"] == 0
        assert changed["ETF"]["未变"] == 0


class TestRetryReusesListingDates:
    def test_retry_passes_known_listing_dates_to_collector(self, monkeypatch, tmp_path):
        db = tmp_path / "market_data.db"
        storage.init_schema(db)
        config = {
            "storage": {"backend": "sqlite", "db_path": str(db)},
            "etf": {
                "codes": ["510050"],
                "listing_dates": {"510050": "2004-12-30"},
                "default_trade_days": 1,
            },
        }
        monitor_cfg = {"history_file": str(tmp_path / "interface_runs.jsonl")}
        seen: dict = {}

        def fake_collect_etf(**kwargs):
            seen.update(kwargs)
            return pd.DataFrame()

        monkeypatch.setattr(main, "collect_etf", fake_collect_etf)
        monkeypatch.setattr(main, "resolve_default_end", lambda _: "2026-11-26")
        monkeypatch.setattr(main, "get_trade_dates", lambda *_a, **_k: ["2026-11-26"])
        monkeypatch.setattr(main, "_save_listing_dates", lambda *_a, **_k: None)
        monkeypatch.setattr(main, "_finish", lambda *_a, **_k: [])

        tasks = [{
            "interface_key": "etf.trade_calendar",
            "interface_name": "ETF交易日历",
            "failed_date": "2026-11-26",
            "params": {},
            "error": "test",
            "status": "failed",
        }]
        rc = main._retry_failed(config, monitor_cfg, tasks)
        assert rc == 0
        assert seen["listing_dates"]["510050"] == "2004-12-30"
