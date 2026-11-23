"""数据看板：宏观 7 图 + ETF 标签变化 2 图的装配与接口测试。"""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import storage
import web_app
from processors.dashboard import build_etf_charts, build_macro_charts


@pytest.fixture
def client(base, config, config_file, db_path, macro_df, etf_df, run_records, monkeypatch):
    """与 test_api.py 一致的隔离测试客户端。"""
    storage.init_schema(db_path)
    storage.upsert_macro(db_path, macro_df, "test_run")
    storage.upsert_etf(db_path, etf_df, "test_run")
    storage.append_interface_runs(db_path, run_records)
    storage.save_etf_config(db_path, config["etf"])
    monkeypatch.setattr(web_app, "CONFIG_PATH", config_file)
    monkeypatch.setattr(web_app, "BASE_DIR", base)
    monkeypatch.setattr(web_app, "get_fund_name_map", lambda: {})
    with TestClient(web_app.app) as c:
        yield c


def _derived_rows(codes: list[str], dates: list[str], value: float = 1.0) -> pd.DataFrame:
    rows = []
    for date in dates:
        for code in codes:
            rows.append({
                "code": code, "name": code, "date": date, "value": value,
                "unit": "", "formula": "", "grp": "A", "source": "test",
                "computed_at": "2026-11-23 00:00:00",
            })
    return pd.DataFrame(rows)


def _macro_rows() -> pd.DataFrame:
    rows = []
    for date in ("2026-08", "2026-09", "2026-10"):
        rows.append({
            "指标代码": "CN_GDP", "指标名称": "GDP", "日期": date,
            "数值": 100.0, "单位": "亿元", "同比(%)": 4.5,
            "环比(%)": None, "数据源": "NBS", "采集时间": "test", "run_id": "test",
        })
        rows.append({
            "指标代码": "M2", "指标名称": "M2", "日期": date,
            "数值": 3_000_000.0, "单位": "亿元", "同比(%)": 7.0,
            "环比(%)": None, "数据源": "PBC", "采集时间": "test", "run_id": "test",
        })
        rows.append({
            "指标代码": "A_SHARE_MC", "指标名称": "A股总市值", "日期": date,
            "数值": 1_000_000.0, "单位": "亿元", "同比(%)": None,
            "环比(%)": None, "数据源": "test", "采集时间": "test", "run_id": "test",
        })
    return pd.DataFrame(rows)


def _index_rows() -> pd.DataFrame:
    return pd.DataFrame([
        {"指数代码": "000001", "指数名称": "上证指数", "交易日期": "2026-10-30",
         "开盘价": 3400, "最高价": 3420, "最低价": 3390, "收盘价": 3410,
         "成交量": 1, "涨跌幅(%)": 0.1, "数据源": "test", "采集时间": "test", "run_id": "test"},
        {"指数代码": "000001", "指数名称": "上证指数", "交易日期": "2026-10-31",
         "开盘价": 3410, "最高价": 3540, "最低价": 3410, "收盘价": 3500,
         "成交量": 1, "涨跌幅(%)": 2.6, "数据源": "test", "采集时间": "test", "run_id": "test"},
    ])


class TestMacroDashboard:
    def setup_method(self):
        self.codes = [
            "TRADE_YOY", "RETAIL_SALES_YOY", "CPI_YOY", "PPI_YOY", "CPI_PPI_GAP",
            "SFR_STOCK_YOY", "RMB_LOAN_NEW", "M1_YOY", "M2_YOY", "M1_M2_GAP",
            "A_SHARE_MC_M2", "HH_DEPOSIT_NEW_YOY", "GOLD_NEW_TON",
        ]
        self.dates = ["2026-08", "2026-09", "2026-10"]
        self.charts = build_macro_charts(
            _derived_rows(self.codes, self.dates), _macro_rows(), _index_rows(), months=3)

    def test_seven_charts_in_order(self):
        assert len(self.charts) == 7
        assert [c["id"] for c in self.charts] == [
            "gdp_trade", "price", "credit", "money_gap",
            "m2_market", "hh_deposit", "gold",
        ]
        assert self.charts[0]["title"].startswith("图1")
        assert self.charts[-1]["title"].startswith("图7")

    def test_chart_series_and_axes(self):
        by_id = {c["id"]: c for c in self.charts}
        assert [s["name"] for s in by_id["price"]["series"]] == [
            "社零同比", "CPI同比", "PPI同比", "CPI-PPI剪刀差"]
        credit = by_id["credit"]
        assert len(credit["y_axes"]) == 2
        assert credit["series"][1]["y_axis_index"] == 1
        assert credit["series"][1]["type"] == "bar"
        m2_chart = by_id["m2_market"]
        assert len(m2_chart["y_axes"]) == 3
        assert [s["name"] for s in m2_chart["series"]] == [
            "M2", "A股总市值", "A股总市值/M2", "上证指数"]

    def test_m2_converted_and_index_month_end(self):
        m2_chart = next(c for c in self.charts if c["id"] == "m2_market")
        series = {s["name"]: s["data"] for s in m2_chart["series"]}
        assert series["M2"][-1] == ["2026-10", 300.0]
        assert series["A股总市值"][-1] == ["2026-10", 100.0]
        # 日频指数取月末最后一个交易日收盘
        assert series["上证指数"] == [["2026-10", 3500.0]]

    def test_months_cutoff(self):
        all_charts = build_macro_charts(
            _derived_rows(self.codes, ["2025-01", "2026-08", "2026-10"]),
            _macro_rows(), _index_rows(), months=3)
        s = next(c for c in all_charts if c["id"] == "gold")["series"][0]
        assert [d for d, _ in s["data"]] == ["2026-08", "2026-10"]
        full = build_macro_charts(
            _derived_rows(self.codes, ["2025-01", "2026-08", "2026-10"]),
            _macro_rows(), _index_rows(), months=0)
        s_full = next(c for c in full if c["id"] == "gold")["series"][0]
        assert [d for d, _ in s_full["data"]] == ["2025-01", "2026-08", "2026-10"]


