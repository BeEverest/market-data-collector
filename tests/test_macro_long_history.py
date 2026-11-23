"""宏观长周期历史补全与黄金异常过滤测试。"""
from __future__ import annotations

import pandas as pd
import pytest

import storage
from collectors import macro as m
from collectors.macro_long_history import (
    build_long_hh_deposit, build_long_loan_balance, build_retail_jan_feb,
)
from processors.derived import compute_derived


def _add(dfs, code, dates, values, extra=None):
    rows = []
    for d, v in zip(dates, values):
        rows.append({"指标代码": code, "指标名称": code, "日期": d, "数值": v,
                     "单位": "亿元", "同比(%)": None, "环比(%)": None,
                     "数据源": "test", "采集时间": "test"})
    dfs[code] = pd.DataFrame(rows)


class TestLongHistory:
    def test_loan_balance_reconstructed_backwards(self):
        inc = pd.DataFrame({
            "月份": ["2024年05月份", "2024年04月份", "2024年03月份", "2024年02月份"],
            "当月": [9500.0, 8000.0, 9000.0, 7000.0],
        })
        cur = pd.DataFrame({
            "月份": ["2024-05", "2024-06"],
            "累计人民币贷款-总额": [2487269.61, 2508526.52],
        })
        fetched = {
            "macro_china_new_financial_credit": inc,
            "macro_rmb_loan": cur,
        }
        df = build_long_loan_balance(lambda fn: fetched.get(fn, pd.DataFrame()), "2026-11-23 00:00:00")
        assert not df.empty
        by_date = df.set_index("日期")["数值"]
        assert by_date["2024-05"] == pytest.approx(2487269.61)
        assert by_date["2024-04"] == pytest.approx(2487269.61 - 9500)
        assert by_date["2024-03"] == pytest.approx(2487269.61 - 9500 - 8000)

    def test_hh_deposit_long_source(self):
        df = pd.DataFrame({
            "统计时间": ["2024.12", "2024.11", "2024.10"],
            "储蓄存款": [1521245.67, 1499261.27, 1491426.93],
            "储蓄存款同比增长": [10.1, 10.3, 10.5],
        })
        out = build_long_hh_deposit(lambda fn: df, "2026-11-23 00:00:00")
        assert len(out) == 3
        assert out.iloc[0]["日期"] == "2024-10"
        assert out.iloc[-1]["数值"] == pytest.approx(1521245.67)

    def test_retail_jan_feb_from_cumulative_yoy(self):
        df = pd.DataFrame({
            "月份": ["2026年02月份", "2026年01月份", "2025年02月份"],
            "当月": [None, None, None],
            "同比增长": [None, None, None],
            "累计": [86078.9, 43039.45, 83730.6],
            "累计-同比增长": [2.8, 2.8, 4.0],
        })
        out = build_retail_jan_feb(lambda fn: df, "2026-11-23 00:00:00")
        assert len(out) == 3
        feb26 = out[out["日期"] == "2026-02"].iloc[0]
        assert feb26["同比(%)"] == pytest.approx(2.8)
        assert feb26["数值"] == pytest.approx(86078.9 - 43039.45, abs=0.01)


class TestGoldAnomaly:
    def test_big_disclosure_jump_skipped(self, macro_df):
        d = macro_df.copy()
        # 追加 2015-05 / 2015-06 的口径跳变（3389 -> 5332 万盎司 = +604 吨）
        extra = []
        for date, val in (("2015-05", 3389.0), ("2015-06", 5332.0), ("2015-07", 5393.0)):
            extra.append({
                "指标代码": "GOLD_RESERVE", "指标名称": "央行黄金储备", "日期": date,
                "数值": val, "单位": "万盎司", "同比(%)": None, "环比(%)": None,
                "数据源": "test", "采集时间": "test", "run_id": "test",
            })
        out = compute_derived(pd.concat([d, pd.DataFrame(extra)], ignore_index=True))
        gold = out[out["code"] == "GOLD_NEW_TON"].set_index("date")["value"]
        # 604 吨的口径重述被过滤；正常月份差分（5393-5332=61 万盎司）保留
        assert "2015-06" not in gold.index
        assert gold["2015-07"] == pytest.approx(61 * 31.1034768 / 100, abs=1e-3)
