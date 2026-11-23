"""数据处理模块：派生指标口径与存储测试。"""
from __future__ import annotations

import pandas as pd
import pytest

import storage
from processors.derived import compute_derived


def _row(code, date, value=None, yoy=None):
    return {
        "指标代码": code, "指标名称": code, "日期": date, "数值": value,
        "单位": "", "同比(%)": yoy, "环比(%)": None,
        "数据源": "test", "采集时间": "2026-11-23 00:00:00",
    }


@pytest.fixture
def macro_df() -> pd.DataFrame:
    rows = [
        _row("TRADE_CUM_TOTAL", "2025-10", 1000), _row("TRADE_CUM_TOTAL", "2025-11", 1100),
        _row("TRADE_CUM_TOTAL", "2026-10", 1150), _row("TRADE_CUM_TOTAL", "2026-11", 1265),
        _row("RETAIL_SALES", "2026-11", 100, 0.4),
        _row("CPI", "2026-11", 100, 0.7), _row("PPI", "2026-11", 100, 5.7),
        _row("SFR_STOCK", "2026-11", 4691600, 7.2),
        _row("M1", "2026-11", 100, 4.4), _row("M2", "2026-11", 1000000, 7.1),
        _row("A_SHARE_MC", "2026-11", 300000),
        _row("HH_DEPOSIT", "2025-10", 100), _row("HH_DEPOSIT", "2025-11", 110),
        _row("HH_DEPOSIT", "2026-10", 130), _row("HH_DEPOSIT", "2026-11", 145),
        _row("RMB_LOAN", "2025-11", 1000), _row("RMB_LOAN", "2026-10", 1000),
        _row("RMB_LOAN", "2026-11", 1200),
        _row("GOLD_RESERVE", "2025-11", 6000), _row("GOLD_RESERVE", "2026-10", 6000),
        _row("GOLD_RESERVE", "2026-11", 6083),
    ]
    return pd.DataFrame(rows)


class TestComputeDerived:
    def test_all_confirmed_formulas(self, macro_df):
        df = compute_derived(macro_df, computed_at="2026-11-23 12:00:00")
        got = df.set_index(["code", "date"])["value"]

        assert got[("TRADE_YOY", "2026-11")] == pytest.approx(15.0)  # 官方累计
        assert got[("RETAIL_SALES_YOY", "2026-11")] == pytest.approx(0.4)
        assert got[("CPI_PPI_GAP", "2026-11")] == pytest.approx(-5.0)
        assert got[("SFR_STOCK_YOY", "2026-11")] == pytest.approx(7.2)  # 央行官方增速
        assert got[("M1_M2_GAP", "2026-11")] == pytest.approx(-2.7)
        assert got[("A_SHARE_MC_M2", "2026-11")] == pytest.approx(30.0)
        assert got[("HH_DEPOSIT_NEW", "2026-11")] == pytest.approx(15.0)
        assert got[("HH_DEPOSIT_NEW_YOY", "2026-11")] == pytest.approx(5.0)
        assert got[("RMB_LOAN_NEW", "2026-11")] == pytest.approx(200.0)  # 余额差分
        # 83 万盎司 = 83 × 10^4 × 31.1034768 克 ÷ 10^6 克/吨 = 25.816 吨（÷100，不是 ÷10000）
        assert got[("GOLD_NEW_TON", "2026-11")] == pytest.approx(83 * 31.1034768 / 100, abs=1e-4)

    def test_missing_months_are_skipped(self, macro_df):
        df = compute_derived(macro_df)
        got = df.set_index(["code", "date"])["value"]
        # 2025-10 无上月数据，不产生活期新增；2025-11 无去年同月，不产生同比多增
        assert ("HH_DEPOSIT_NEW", "2025-10") not in got.index
        assert ("HH_DEPOSIT_NEW_YOY", "2025-11") not in got.index
        assert ("TRADE_YOY", "2025-10") not in got.index

    def test_metadata_complete(self, macro_df):
        df = compute_derived(macro_df)
        assert set(["code", "name", "date", "value", "unit", "formula", "grp", "source", "computed_at"]).issubset(df.columns)
        assert set(df["code"]) <= set([
            "TRADE_YOY", "RETAIL_SALES_YOY", "CPI_YOY", "PPI_YOY", "CPI_PPI_GAP",
            "SFR_STOCK_YOY", "M1_YOY", "M2_YOY", "M1_M2_GAP", "A_SHARE_MC_M2",
            "HH_DEPOSIT_NEW", "HH_DEPOSIT_NEW_YOY", "RMB_LOAN_NEW", "GOLD_NEW_TON",
        ])
        assert set(df["grp"]) <= {"A. 价格与通胀", "B. 货币与信用", "C. 存贷款与储备"}


class TestDerivedStorage:
    def test_upsert_and_read(self, tmp_path, macro_df):
        db = tmp_path / "derived.db"
        out = compute_derived(macro_df)
        n = storage.upsert_derived(db, out)
        assert n == len(out)
        # 幂等：重复写入不增加行数
        storage.upsert_derived(db, out)
        month = storage.read_derived(db, "2026-11")
        assert set(month["code"]) == set(out[out["date"] == "2026-11"]["code"])
        assert "2026-11" in storage.derived_months(db)
        assert storage.read_derived(db, "1999-01").empty