class TestEtfDashboard:
    def test_aggregates_by_tag_and_months(self):
        df = pd.DataFrame([
            {"基金代码": "510050", "交易日期": "2026-10-15", "份额变化(亿份)": 9.0, "规模变化(亿元)": 10.0},
            {"基金代码": "510050", "交易日期": "2026-11-18", "份额变化(亿份)": 1.0, "规模变化(亿元)": 2.0},
            {"基金代码": "510300", "交易日期": "2026-11-18", "份额变化(亿份)": 3.0, "规模变化(亿元)": 4.0},
            {"基金代码": "510050", "交易日期": "2026-11-19", "份额变化(亿份)": 5.0, "规模变化(亿元)": 6.0},
            {"基金代码": "518880", "交易日期": "2026-11-19", "份额变化(亿份)": -0.5, "规模变化(亿元)": -1.5},
            {"基金代码": "510050", "交易日期": "2026-11-20", "份额变化(亿份)": 7.0, "规模变化(亿元)": 8.0},
        ])
        tag_map = {"510050": ["宽基", "汇金"], "510300": ["汇金"], "518880": ["黄金"]}
        # 近一年（months=12）覆盖全部；近 1 个月只保留 2026-11
        charts = build_etf_charts(df, tag_map, months=1)
        assert [c["id"] for c in charts] == ["etf_huijin", "etf_gold"]
        hj = charts[0]
        assert hj["etf_count"] == 2
        assert hj["series"][0]["data"] == [
            ["2026-11-18", 4.0], ["2026-11-19", 5.0], ["2026-11-20", 7.0]]
        assert hj["series"][1]["data"] == [
            ["2026-11-18", 6.0], ["2026-11-19", 6.0], ["2026-11-20", 8.0]]
        gold = charts[1]
        assert gold["series"][0]["data"] == [["2026-11-19", -0.5]]

        full = build_etf_charts(df, tag_map, months=0)
        assert full[0]["series"][0]["data"][0] == ["2026-10-15", 9.0]

    def test_no_tags_returns_empty_series(self):
        df = pd.DataFrame([
            {"基金代码": "510050", "交易日期": "2026-11-20",
             "份额变化(亿份)": 1.0, "规模变化(亿元)": 2.0},
        ])
        charts = build_etf_charts(df, {}, months=12)
        assert len(charts) == 2
        assert all(c["etf_count"] == 0 for c in charts)
        assert all(not s["data"] for c in charts for s in c["series"])


class TestDashboardApi:
    def test_dashboard_endpoint(self, client, db_path, config):
        storage.upsert_derived(db_path, _derived_rows(["GOLD_NEW_TON", "M2_YOY"], ["2026-10"]))
        storage.upsert_index(db_path, _index_rows(), "test")
        etf_cfg = dict(config["etf"])
        etf_cfg["tags"] = {"510050": ["汇金"], "159915": ["黄金"]}
        storage.save_etf_config(db_path, etf_cfg)

        data = client.get("/api/dashboard?months=12&etf_months=12").json()
        assert data["months"] == 12 and data["etf_months"] == 12
        assert len(data["macro"]["charts"]) == 7
        assert len(data["etf"]["charts"]) == 2
        assert data["etf"]["charts"][0]["etf_count"] == 1
        assert "generated_at" in data
