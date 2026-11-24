"""交易数据层测试：原始数据落库 + 计算层口径 + 重算 + 看板装配。"""
from __future__ import annotations

import pandas as pd
import pytest

import storage
from processors.dashboard import build_trade_charts
from processors.trade import (
    INDICATORS, compute_trade, latest_snapshot, margin_policy_for, recompute_trade,
)


def _snapshot(n=100):
    """构造个股快照：成交额递减，涨跌幅可统计。"""
    rows = []
    for i in range(n):
        rows.append({
            "code": str(600000 + i).zfill(6), "name": f"股{i}",
            "pct_change": 1.0 if i < 40 else (-1.0 if i < 90 else 0.0),
            "volume": 1e6, "amount": float(10_000_000 - i * 50_000),
        })
    return pd.DataFrame(rows)


class TestMarginPolicy:
    def test_policy_by_date(self):
        policy = [
            {"effective": "2015-11-01", "value": 100.0},
            {"effective": "2023-08-28", "value": 80.0},
        ]
        assert margin_policy_for(policy, "2020-01-01") == 100.0
        assert margin_policy_for(policy, "2023-08-28") == 80.0
        assert margin_policy_for(policy, "2026-11-24") == 80.0
        assert margin_policy_for(policy, "2010-01-01") is None


class TestComputeTrade:
    def test_all_formulas(self):
        ex = {"sse_amount": 7000.0, "szse_amount": 7.0e11,
              "sse_float_mv": 500000.0, "szse_float_mv": 2.9e13}
        mg = {"margin_ratio": 247.0, "financing_balance": 21000.0,
              "securities_balance": 290.0, "financing_buy": 1200.0,
              "debt_investors": 1968001.0}
        policy = [{"effective": "2023-08-28", "value": 80.0}]
        r = compute_trade(ex, mg, _snapshot(), policy, "2026-11-24")
        assert r["total_amount"] == pytest.approx(7000 + 7000)  # 深 7e11/1e8=7000
        assert r["margin_balance"] == pytest.approx(21290.0)
        assert r["margin_ratio"] == 247.0
        assert r["margin_financing_ratio"] == 80.0
        assert r["debt_investors"] == 1968001.0
        # 集中度：前5%成交额 / 总成交额
        amt = _snapshot()["amount"].sort_values(ascending=False)
        assert r["concentration5"] == pytest.approx(
            amt.head(5).sum() / amt.sum() * 100, abs=1e-3)
        # 融资余额占流通市值比
        assert r["financing_to_float_mv"] == pytest.approx(
            21000.0 / (500000.0 + 2.9e13 / 1e8) * 100, abs=1e-3)
        # 融资买入额占比
        assert r["financing_buy_pct"] == pytest.approx(1200.0 / 14000.0 * 100, abs=1e-3)
        # 涨跌家数
        assert r["up_count"] == 40 and r["down_count"] == 50 and r["flat_count"] == 10
        assert set(r["source_set"].split("+")) == {"exchange", "margin", "snapshot"}

    def test_missing_sources_yield_none(self):
        r = compute_trade(None, None, None, None, "2026-11-24")
        assert r["total_amount"] is None
        assert r["concentration5"] is None
        assert r["up_count"] is None
        assert r["source_set"] == ""


class TestTradeStorage:
    def test_raw_upsert_idempotent(self, db_path):
        ex = pd.DataFrame([{"trade_date": "2026-11-24", "sse_amount": 7000.0,
                            "szse_amount": 7e11, "source": "test", "collected_at": "now"}])
        assert storage.upsert_trade_exchange(db_path, ex, "run1") == 1
        assert storage.upsert_trade_exchange(db_path, ex, "run2") == 1  # 幂等覆盖
        got = storage.read_trade_raw(db_path, "exchange")
        assert len(got) == 1 and got.iloc[0]["run_id"] == "run2"

    def test_trade_biz_key(self):
        assert storage.trade_biz_key("2026-11-24") == "20261124"
        assert storage.trade_raw_biz_key("EX", "2026-11-24") == "EX_20261124"
        assert storage.trade_snapshot_biz_key("1", "2026-11-24") == "000001_20261124"

    def test_recompute_and_read(self, db_path):
        storage.upsert_trade_exchange(db_path, pd.DataFrame([{
            "trade_date": "2026-11-24", "sse_amount": 7000.0, "szse_amount": 7e11,
            "sse_float_mv": 500000.0, "szse_float_mv": 2.9e13,
            "source": "test", "collected_at": "now"}]), "run1")
        storage.upsert_trade_snapshot(db_path, _snapshot(20).assign(
            trade_date="2026-11-24", source="test", collected_at="now"), "run1")
        config = {"trade_policy": {"margin_ratio": [{"effective": "2023-08-28", "value": 80.0}]}}
        n = recompute_trade(db_path, config=config, full=True)
        assert n == 1
        df = storage.read_trade(db_path)
        assert len(df) == 1
        assert df.iloc[0]["total_amount"] == pytest.approx(14000.0)
        assert df.iloc[0]["concentration5"] > 0
        snap = latest_snapshot(db_path)
        assert snap["coverage"]["trade_days"] == 1
        assert len(snap["indicators"]) == 10

    def test_clear_tables_include_raw_not_derived(self):
        assert "trade_exchange_raw" in storage.CLEAR_TABLES
        assert "trade_observation" not in storage.CLEAR_TABLES  # 计算表不随清理删除


class TestTradeCharts:
    def test_build_two_charts(self):
        df = pd.DataFrame([{
            "trade_date": "2026-11-24", "total_amount": 14000.0, "concentration5": 47.5,
            "margin_balance": 21290.0, "margin_ratio": 247.0, "up_count": 1951,
            "down_count": 3498, "debt_investors": 1968001.0,
        }])
        charts = build_trade_charts(df)
        assert [c["id"] for c in charts] == ["trade_market", "trade_margin"]
        # _chart 会过滤无数据的序列：图1 四序列中成交额/集中度/涨跌家数均有值
        assert {s["name"] for s in charts[0]["series"]} == {
            "全A成交额", "成交集中度(前5%)", "上涨家数", "下跌家数"}
        # 图2 仅有值的三序列保留（维持担保比例/两融余额/有负债投资者数）
        assert {s["name"] for s in charts[1]["series"]} == {
            "平均维持担保比例", "两融余额", "有负债投资者数"}
        assert charts[0]["series"][0]["data"] == [["2026-11-24", 14000.0]]

    def test_empty_df(self):
        charts = build_trade_charts(pd.DataFrame())
        assert len(charts) == 2 and all(c["series"] == [] for c in charts)


def test_indicator_codes():
    assert set(INDICATORS) == {
        "TOTAL_AMOUNT", "CONCENTRATION5", "MARGIN_RATIO", "MARGIN_FINANCING_RATIO",
        "MARGIN_BALANCE", "FINANCING_BALANCE", "SECURITIES_BALANCE",
        "FINANCING_TO_FLOAT_MV", "FINANCING_BUY_PCT", "DEBT_INVESTORS",
    }
