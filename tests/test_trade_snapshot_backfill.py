"""全A快照历史补数：代码迁移映射与 MCP 批量返回解析。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from backfill_trade_snapshot import (  # noqa: E402
    _number, parse_kline_row, parse_snapshot_response, to_mcp_code,
)


class TestSnapshotBackfillParse:
    def test_market_and_bse_code_migration(self):
        assert to_mcp_code("sh600000") == "600000.SH"
        assert to_mcp_code("sz000001") == "000001.SZ"
        assert to_mcp_code("bj920000") == "920000.BJ"
        assert to_mcp_code("bj430017") == "920017.BJ"
        assert to_mcp_code("bj830779") == "920779.BJ"

    def test_number_units(self):
        assert _number("35.84万股", "volume") == 358400
        assert _number("336.8万", "amount") == 3368000
        assert _number("-5.992%", "pct_change") == -5.992
        assert _number("9.1元", "last_price") == 9.1

    def test_parse_kline_row_converts_volume_hands_to_shares(self):
        row = "2026-11-25,9.68,9.1,9.68,9.01,3584,3367633.16,6.92,-5.99,-0.58,0.62"
        got = parse_kline_row(row, "2026-11-25")
        assert got == {
            "last_price": 9.1, "volume": 358400,
            "amount": 3367633.16, "pct_change": -5.99,
        }
        assert parse_kline_row(row, "2026-11-24") is None

    def test_parse_batch_response_maps_old_and_new_codes(self):
        data = {"data": [{
            "columns": ["2026-11-25(日)", "安徽凤凰(920000.BJ)", "星昊医药(920017.BJ)"],
            "items": [
                ["成交量", "35.84万股", "299.5万股"],
                ["成交额", "336.8万", "3462万"],
                ["涨跌幅", "-5.992%", "1.333%"],
                ["收盘价", "9.1元", "11.4元"],
            ],
        }]}
        code_map = {
            "920000.BJ": [{"code": "bj920000", "name": "安徽凤凰"}],
            "920017.BJ": [{"code": "bj920017", "name": "星昊医药"},
                          {"code": "bj430017", "name": "星昊医药"}],
        }
        got = parse_snapshot_response(data, code_map)
        assert got["920000.BJ"]["volume"] == 358400
        assert got["920000.BJ"]["amount"] == 3368000
        assert got["920000.BJ"]["pct_change"] == -5.992
        assert got["920000.BJ"]["last_price"] == 9.1
        assert got["920017.BJ"]["volume"] == 2995000
        assert got["920017.BJ"]["last_price"] == 11.4
