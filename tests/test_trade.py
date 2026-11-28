"""交易数据层测试：原始数据落库 + 计算层口径 + 重算 + 看板装配。"""
from __future__ import annotations

import pandas as pd
import pytest

import storage
from collectors import trade as trade_collector
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


class _FakeMonitor:
    """采集测试用的轻量监控，只记录调用参数。"""

    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)


def _force_mcp_error(monkeypatch, message="你的积分已用完～请购买套餐包"):
    """让涨跌家数的 MCP 主链路稳定失败，以便验证降级路径。"""
    import tools.backfill_trade_eastmoney as eastmoney

    def boom(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(eastmoney, "mcp_call", boom)


class TestMarginPolicy:
    def test_policy_by_date(self):
        # 用权威阶梯（与 DEFAULT_MARGIN_POLICY/config.json 一致）验证五段生效日
        policy = [
            {"effective": "2010-03-31", "value": 50.0},
            {"effective": "2015-11-23", "value": 100.0},
            {"effective": "2023-09-11", "value": 80.0},
            {"effective": "2026-01-19", "value": 100.0},
            {"effective": "2026-11-30", "value": 80.0},
        ]
        assert margin_policy_for(policy, "2015-01-05") == 50.0
        assert margin_policy_for(policy, "2015-11-22") == 50.0
        assert margin_policy_for(policy, "2015-11-23") == 100.0
        assert margin_policy_for(policy, "2020-01-01") == 100.0
        assert margin_policy_for(policy, "2023-09-08") == 100.0
        assert margin_policy_for(policy, "2023-09-11") == 80.0
        assert margin_policy_for(policy, "2026-01-16") == 80.0
        assert margin_policy_for(policy, "2026-01-19") == 100.0
        assert margin_policy_for(policy, "2026-11-24") == 100.0  # 11/30 尚未生效
        assert margin_policy_for(policy, "2026-11-30") == 80.0

    def test_default_policy_matches_authoritative_steps(self):
        from processors.trade import DEFAULT_MARGIN_POLICY
        assert [(s["effective"], s["value"]) for s in DEFAULT_MARGIN_POLICY] == [
            ("2010-03-31", 50.0), ("2015-11-23", 100.0), ("2023-09-11", 80.0),
            ("2026-01-19", 100.0), ("2026-11-30", 80.0),
        ]


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

    def test_congestion_fallback_without_snapshot(self):
        r = compute_trade(None, None, None, None, "2022-07-06",
                          congestion_row={"concentration5": 41.88})
        assert r["concentration5"] == pytest.approx(41.88)
        assert r["source_set"] == "congestion"

    def test_risk_premium_is_source_value(self):
        r = compute_trade(None, None, None, None, "2026-11-26",
                          risk_row={"risk_premium": 5.8757, "ratio": 4.6926})
        assert r["risk_premium"] == pytest.approx(5.8757)
        assert "risk_premium" in r["source_set"].split("+")


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
        assert storage.trade_raw_biz_key("CG", "2026-11-24") == "CG_20261124"
        assert storage.trade_snapshot_biz_key("1", "2026-11-24") == "000001_20261124"

    def test_congestion_raw_upsert_idempotent(self, db_path):
        cg = pd.DataFrame([{"trade_date": "2022-07-06", "concentration5": 41.88,
                            "source": "乐咕", "collected_at": "now"}])
        assert storage.upsert_trade_congestion(db_path, cg, "run1") == 1
        assert storage.upsert_trade_congestion(db_path, cg, "run2") == 1
        got = storage.read_trade_raw(db_path, "congestion")
        assert len(got) == 1
        assert got.iloc[0]["concentration5"] == pytest.approx(41.88)
        assert got.iloc[0]["run_id"] == "run2"

    def test_risk_premium_raw_upsert_idempotent(self, db_path):
        rp = pd.DataFrame([{
            "trade_date": "2026-11-26", "risk_premium": 5.8757, "ratio": 4.6926,
            "close": 3900.55, "ma70": 5.7692, "avg": 5.722,
            "upper1std": 6.2953, "lower1std": 5.1487,
            "upper2std": 6.8686, "lower2std": 4.5754,
            "source": "funddb", "collected_at": "now",
        }])
        assert storage.upsert_trade_risk_premium(db_path, rp, "run1") == 1
        assert storage.upsert_trade_risk_premium(db_path, rp, "run2") == 1
        got = storage.read_trade_raw(db_path, "risk_premium")
        assert len(got) == 1
        assert got.iloc[0]["risk_premium"] == pytest.approx(5.8757)
        assert got.iloc[0]["ratio"] == pytest.approx(4.6926)
        assert got.iloc[0]["run_id"] == "run2"

    def test_recompute_uses_risk_premium(self, db_path):
        storage.upsert_trade_risk_premium(db_path, pd.DataFrame([{
            "trade_date": "2026-11-26", "risk_premium": 5.8757,
            "source": "funddb", "collected_at": "now",
        }]), "run1")
        n = recompute_trade(db_path, config={}, full=True)
        assert n == 1
        df = storage.read_trade(db_path)
        assert df.iloc[0]["risk_premium"] == pytest.approx(5.8757)
        assert "risk_premium" in df.iloc[0]["source_set"].split("+")

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
        assert len(snap["indicators"]) == 11

    def test_recompute_uses_congestion_fallback(self, db_path):
        storage.upsert_trade_exchange(db_path, pd.DataFrame([{
            "trade_date": "2022-07-06", "sse_amount": 5000.0, "szse_amount": 5.9e11,
            "source": "test", "collected_at": "now"}]), "run1")
        storage.upsert_trade_congestion(db_path, pd.DataFrame([{
            "trade_date": "2022-07-06", "concentration5": 41.88,
            "source": "乐咕", "collected_at": "now"}]), "run1")
        n = recompute_trade(db_path, config={}, full=True)
        assert n == 1
        df = storage.read_trade(db_path)
        assert df.iloc[0]["concentration5"] == pytest.approx(41.88)
        assert "congestion" in df.iloc[0]["source_set"].split("+")

    def test_precise_sources_include_breadth_and_congestion(self, monkeypatch):
        calls = []

        def fake(name):
            def inner(*args, **kwargs):
                calls.append(name)
                return pd.DataFrame([{"trade_date": "2026-11-25"}])
            return inner

        monkeypatch.setattr(trade_collector, "_collect_breadth", fake("breadth"))
        monkeypatch.setattr(trade_collector, "_collect_congestion", fake("congestion"))
        monkeypatch.setattr(trade_collector, "_collect_risk_premium", fake("risk_premium"))
        raw = trade_collector.collect_trade(
            sources=["breadth", "congestion", "risk_premium"],
            start_date="2026-11-25", end_date="2026-11-25", monitor=None,
        )
        assert set(raw) == {"breadth", "congestion", "risk_premium"}
        assert calls == ["breadth", "congestion", "risk_premium"]

    def test_mcp_quota_message_is_business_error(self):
        """MCP 返回 message 但无 data 时必须抛错，不能被解析成空数据。"""
        from tools.backfill_trade_eastmoney import _check_mcp_payload

        with pytest.raises(RuntimeError, match="积分已用完"):
            _check_mcp_payload({"message": "你的积分已用完～请购买套餐包"})
        assert _check_mcp_payload({"data": []}) == {"data": []}

    def test_breadth_mcp_error_recorded_as_failed(self, monkeypatch, db_path):
        """涨跌家数采集遇到 MCP 业务错误时，监控记为失败并保留原因。"""
        import importlib
        import json as json_mod

        import akshare as ak

        mcp_mod = importlib.import_module("tools.backfill_trade_eastmoney")

        class FakeResp:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self.payload

        def fake_post(self, url, headers=None, json=None, timeout=None):
            if (json or {}).get("method") == "initialize":
                return FakeResp({"result": {}})
            inner = json_mod.dumps({"message": "你的积分已用完～请购买套餐包"}, ensure_ascii=False)
            return FakeResp({"result": {"content": [{"text": inner}]}})

        # 保留真实 mcp_call，只替换底层 HTTP 响应，验证业务错误识别链路
        monkeypatch.setattr(mcp_mod.requests.Session, "post", fake_post)
        # 降级路径也不允许访问真实行情，保证断言稳定
        monkeypatch.setattr(ak, "stock_zh_a_spot", lambda: (_ for _ in ()).throw(
            RuntimeError("新浪快照不可用")))

        mon = _FakeMonitor()
        df = trade_collector._collect_breadth(
            "2026-11-25", "2026-11-25", monitor=mon,
            collected_at="2026-11-26 21:18:36", db_path=db_path,
        )
        assert df.empty
        assert len(mon.records) == 1
        assert mon.records[0]["status"] == "failed"
        assert "积分已用完" in mon.records[0]["error"]

    def test_breadth_fallback_uses_stored_snapshot_and_logs_degrade(self, monkeypatch, db_path):
        """MCP 失败时优先从已落库快照派生涨跌家数，并保留降级证据。"""
        day = "2026-11-25"
        snap = _snapshot(100).assign(trade_date=day, source="test", collected_at="now")
        assert storage.upsert_trade_snapshot(db_path, snap, "run1") == 100
        _force_mcp_error(monkeypatch)

        mon = _FakeMonitor()
        df = trade_collector._collect_breadth(
            day, day, monitor=mon, collected_at="2026-11-26 21:30:00", db_path=db_path,
        )
        assert len(df) == 1
        assert df.iloc[0]["trade_date"] == day
        assert df.iloc[0]["up_count"] == 40
        assert df.iloc[0]["down_count"] == 50
        assert df.iloc[0]["flat_count"] == 10
        assert "MCP失败降级派生" in df.iloc[0]["source"]
        assert len(mon.records) == 1
        rec = mon.records[0]
        assert rec["status"] == "success"
        assert rec["interface_name"] == "涨跌家数(新浪降级)"
        assert rec["params"]["degraded_from"] == "mx_comprehensive_finance_data"
        assert rec["rows"] == 1
        assert "积分已用完" in rec["error"]

    def test_breadth_fallback_does_not_overwrite_existing_raw(self, monkeypatch, db_path):
        """已落库的涨跌家数原数据不被降级结果覆盖。"""
        day = "2026-11-25"
        snap = _snapshot(100).assign(trade_date=day, source="test", collected_at="now")
        storage.upsert_trade_snapshot(db_path, snap, "run1")
        original = pd.DataFrame([{
            "trade_date": day, "up_count": 1, "down_count": 2, "flat_count": 3,
            "source": "eastmoney", "collected_at": "old",
        }])
        assert storage.upsert_trade_breadth(db_path, original, "run1") == 1
        _force_mcp_error(monkeypatch)

        mon = _FakeMonitor()
        df = trade_collector._collect_breadth(
            day, day, monitor=mon, collected_at="2026-11-26 21:30:00", db_path=db_path,
        )
        assert df.empty
        got = storage.read_trade_raw(db_path, "breadth", start=day, end=day)
        assert len(got) == 1
        assert got.iloc[0]["up_count"] == 1
        assert got.iloc[0]["down_count"] == 2
        assert got.iloc[0]["flat_count"] == 3
        assert got.iloc[0]["source"] == "eastmoney"
        assert mon.records[0]["status"] == "success"
        assert mon.records[0]["interface_name"] == "涨跌家数(已存在跳过)"

    def test_breadth_fallback_realtime_rejects_history(self, monkeypatch, db_path):
        """实时新浪快照只能降级当日，不得跨日补历史。"""
        import akshare as ak

        _force_mcp_error(monkeypatch)
        monkeypatch.setattr(ak, "stock_zh_a_spot", lambda: pd.DataFrame({
            "涨跌幅": [1.0, -1.0, 0.0],
        }))
        monkeypatch.setattr(trade_collector, "_sina_snapshot_trade_date", lambda: "2026-11-26")

        mon = _FakeMonitor()
        df = trade_collector._collect_breadth(
            "2020-01-02", "2020-01-02", monitor=mon,
            collected_at="2026-11-26 21:30:00", db_path=db_path,
        )
        assert df.empty
        assert mon.records[0]["status"] == "failed"
        assert "历史日期禁止跨日降级" in mon.records[0]["error"]

    def test_breadth_fallback_realtime_accepts_current_range(self, monkeypatch, db_path):
        """请求区间包含实时快照交易日时，允许从新浪快照派生。"""
        import akshare as ak

        _force_mcp_error(monkeypatch)
        monkeypatch.setattr(ak, "stock_zh_a_spot", lambda: pd.DataFrame({
            "涨跌幅": [1.0, -1.0, 0.0],
        }))
        monkeypatch.setattr(trade_collector, "_sina_snapshot_trade_date", lambda: "2026-11-26")

        mon = _FakeMonitor()
        df = trade_collector._collect_breadth(
            "2026-11-26", "2026-11-26", monitor=mon,
            collected_at="2026-11-26 21:30:00", db_path=db_path,
        )
        assert len(df) == 1
        assert df.iloc[0]["up_count"] == 1
        assert df.iloc[0]["down_count"] == 1
        assert df.iloc[0]["flat_count"] == 1
        assert mon.records[0]["status"] == "success"
        assert mon.records[0]["interface_name"] == "涨跌家数(新浪降级)"
        assert "积分已用完" in mon.records[0]["error"]

    def test_clear_tables_protect_raw_not_derived(self):
        assert "trade_exchange_raw" not in storage.CLEAR_TABLES
        assert "trade_congestion_raw" not in storage.CLEAR_TABLES
        assert "trade_observation" not in storage.CLEAR_TABLES  # 计算表不随清理删除
        assert all(table not in storage.CLEAR_TABLES for table in storage.COLLECTED_DATA_TABLES)


class TestRiskPremiumCollector:
    """沪深300风险溢价率直采测试（funddb / 韭圈儿同源接口）。"""

    @staticmethod
    def _series_payload():
        return {"tab": "fed", "name": "沪深300", "series": [
            {"date": "2026-11-24", "value": 5.7221, "ratio": 4.60, "close": 3963.82,
             "ma70": 5.70, "avg": 5.71, "upper1std": 6.29, "lower1std": 5.14,
             "upper2std": 6.86, "lower2std": 4.57},
            {"date": "2026-11-25", "value": 5.8757, "ratio": 4.6926, "close": 3898.24,
             "ma70": 5.7692, "avg": 5.722, "upper1std": 6.2953, "lower1std": 5.1487,
             "upper2std": 6.8686, "lower2std": 4.5754},
            {"date": "2026-11-26", "value": 5.9, "ratio": 4.7, "close": 3900.55},
        ]}

    def test_collect_filters_range_and_keeps_source_fields(self, monkeypatch):
        import requests

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return self._series_payload()

        # 绑定实例方法供 monkeypatch 使用
        FakeResp._series_payload = staticmethod(self._series_payload)
        monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResp())

        mon = _FakeMonitor()
        df = trade_collector._collect_risk_premium(
            "2026-11-25", "2026-11-25", monitor=mon,
            collected_at="2026-11-26 23:00:00", retries=1, retry_interval=0,
        )
        assert len(df) == 1
        row = df.iloc[0]
        assert row["trade_date"] == "2026-11-25"
        assert row["risk_premium"] == pytest.approx(5.8757)
        assert row["ratio"] == pytest.approx(4.6926)
        assert row["close"] == pytest.approx(3898.24)
        assert row["ma70"] == pytest.approx(5.7692)
        assert row["avg"] == pytest.approx(5.722)
        assert row["upper1std"] == pytest.approx(6.2953)
        assert row["lower1std"] == pytest.approx(5.1487)
        assert row["upper2std"] == pytest.approx(6.8686)
        assert row["lower2std"] == pytest.approx(4.5754)
        assert "韭圈儿" in row["source"]
        assert len(mon.records) == 1
        assert mon.records[0]["status"] == "success"
        assert mon.records[0]["interface_key"] == "trade.risk_premium"
        assert mon.records[0]["rows"] == 1

    def test_collect_failure_recorded_as_failed(self, monkeypatch):
        import requests

        def boom(*args, **kwargs):
            raise RuntimeError("funddb unavailable")

        monkeypatch.setattr(requests, "get", boom)
        mon = _FakeMonitor()
        df = trade_collector._collect_risk_premium(
            "2026-11-25", "2026-11-25", monitor=mon,
            collected_at="2026-11-26 23:00:00", retries=1, retry_interval=0,
        )
        assert df.empty
        assert len(mon.records) == 1
        assert mon.records[0]["status"] == "failed"
        assert "funddb unavailable" in mon.records[0]["error"]

    def test_catalog_exposes_direct_source(self):
        codes = {it["code"] for it in trade_collector.catalog()}
        assert "risk_premium" in codes
        item = next(it for it in trade_collector.catalog() if it["code"] == "risk_premium")
        assert item["unit"] == "%"
        assert item["granularity"] == "交易日"
        assert "equity-bond" in item["iface"]


