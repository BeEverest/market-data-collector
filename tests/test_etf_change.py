"""ETF 标签配置与较上一交易日变化指标测试。"""
from __future__ import annotations

import pandas as pd
import pytest

import storage
from processors.etf_change import compute_etf_changes


def _etf_row(code, date, shares, cap):
    return {
        "基金代码": code, "基金简称": code, "交易市场": "SH",
        "交易日期": date, "份额(亿份)": shares, "单位净值(元)": 1.0,
        "规模(亿元)": cap, "数据源": "test", "采集时间": "test",
    }


class TestComputeEtfChanges:
    def test_changes_use_previous_trading_day_per_code(self):
        df = pd.DataFrame([
            _etf_row("510050", "2026-10-17", 100.0, 350.0),
            _etf_row("510050", "2026-10-20", 110.0, 385.0),
            _etf_row("159915", "2026-10-17", 50.0, 120.0),
            _etf_row("159915", "2026-10-20", 48.0, 115.2),
        ])
        out = compute_etf_changes(df).set_index(["基金代码", "交易日期"])
        row = out.loc[("510050", "2026-10-20")]
        assert row["份额变化(亿份)"] == pytest.approx(10.0)
        assert row["份额变化率(%)"] == pytest.approx(10.0)
        assert row["规模变化(亿元)"] == pytest.approx(35.0)
        assert row["规模变化率(%)"] == pytest.approx(10.0)
        assert pd.isna(out.loc[("510050", "2026-10-17"), "份额变化(亿份)"])
        down = out.loc[("159915", "2026-10-20")]
        assert down["份额变化(亿份)"] == pytest.approx(-2.0)
        assert down["份额变化率(%)"] == pytest.approx(-4.0)

    def test_net_inflow_uses_nav_and_excludes_price_move(self):
        """净流入=份额变化×当日净值，不含净值涨跌的市值波动。"""
        df = pd.DataFrame([
            {**_etf_row("510050", "2026-10-17", 100.0, 350.0), "单位净值(元)": 3.5},
            {**_etf_row("510050", "2026-10-20", 110.0, 392.7), "单位净值(元)": 3.57},
        ])
        out = compute_etf_changes(df).set_index("交易日期").loc["2026-10-20"]
        assert out["份额变化(亿份)"] == pytest.approx(10.0)
        assert out["规模变化(亿元)"] == pytest.approx(42.7)  # 含净值上涨
        assert out["净流入(亿元)"] == pytest.approx(35.7)    # 10 亿份 × 3.57，剔除价格因素

    def test_split_day_net_inflow_blank(self):
        """份额折算（净值单日涨跌>20%）当日净流入置空。"""
        df = pd.DataFrame([
            {**_etf_row("510310", "2024-09-19", 1218.64, 1947.03), "单位净值(元)": 1.5977},
            {**_etf_row("510310", "2024-09-20", 623.47, 1996.42), "单位净值(元)": 3.2021},
        ])
        out = compute_etf_changes(df).set_index("交易日期").loc["2024-09-20"]
        assert out["份额变化(亿份)"] == pytest.approx(-595.17, abs=0.01)
        assert pd.isna(out["净流入(亿元)"])  # 折算日不计入净流入

    def test_nonpositive_previous_value_has_no_rate(self):
        df = pd.DataFrame([
            _etf_row("510050", "2026-10-17", 0.0, 0.0),
            _etf_row("510050", "2026-10-20", 10.0, 20.0),
        ])
        out = compute_etf_changes(df).set_index("交易日期").loc["2026-10-20"]
        assert out["份额变化(亿份)"] == pytest.approx(10.0)
        assert pd.isna(out["份额变化率(%)"])
        assert pd.isna(out["规模变化率(%)"])


class TestReadEtfWithChanges:
    def test_filters_do_not_break_previous_day_baseline(self, tmp_path):
        db = tmp_path / "etf_change.db"
        storage.upsert_etf(db, pd.DataFrame([
            _etf_row("510050", "2026-10-17", 100.0, 350.0),
            _etf_row("510050", "2026-10-20", 110.0, 385.0),
        ]), "test")
        # 只看 10-20 时，上一交易日基准仍来自全库 10-17
        total, out = storage.read_etf_with_changes(db, code="510050", start="2026-10-20")
        assert total == 1
        assert out.iloc[0]["份额变化(亿份)"] == pytest.approx(10.0)
        assert "标签" not in out.columns
