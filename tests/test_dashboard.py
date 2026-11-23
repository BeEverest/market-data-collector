"""数据看板：宏观 7 图 + ETF 标签变化 2 图的装配与接口测试。"""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import storage
import web_app
from processors.dashboard import (
    build_etf_charts, build_etf_daily_detail, build_macro_charts,
)


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
        assert len(self.charts) == 8
        assert [c["id"] for c in self.charts] == [
            "gdp_trade", "price", "credit", "money_gap",
            "m2_market", "hh_deposit", "gold", "loan_new_chart",
        ]
        assert self.charts[0]["title"].startswith("图1")
        assert self.charts[-1]["title"].startswith("图8")
        # 新增图8：人民币贷款当月新增，柱状单轴
        loan = self.charts[-1]
        assert loan["title"].endswith("人民币贷款当月新增")
        assert loan["series"][0]["name"] == "人民币贷款当月新增"
        assert loan["series"][0]["type"] == "bar"
        assert len(loan["y_axes"]) == 1 and loan["y_axes"][0]["name"] == "当月新增 (亿元)"

    def test_chart_series_and_axes(self):
        by_id = {c["id"]: c for c in self.charts}
        # 图1 GDP 用次坐标轴（右轴），进出口同比保持左轴
        gdp = by_id["gdp_trade"]
        assert len(gdp["y_axes"]) == 2
        assert gdp["y_axes"][1]["name"] == "GDP同比 (%)"
        assert gdp["series"][0]["name"] == "GDP同比" and gdp["series"][0]["y_axis_index"] == 1
        assert gdp["series"][1]["name"] == "进出口同比" and gdp["series"][1]["y_axis_index"] == 0
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
        # 汇金原图(按交易日) + 汇金细分(按ETF净流入降序) + 黄金原图 + 汇金累计份额变化
        # 近1月只保留 2026-11；510050 规模变化 = 2+6+8 = 16，510300 = 4，合计 20
        charts = build_etf_charts(df, tag_map, months=1)
        assert [c["id"] for c in charts] == [
            "etf_huijin", "etf_huijin_detail", "etf_huijin_cumulative", "etf_gold"]
        hj = charts[0]
        assert hj.get("layout") is None  # 汇金原图为默认时间序列
        assert hj["etf_count"] == 2
        assert hj["series"][0]["data"] == [
            ["2026-11-18", 4.0], ["2026-11-19", 5.0], ["2026-11-20", 7.0]]
        assert hj["series"][1]["data"] == [
            ["2026-11-18", 6.0], ["2026-11-19", 6.0], ["2026-11-20", 8.0]]
        detail = charts[1]
        assert detail["layout"] == "etf_rank"
        assert detail["group"] == "huijin_detail"
        assert detail["total_cap_change"] == 20.0
        assert detail["total_share_change"] == 16.0
        assert detail["series"][0]["data"] == [["510050", 16.0], ["510300", 4.0]]  # 降序
        # 点击柱子展开每日明细所需的元数据：代码/简称与柱子同序，起始月与图表同口径
        assert detail["detail_start"] == "2026-11"
        assert detail["rank_meta"] == [
            {"code": "510050", "name": "510050"}, {"code": "510300", "name": "510300"}]
        # 累计份额变化在细分图之后、黄金之前（索引2）：逐日累加 4+5+7 = 16
        cum = charts[2]
        assert cum["id"] == "etf_huijin_cumulative"
        gold = charts[3]
        assert gold["id"] == "etf_gold"
        assert gold["series"][0]["data"] == [["2026-11-19", -0.5]]
        assert cum["series"][0]["data"] == [
            ["2026-11-18", 4.0], ["2026-11-19", 9.0], ["2026-11-20", 16.0]]
        assert cum["total_cap_change"] == 16.0

        full = build_etf_charts(df, tag_map, months=0)
        # 全量区间：510050 = 10+2+6+8 = 26；510300 = 4
        assert full[0]["series"][0]["data"][0] == ["2026-10-15", 9.0]
        assert full[1]["series"][0]["data"] == [["510050", 26.0], ["510300", 4.0]]
        assert full[1]["total_cap_change"] == 30.0
        assert full[1]["detail_start"] == ""  # 全部区间不设起始月

    def test_etf_rank_sort_with_negative_and_name(self):
        """净流入有正有负时按降序，X 轴标签优先用基金简称。"""
        df = pd.DataFrame([
            {"基金代码": "510050", "基金简称": "上证50ETF", "交易日期": "2026-11-19",
             "份额变化(亿份)": 1.0, "规模变化(亿元)": -3.0},
            {"基金代码": "510300", "基金简称": "沪深300ETF", "交易日期": "2026-11-19",
             "份额变化(亿份)": 2.0, "规模变化(亿元)": 8.0},
            {"基金代码": "159915", "基金简称": "创业板ETF", "交易日期": "2026-11-19",
             "份额变化(亿份)": 0.5, "规模变化(亿元)": 5.0},
        ])
        tag_map = {c: ["汇金"] for c in ["510050", "510300", "159915"]}
        detail = build_etf_charts(df, tag_map, months=1)[1]  # 细分图在索引1
        data = detail["series"][0]["data"]
        assert data == [["沪深300ETF", 8.0], ["创业板ETF", 5.0], ["上证50ETF", -3.0]]
        assert detail["total_cap_change"] == 10.0  # 8+5-3
        assert [m["code"] for m in detail["rank_meta"]] == ["510300", "159915", "510050"]
        assert [m["name"] for m in detail["rank_meta"]] == [d[0] for d in data]

    def test_huijin_detail_prefers_net_inflow_column(self):
        """有「净流入(亿元)」列时细分图用申赎口径，不用规模变化。"""
        df = pd.DataFrame([
            {"基金代码": "510050", "基金简称": "上证50ETF", "交易日期": "2026-11-20",
             "份额变化(亿份)": 10.0, "规模变化(亿元)": 42.7, "净流入(亿元)": 35.7},
        ])
        tag_map = {"510050": ["汇金"]}
        detail = build_etf_charts(df, tag_map, months=1)[1]
        assert detail["total_cap_change"] == 35.7  # 净流入而非规模变化 42.7
        assert detail["series"][0]["data"] == [["上证50ETF", 35.7]]

    def test_etf_chart_flow_uses_net_inflow(self):
        """原图第二条线统一为申赎净流入：有净流入列用申赎口径，标题/Y轴同步改名；无该列回退规模变化。"""
        df = pd.DataFrame([
            {"基金代码": "510050", "基金简称": "上证50ETF", "交易日期": "2026-11-20",
             "份额变化(亿份)": 10.0, "规模变化(亿元)": 42.7, "净流入(亿元)": 35.7},
        ])
        chart = build_etf_charts(df, {"510050": ["汇金"]}, months=1)[0]
        assert "申赎净流入" in chart["title"]
        assert chart["y_axes"][1]["name"] == "申赎净流入 (亿元)"
        assert chart["series"][1]["name"] == "汇金·申赎净流入"
        assert chart["series"][1]["data"] == [["2026-11-20", 35.7]]  # 净流入 35.7，非规模变化 42.7

        # 折算日净流入为空：该日不出现在资金流序列
        df2 = pd.DataFrame([
            {"基金代码": "510050", "交易日期": "2026-11-20",
             "份额变化(亿份)": 10.0, "规模变化(亿元)": 42.7, "净流入(亿元)": None},
        ])
        chart2 = build_etf_charts(df2, {"510050": ["汇金"]}, months=1)[0]
        assert chart2["series"][1]["data"] == []

        # 旧数据无净流入列：回退规模变化以兼容
        df3 = pd.DataFrame([
            {"基金代码": "510050", "交易日期": "2026-11-20",
             "份额变化(亿份)": 10.0, "规模变化(亿元)": 42.7},
        ])
        chart3 = build_etf_charts(df3, {"510050": ["汇金"]}, months=1)[0]
        assert chart3["series"][1]["data"] == [["2026-11-20", 42.7]]

    def test_daily_detail_summary_and_day(self):
        """按天汇总含区间合计；展开某天返回当日各 ETF 明细并按规模变化降序。"""
        df = pd.DataFrame([
            {"基金代码": "510050", "基金简称": "上证50ETF", "交易日期": "2026-11-19",
             "份额变化(亿份)": 1.0, "份额变化率(%)": 5.0, "规模变化(亿元)": 2.0, "规模变化率(%)": 4.0},
            {"基金代码": "510300", "基金简称": "沪深300ETF", "交易日期": "2026-11-19",
             "份额变化(亿份)": 3.0, "份额变化率(%)": 6.0, "规模变化(亿元)": 6.0, "规模变化率(%)": 7.0},
            {"基金代码": "510050", "基金简称": "上证50ETF", "交易日期": "2026-11-20",
             "份额变化(亿份)": -1.0, "份额变化率(%)": -2.0, "规模变化(亿元)": -3.0, "规模变化率(%)": -1.0},
            {"基金代码": "510300", "基金简称": "沪深300ETF", "交易日期": "2026-11-20",
             "份额变化(亿份)": 2.0, "份额变化率(%)": 3.0, "规模变化(亿元)": 5.0, "规模变化率(%)": 2.0},
        ])
        tag_map = {"510050": ["汇金"], "510300": ["汇金"], "518880": ["黄金"]}
        summary = build_etf_daily_detail(df, tag_map, months=1)
        assert summary["range"] == "近 1 个月"
        # 测试数据无净流入列，回退到规模变化合计
        assert summary["total"] == {"share": 5.0, "cap": 10.0, "net": 10.0, "days": 2}
        assert [r["date"] for r in summary["rows"]] == ["2026-11-20", "2026-11-19"]
        assert summary["rows"][0] == {"date": "2026-11-20", "share": 1.0, "cap": 2.0,
                                       "net": 2.0, "count": 2}

        day = build_etf_daily_detail(df, tag_map, months=1, date="2026-11-19")
        assert day["date"] == "2026-11-19"
        assert [r["code"] for r in day["rows"]] == ["510300", "510050"]  # 规模变化降序
        assert day["rows"][0]["name"] == "沪深300ETF"
        assert day["rows"][0]["share_rate"] == 6.0

    def test_etf_custom_date_range_overrides_months(self):
        """start/end 精确日期优先于 months，图表与按天明细同口径。"""
        df = pd.DataFrame([
            {"基金代码": "510050", "基金简称": "上证50ETF", "交易日期": "2026-10-15",
             "份额变化(亿份)": 9.0, "规模变化(亿元)": 10.0},
            {"基金代码": "510050", "基金简称": "上证50ETF", "交易日期": "2026-11-10",
             "份额变化(亿份)": 2.0, "规模变化(亿元)": 3.0},
            {"基金代码": "510050", "基金简称": "上证50ETF", "交易日期": "2026-11-18",
             "份额变化(亿份)": 4.0, "规模变化(亿元)": 5.0},
            {"基金代码": "510050", "基金简称": "上证50ETF", "交易日期": "2026-11-20",
             "份额变化(亿份)": 7.0, "规模变化(亿元)": 8.0},
        ])
        tag_map = {"510050": ["汇金"]}
        # 指定 11-10 ~ 11-18，即使 months=1 也应以精确区间为准
        charts = build_etf_charts(df, tag_map, months=1, start="2026-11-10", end="2026-11-18")
        hj = charts[0]
        assert hj["series"][0]["data"] == [["2026-11-10", 2.0], ["2026-11-18", 4.0]]
        assert "2026-11-10 ~ 2026-11-18" in hj["note"]
        detail = charts[1]
        assert detail["detail_start"] == "2026-11-10"
        assert detail["detail_end"] == "2026-11-18"
        assert detail["range_label"] == "2026-11-10 ~ 2026-11-18"

        # 单边：仅 start
        hj2 = build_etf_charts(df, tag_map, months=12, start="2026-11-18")[0]
        assert [d for d, _ in hj2["series"][0]["data"]] == ["2026-11-18", "2026-11-20"]
        assert hj2["series"][1]["data"]  # 资金流线非空
        # 仅 end
        hj3 = build_etf_charts(df, tag_map, months=12, end="2026-11-10")[0]
        assert [d for d, _ in hj3["series"][0]["data"]] == ["2026-10-15", "2026-11-10"]

        # 按天明细同样按精确区间过滤并反映范围
        daily = build_etf_daily_detail(df, tag_map, months=1, start="2026-11-10", end="2026-11-18")
        assert [r["date"] for r in daily["rows"]] == ["2026-11-18", "2026-11-10"]
        assert daily["range"] == "2026-11-10 ~ 2026-11-18"
        assert daily["total"]["share"] == 6.0  # 2 + 4

    def test_huijin_chart_overlays_index(self):
        """有指数数据时汇金原图叠加日线（上下双图），黄金图与细分图不叠加；窗口与 ETF 一致。"""
        df = pd.DataFrame([
            {"基金代码": "510050", "交易日期": "2026-10-30",
             "份额变化(亿份)": 1.0, "规模变化(亿元)": 2.0},
            {"基金代码": "510050", "交易日期": "2026-11-18",
             "份额变化(亿份)": 3.0, "规模变化(亿元)": 4.0},
            {"基金代码": "518880", "交易日期": "2026-11-18",
             "份额变化(亿份)": -0.5, "规模变化(亿元)": -1.5},
        ])
        tag_map = {"510050": ["汇金"], "518880": ["黄金"]}
        idx = pd.DataFrame([
            {"指数代码": "000001", "指数名称": "上证指数", "交易日期": "2026-10-30", "收盘价": 3410.0},
            {"指数代码": "000001", "指数名称": "上证指数", "交易日期": "2026-11-18", "收盘价": 3520.5},
            {"指数代码": "000001", "指数名称": "上证指数", "交易日期": "2026-11-20", "收盘价": 3600.0},
        ])
        # 精确区间：指数按同窗口截取
        charts = build_etf_charts(df, tag_map, months=12,
                                  start="2026-11-18", end="2026-11-19", index_df=idx)
        hj = charts[0]
        assert hj["layout"] == "etf_overlay"
        assert hj["index_name"] == "上证指数 (点)"
        assert hj["index_data"] == [["2026-11-18", 3520.5]]
        assert "上方叠加上证指数日线" in hj["note"]
        # 细分图、黄金图保持各自布局，不受叠加影响
        assert charts[1].get("layout") == "etf_rank"
        assert charts[2].get("layout") is None

        # 月份模式：low 为 YYYY-MM，指数按月份截取
        charts_m = build_etf_charts(df, tag_map, months=1, index_df=idx)
        assert [d for d, _ in charts_m[0]["index_data"]] == ["2026-11-18", "2026-11-20"]

        # 无指数数据：退化为普通单图（兼容既有行为）
        assert build_etf_charts(df, tag_map, months=1)[0].get("layout") is None

    def test_overlay_index_switchable(self):
        """叠加指数可切换：按 index_code 取对应日线与名称，无该指数数据则不叠加。"""
        df = pd.DataFrame([
            {"基金代码": "510050", "交易日期": "2026-11-20",
             "份额变化(亿份)": 1.0, "规模变化(亿元)": 2.0},
        ])
        idx = pd.DataFrame([
            {"指数代码": "000001", "指数名称": "上证指数", "交易日期": "2026-11-20", "收盘价": 3533.0},
            {"指数代码": "000300", "指数名称": "沪深300", "交易日期": "2026-11-20", "收盘价": 4100.0},
            {"指数代码": "399006", "指数名称": "创业板指", "交易日期": "2026-11-20", "收盘价": 2200.0},
        ])
        tag_map = {"510050": ["汇金"]}
        hj = build_etf_charts(df, tag_map, months=1, index_df=idx, index_code="000300")[0]
        assert hj["index_code"] == "000300"
        assert hj["index_name"] == "沪深300 (点)"
        assert hj["index_data"] == [["2026-11-20", 4100.0]]
        assert "上方叠加沪深300日线" in hj["note"]
        # 切换另一指数
        hj2 = build_etf_charts(df, tag_map, months=1, index_df=idx, index_code="399006")[0]
        assert hj2["index_name"] == "创业板指 (点)"
        assert hj2["index_data"] == [["2026-11-20", 2200.0]]
        # 不存在的指数：退化为普通单图
        hj3 = build_etf_charts(df, tag_map, months=1, index_df=idx, index_code="999999")[0]
        assert hj3.get("layout") is None

    def test_no_tags_returns_empty_series(self):
        df = pd.DataFrame([
            {"基金代码": "510050", "交易日期": "2026-11-20",
             "份额变化(亿份)": 1.0, "规模变化(亿元)": 2.0},
        ])
        charts = build_etf_charts(df, {}, months=12)
        assert [c["id"] for c in charts] == ["etf_huijin", "etf_huijin_detail", "etf_gold"]
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
        assert len(data["macro"]["charts"]) == 8
        assert data["macro"]["charts"][-1]["id"] == "loan_new_chart"
        assert len(data["etf"]["charts"]) == 4
        assert data["etf"]["charts"][0]["etf_count"] == 1
        assert data["etf"]["charts"][0]["id"] == "etf_huijin"
        assert data["etf"]["charts"][1]["id"] == "etf_huijin_detail"
        assert data["etf"]["charts"][2]["id"] == "etf_huijin_cumulative"
        assert data["etf"]["charts"][3]["id"] == "etf_gold"
        assert "generated_at" in data
        # 接口透传指数数据：汇金原图叠加日线（上下双图）
        hj = data["etf"]["charts"][0]
        assert hj["layout"] == "etf_overlay"
        assert [d for d, _ in hj["index_data"]] == ["2026-10-30", "2026-10-31"]
        assert data["etf"]["charts"][3].get("layout") is None  # 黄金图为普通时间序列
        # 默认叠加指数为上证指数，并返回可用指数选项
        assert data["overlay_index"] == "000001"
        assert {o["code"]: o["name"] for o in data["index_options"]} == {"000001": "上证指数"}

        # 切换叠加指数：接口按 overlay_index 取对应指数日线
        data2 = client.get("/api/dashboard?months=12&etf_months=12&overlay_index=000001").json()
        assert data2["overlay_index"] == "000001"
        assert data2["etf"]["charts"][0]["index_code"] == "000001"

    def test_dashboard_endpoint_etf_date_range(self, client):
        data = client.get(
            "/api/dashboard?months=12&etf_months=12"
            "&etf_start=2026-11-10&etf_end=2026-11-20"
        ).json()
        assert data["etf_start"] == "2026-11-10"
        assert data["etf_end"] == "2026-11-20"
        # 未配置 ETF 标签时无汇金数据，故不生成累计图（3 张）；有汇金数据时为 4 张
        ids = [c["id"] for c in data["etf"]["charts"]]
        assert "etf_huijin" in ids and "etf_gold" in ids
        if "etf_huijin_cumulative" in ids:
            cum = next(c for c in data["etf"]["charts"] if c["id"] == "etf_huijin_cumulative")
            assert all("2026-11-10" <= d <= "2026-11-20" for d, _ in cum["series"][0]["data"])

    def test_etf_daily_endpoint_date_range(self, client):
        data = client.get(
            "/api/dashboard/etf-daily?months=12"
            "&start=2026-11-10&end=2026-11-20"
        ).json()
        assert "rows" in data and "total" in data
        assert "2026-11-10" in data["range"]

    def test_etf_daily_endpoint(self, client, db_path, config):
        etf_cfg = dict(config["etf"])
        etf_cfg["tags"] = {"510050": ["汇金"], "159915": ["黄金"]}
        storage.save_etf_config(db_path, etf_cfg)
        summary = client.get("/api/dashboard/etf-daily?months=12").json()
        assert "rows" in summary and "total" in summary
        assert summary["total"]["days"] >= 1
        target = summary["rows"][0]["date"]
        day = client.get(f"/api/dashboard/etf-daily?months=12&date={target}").json()
        assert day["date"] == target
        assert isinstance(day["rows"], list)
        if day["rows"]:
            assert "code" in day["rows"][0] and "cap" in day["rows"][0]
