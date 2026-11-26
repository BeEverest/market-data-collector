"""实时快照日期锚定：禁止把盘中值错标到上一交易日。"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

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
