"""数据处理模块：派生指标口径与存储测试。"""
from __future__ import annotations

import pandas as pd
import pytest

import storage
from processors.derived import compute_derived, incremental_targets


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

    def test_trade_yoy_backfill_from_monthly_total(self):
        """官方累计仅 2023 起；早期年份用月度 TRADE_TOTAL 年内累加重建累计，同比可回溯。"""
        rows = [
            _row("TRADE_TOTAL", "2015-01", 100), _row("TRADE_TOTAL", "2015-02", 110),
            _row("TRADE_TOTAL", "2016-01", 120), _row("TRADE_TOTAL", "2016-02", 132),
            _row("TRADE_CUM_TOTAL", "2025-01", 1000),
            _row("TRADE_CUM_TOTAL", "2026-01", 1150),  # 官方同比 15%
        ]
        df = compute_derived(pd.DataFrame(rows))
        got = df.set_index(["code", "date"])["value"]
        # 2016 年重建累计：1 月 120、2 月 252；对比 2015 年 100、210
        assert got[("TRADE_YOY", "2016-01")] == pytest.approx(20.0)
        assert got[("TRADE_YOY", "2016-02")] == pytest.approx(20.0)
        # 官方累计优先于重建值
        assert got[("TRADE_YOY", "2026-01")] == pytest.approx(15.0)
        # 最早月份无去年同月分母，不产出同比
        assert ("TRADE_YOY", "2015-01") not in got.index

    def test_missing_months_are_skipped(self, macro_df):
        df = compute_derived(macro_df)
        got = df.set_index(["code", "date"])["value"]
        # 2025-10 无上月数据，不产生活期新增；2025-11 无去年同月，不产生同比多增
        assert ("HH_DEPOSIT_NEW", "2025-10") not in got.index
        assert ("HH_DEPOSIT_NEW_YOY", "2025-11") not in got.index
        assert ("TRADE_YOY", "2025-10") not in got.index

    def test_filter_by_month_and_code(self, macro_df):
        """month/only_code 仅过滤输出，不参与计算；差分指标仍基于全量数据得出。"""
        full = compute_derived(macro_df)
        by_month = compute_derived(macro_df, month="2026-11")
        assert not by_month.empty
        assert set(by_month["date"]) == {"2026-11"}
        assert set(by_month["code"]) == set(full[full["date"] == "2026-11"]["code"])

        by_code = compute_derived(macro_df, only_code="RMB_LOAN_NEW")
        assert set(by_code["code"]) == {"RMB_LOAN_NEW"}
        # 差分依赖上月：单指标过滤仍须算出 2026-11 = 1200-1000
        nov = by_code[by_code["date"] == "2026-11"]["value"].iloc[0]
        assert nov == pytest.approx(200.0)

        both = compute_derived(macro_df, month="2026-11", only_code="GOLD_NEW_TON")
        assert len(both) <= 1
        if not both.empty:
            assert set(both["code"]) == {"GOLD_NEW_TON"}
            assert set(both["date"]) == {"2026-11"}

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

    def test_partial_upsert_keeps_other_rows(self, tmp_path, macro_df):
        """局部重算按业务键覆盖，只影响目标月份/指标，不误删其他数据。"""
        db = tmp_path / "derived.db"
        full = compute_derived(macro_df)
        storage.replace_derived(db, full)
        before = len(storage.read_derived(db))

        nov_only = compute_derived(macro_df, month="2026-11")
        storage.upsert_derived(db, nov_only)
        assert len(storage.read_derived(db)) == before  # 行数不变，仅覆盖目标行

        code_only = compute_derived(macro_df, only_code="RMB_LOAN_NEW")
        changed = code_only[code_only["date"] == "2026-11"].copy()
        changed["value"] = 999.0
        storage.upsert_derived(db, changed)
        after = storage.read_derived(db)
        assert len(after) == before
        got = after[(after["code"] == "RMB_LOAN_NEW") & (after["date"] == "2026-11")]
        assert got["value"].iloc[0] == pytest.approx(999.0)
        # 其他指标不受影响
        gold = after[(after["code"] == "GOLD_NEW_TON") & (after["date"] == "2026-11")]
        assert not gold.empty


