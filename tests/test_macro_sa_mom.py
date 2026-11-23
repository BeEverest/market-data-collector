"""社零环比口径校正测试：官方季调环比解析 + 覆盖 + 降级。"""
from __future__ import annotations

import pandas as pd
import pytest

from collectors import macro_sa_mom
from collectors.macro_sa_mom import apply_sa_mom, parse_sa_mom


@pytest.fixture(autouse=True)
def _no_sfr_network(monkeypatch):
    """本文件只测社零；禁用社融存量官方联网，避免额外网络依赖。"""
    from collectors import macro_sfr_stock
    monkeypatch.setattr(
        macro_sfr_stock, "fetch_sfr_stock",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tests: sfr network disabled")),
    )


SAMPLE_TEXT = (
    "社会消费品零售总额 附注 5.环比数据修订 根据季节调整模型自动修正的结果，"
    "对2025年10月份以来的社会消费品零售总额环比增速进行修订。修订结果及2026年10月份环比数据如下： "
    "年份 月份 环比增速（ % ） 2025 年 10 月 0.11 11 月 -0.14 12 月 -0.08 "
    "2026 年 1 月 0.15 2 月 0.28 3 月 0.11 4 月 -0.26 5 月 -0.10 "
    "6 月 0.25 7 月 0.07 8 月 -0.21 9 月 0.34 10 月 0.02 6.指标调整说明 后续文字"
)


class TestParseSaMom:
    def test_parse_full_table(self):
        d = parse_sa_mom(SAMPLE_TEXT)
        assert len(d) == 13
        assert d["2025-10"] == pytest.approx(0.11)
        assert d["2025-12"] == pytest.approx(-0.08)
        assert d["2026-01"] == pytest.approx(0.15)
        assert d["2026-09"] == pytest.approx(0.34)
        assert d["2026-10"] == pytest.approx(0.02)

    def test_no_anchor_returns_empty(self):
        assert parse_sa_mom("普通新闻稿无任何修订表") == {}

    def test_stops_at_next_section(self):
        # 指标调整说明之后的数字不应被解析为环比
        d = parse_sa_mom(SAMPLE_TEXT + " 15.3 亿元 增长 6.7")
        assert "2026-10" in d and len(d) == 13


class TestApplySaMom:
    def _df(self):
        return pd.DataFrame({
            "指标代码": ["RETAIL_SALES", "RETAIL_SALES", "CPI", "RETAIL_SALES"],
            "日期": ["2026-08", "2026-09", "2026-09", "2025-06"],
            "环比(%)": [2.05, 6.55, 0.1, 2.32],
            "数据源": ["统计局"] * 4,
        })

    def test_override_only_retail_and_known_months(self):
        df, n = apply_sa_mom(self._df(), {"2026-09": 0.34, "2026-08": -0.21})
        assert n == 2
        rows = df.set_index(["指标代码", "日期"])
        assert rows.loc[("RETAIL_SALES", "2026-09"), "环比(%)"] == pytest.approx(0.34)
        assert rows.loc[("RETAIL_SALES", "2026-08"), "环比(%)"] == pytest.approx(-0.21)
        # 未命中的月份保留原值
        assert rows.loc[("RETAIL_SALES", "2025-06"), "环比(%)"] == pytest.approx(2.32)
        # 非社零指标不被改动
        assert rows.loc[("CPI", "2026-09"), "环比(%)"] == pytest.approx(0.1)
        assert rows.loc[("CPI", "2026-09"), "数据源"] == "统计局"

    def test_source_label_marked_on_overridden_rows(self):
        df, n = apply_sa_mom(self._df(), {"2026-09": 0.34}, source_label="国家统计局（季调环比）")
        assert n == 1
        row = df[(df["指标代码"] == "RETAIL_SALES") & (df["日期"] == "2026-09")].iloc[0]
        assert "季调" in row["数据源"]

    def test_empty_map_no_change(self):
        df, n = apply_sa_mom(self._df(), {})
        assert n == 0
        assert df["环比(%)"].tolist() == [2.05, 6.55, 0.1, 2.32]


class TestCollectMacroIntegration:
    def test_sa_mom_map_param_used_without_network(self, monkeypatch):
        """显式传 sa_mom_map 时不联网，直接覆盖社零环比。"""
        from collectors import macro

        raw = pd.DataFrame({
            "月份": ["2026年08月份", "2026年09月份"],
            "当月": [100.0, 110.0],
            "同比增长": [1.0, 2.0],
            "环比增长": [2.05, 6.55],  # 未季调名义环比
        })
        monkeypatch.setattr(macro, "_fetch", lambda ak, fn, **kw: raw.copy() if fn == "macro_china_consumer_goods_retail" else pd.DataFrame())
        # 确保不触发联网：若内部调用 fetch_sa_mom 则失败
        def boom(*a, **k):
            raise AssertionError("不应联网")
        monkeypatch.setattr(macro_sa_mom, "fetch_sa_mom", boom)

        df = macro.collect_macro(
            start_date="2026-08", end_date="2026-09",
            sa_mom_map={"2026-09": 0.34},
        )
        row = df[(df["指标代码"] == "RETAIL_SALES") & (df["日期"] == "2026-09")].iloc[0]
        assert row["环比(%)"] == pytest.approx(0.34)
        assert "季调" in row["数据源"]

    def test_fetch_failure_degrades_gracefully(self, monkeypatch):
        """季调抓取失败时保留接口原环比，采集不中断。"""
        from collectors import macro

        raw = pd.DataFrame({
            "月份": ["2026年09月份"], "当月": [110.0],
            "同比增长": [2.0], "环比增长": [6.55],
        })
        monkeypatch.setattr(macro, "_fetch", lambda ak, fn, **kw: raw.copy() if fn == "macro_china_consumer_goods_retail" else pd.DataFrame())
        def boom(*a, **k):
            raise RuntimeError("网络不可达")
        monkeypatch.setattr(macro_sa_mom, "fetch_sa_mom", boom)

        df = macro.collect_macro(start_date="2026-09", end_date="2026-09")
        row = df[df["指标代码"] == "RETAIL_SALES"].iloc[0]
        assert row["环比(%)"] == pytest.approx(6.55)  # 降级保留原值
        assert "季调" not in str(row["数据源"])
