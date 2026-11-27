"""实时快照日期锚定：禁止把盘中值错标到上一交易日。"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from collectors import trade as trade_collector
from collectors.trade import _snapshot_trade_date


class TestSnapshotTradeDate:
    def test_full_date_timestamp_wins(self):
        df = pd.DataFrame({"时间戳": ["2026-11-25 15:30:01", "2026-11-25 15:30:02"]})
        got = _snapshot_trade_date(df, datetime(2026, 11, 26, 10, 30))
        assert got == "2026-11-25"

    def test_intraday_pure_time_never_backfills_previous_day(self):
        df = pd.DataFrame({"时间戳": ["10:28:59", "10:29:00"]})
        assert _snapshot_trade_date(df, datetime(2026, 11, 26, 10, 29)) == ""

    def test_after_close_pure_time_uses_today(self):
        df = pd.DataFrame({"时间戳": ["15:30:01", "15:30:01"]})
        assert _snapshot_trade_date(df, datetime(2026, 11, 26, 15, 30)) == "2026-11-26"

    def test_weekend_after_close_still_rejected(self):
        df = pd.DataFrame({"时间戳": ["15:30:01", "15:30:01"]})
        assert _snapshot_trade_date(df, datetime(2026, 11, 28, 15, 30)) == ""


class _FakeMonitor:
    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)


def test_collect_snapshot_skips_before_close_without_failure(monkeypatch):
    """盘中不调用快照接口，也不产出失败监控；避免错标到上一交易日。"""
    monkeypatch.setattr(trade_collector, "_snapshot_market_closed", lambda now=None: False)
    mon = _FakeMonitor()

    out = trade_collector._collect_snapshot(
        "2026-11-26", "2026-11-26", retries=1, retry_interval=0,
        monitor=mon, collected_at="2026-11-26 12:54:21",
    )

    assert out.empty
    assert len(mon.records) == 1
    assert mon.records[0]["status"] == "success"
    assert mon.records[0]["params"]["skipped"] is True
    assert "不回退" in mon.records[0]["params"]["reason"]