class TestIncrementalRecompute:
    """增量重算：只重算受影响 (指标, 月份)，分母仍取全量历史，upsert 不删旧行。"""

    def test_incremental_targets_maps_codes_and_months(self):
        codes, months = incremental_targets({"M2", "CPI"}, {"2026-11"})
        # M2 -> M2_YOY / M1_M2_GAP / A_SHARE_MC_M2；CPI -> CPI_YOY / CPI_PPI_GAP
        assert codes == {"M2_YOY", "M1_M2_GAP", "A_SHARE_MC_M2", "CPI_YOY", "CPI_PPI_GAP"}
        # 偏移并集：当月、下月、明年同月、明年下月
        assert months == {"2026-11", "2026-12", "2027-11", "2027-12"}

    def test_incremental_targets_empty_inputs(self):
        assert incremental_targets(set(), {"2026-11"}) == (set(), set())
        assert incremental_targets({"M2"}, set()) == (set(), set())

    def test_incremental_keeps_diff_and_yoy_denominators(self):
        """差分/二阶差分的目标月计算仍须拿到上月/去年同月分母，结果与全量一致。"""
        df = pd.DataFrame([
            _row("HH_DEPOSIT", "2025-10", 900.0),   # 2025-11 新增的分母
            _row("HH_DEPOSIT", "2025-11", 1000.0),  # 去年同月新增(=100)的分母
            _row("HH_DEPOSIT", "2026-10", 1100.0),
            _row("HH_DEPOSIT", "2026-11", 1300.0),
        ])
        full = compute_derived(df)
        inc = compute_derived(df, target_codes={"HH_DEPOSIT_NEW", "HH_DEPOSIT_NEW_YOY"},
                              target_months={"2026-11"})
        assert set(inc["code"]) == {"HH_DEPOSIT_NEW", "HH_DEPOSIT_NEW_YOY"}
        assert set(inc["date"]) == {"2026-11"}
        # 与全量同月同指标的值完全一致（分母未丢失）
        f = full[(full["date"] == "2026-11") & (full["code"] == "HH_DEPOSIT_NEW")]
        i = inc[inc["code"] == "HH_DEPOSIT_NEW"]
        assert i["value"].iloc[0] == pytest.approx(f["value"].iloc[0]) == pytest.approx(200.0)

    def test_incremental_upsert_does_not_delete_other_rows(self, tmp_path):
        """增量写入按 biz_key 覆盖，只影响目标行，其他派生行保留。"""
        db = tmp_path / "derived.db"
        df = pd.DataFrame([
            _row("M2", "2026-09", 3135000.0, 8.0),
            _row("M2", "2026-10", 3150000.0, 8.2),
            _row("M2", "2026-11", 3160000.0, 8.3),
            _row("CPI", "2026-11", 100.2, 0.2),
        ])
        full = compute_derived(df)
        storage.replace_derived(db, full)
        before = storage.read_derived(db)

        inc = compute_derived(df, target_codes={"M2_YOY"}, target_months={"2026-11"})
        inc = inc.copy()
        inc["value"] = 999.0
        storage.upsert_derived(db, inc)
        after = storage.read_derived(db)
        assert len(after) == len(before)  # 行数不变，未删全表
        got = after[(after["code"] == "M2_YOY") & (after["date"] == "2026-11")]
        assert got["value"].iloc[0] == pytest.approx(999.0)
        # 其他指标/月份保持原值
        cpi = after[(after["code"] == "CPI_YOY") & (after["date"] == "2026-11")]
        assert not cpi.empty and cpi["value"].iloc[0] != 999.0

    def test_read_macro_changes_by_run(self, tmp_path):
        """按 run_id 读取本次采集写入的原始代码/月份集合。"""
        from processors.derived import INDICATORS  # 确保依赖模块可导入

        db = tmp_path / "derived.db"
        df = pd.DataFrame([
            _row("M2", "2026-10", 3150000.0, 8.2),
            _row("M2", "2026-11", 3160000.0, 8.3),
            _row("CPI", "2026-11", 100.2, 0.2),
        ])
        storage.upsert_macro(db, df, run_id="run-A")
        storage.upsert_macro(db, pd.DataFrame([_row("M2", "2026-09", 3135000.0, 8.0)]),
                             run_id="run-B")
        codes, months = storage.read_macro_changes_by_run(db, "run-A")
        assert codes == {"M2", "CPI"}
        assert months == {"2026-10", "2026-11"}
        codes_b, months_b = storage.read_macro_changes_by_run(db, "run-B")
        assert codes_b == {"M2"} and months_b == {"2026-09"}
        assert storage.read_macro_changes_by_run(db, "") == (set(), set())
        assert INDICATORS  # noqa: B011 - 仅确认导入成功

    def test_recompute_derived_incremental_by_run_id(self, tmp_path, monkeypatch):
        """调度增量：只重算 run 写入范围相关派生行，不删其他行；空表回退全量。"""
        import web_app

        db = tmp_path / "derived.db"
        rows = [
            _row("M2", "2026-09", 3135000.0, 8.0),
            _row("M2", "2026-10", 3150000.0, 8.2),
            _row("M2", "2026-11", 3160000.0, 8.3),
            _row("CPI", "2026-09", 100.1, 0.1),
            _row("CPI", "2026-10", 100.2, 0.2),
            _row("CPI", "2026-11", 100.3, 0.3),
        ]
        storage.upsert_macro(db, pd.DataFrame(rows), run_id="run-full")
        cfg = {"storage": {"backend": "sqlite", "db_path": str(db)}}

        # 派生表为空 -> 增量触发时回退全量建立基线
        n1 = web_app._recompute_derived(cfg, trigger="定时调度", run_id="run-full")
        base_rows = storage.read_derived(db)
        assert n1 == len(base_rows) and n1 > 0
        assert set(base_rows["code"]) >= {"M2_YOY", "CPI_YOY"}

        # 模拟"本次只新写入 M2 的 2026-11"：增量只重算 M2 相关行，行数不变
        storage.upsert_macro(db, pd.DataFrame([_row("M2", "2026-11", 9999999.0, 99.0)]),
                             run_id="run-delta")
        n2 = web_app._recompute_derived(cfg, trigger="定时调度", run_id="run-delta")
        after = storage.read_derived(db)
        assert n2 > 0
        assert len(after) == len(base_rows)  # 未删全表
        m2 = after[(after["code"] == "M2_YOY") & (after["date"] == "2026-11")]
        assert not m2.empty and m2["value"].iloc[0] == pytest.approx(99.0)

    def test_recompute_derived_incremental_no_macro_writes_skips(self, tmp_path):
        """run 未写入任何宏观数据（如仅采 ETF/指数）时，增量重算跳过返回 0。"""
        import web_app

        db = tmp_path / "derived.db"
        storage.upsert_macro(db, pd.DataFrame([_row("M2", "2026-11", 3160000.0, 8.3)]),
                             run_id="run-full")
        cfg = {"storage": {"backend": "sqlite", "db_path": str(db)}}
        web_app._recompute_derived(cfg, trigger="定时调度", run_id="run-full")
        before = len(storage.read_derived(db))
        n = web_app._recompute_derived(cfg, trigger="定时调度", run_id="run-with-no-macro")
        assert n == 0
        assert len(storage.read_derived(db)) == before