class TestTradeCharts:
    def test_build_two_charts(self):
        df = pd.DataFrame([{
            "trade_date": "2026-11-24", "total_amount": 14000.0, "concentration5": 47.5,
            "margin_balance": 21290.0, "margin_ratio": 247.0, "up_count": 1951,
            "down_count": 3498, "debt_investors": 1968001.0,
        }])
        charts = build_trade_charts(df)
        # 风险溢价率图3已迁至「估值」维度，日交易仅保留图1/图2。
        assert [c["id"] for c in charts] == ["trade_market", "trade_margin"]
        # _chart 会过滤无数据的序列：图1 四序列中成交额/集中度/涨跌家数均有值
        assert {s["name"] for s in charts[0]["series"]} == {
            "全A成交额", "成交集中度(前5%)", "上涨家数", "下跌家数"}
        # 图2 仅有值的三序列保留（维持担保比例/两融余额/有负债投资者数）
        assert {s["name"] for s in charts[1]["series"]} == {
            "平均维持担保比例", "两融余额", "有负债投资者数"}
        # 成交额从亿元换算为万亿元并保留两位小数：14000 亿元 -> 1.4 万亿元。
        assert charts[0]["series"][0]["data"] == [["2026-11-24", 1.4]]
        assert charts[0]["layout"] == "trade_market_split"

    def test_empty_df(self):
        charts = build_trade_charts(pd.DataFrame())
        assert len(charts) == 2 and all(c["series"] == [] for c in charts)


def test_indicator_codes():
    assert set(INDICATORS) == {
        "TOTAL_AMOUNT", "CONCENTRATION5", "MARGIN_RATIO", "MARGIN_FINANCING_RATIO",
        "MARGIN_BALANCE", "FINANCING_BALANCE", "SECURITIES_BALANCE",
        "FINANCING_TO_FLOAT_MV", "FINANCING_BUY_PCT", "DEBT_INVESTORS",
        "RISK_PREMIUM",
    }
