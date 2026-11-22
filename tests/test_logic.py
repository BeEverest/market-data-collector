"""业务逻辑纯函数单元测试：补丁合并、分页与 JSON 序列化清洗。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

import web_app
from main import _merge_patch


class TestMergePatch:
    def test_empty_inputs(self):
        main = pd.DataFrame([{"日期": "2026-10", "数值": 1.0}])
        assert _merge_patch(main, pd.DataFrame(), ["日期"]) is main
        patch = pd.DataFrame([{"日期": "2026-10", "数值": 2.0}])
        assert _merge_patch(pd.DataFrame(), patch, ["日期"]) is patch

    def test_overwrites_non_empty_fields_only(self):
        main = pd.DataFrame([
            {"日期": "2026-10", "数值": 1.0, "备注": "old", "单位": "亿元"},
            {"日期": "2026-09", "数值": 2.0, "备注": "keep", "单位": "亿元"},
        ])
        patch = pd.DataFrame([{
            "日期": "2026-10", "数值": 9.0, "备注": "", "单位": None,
        }])
        out = _merge_patch(main, patch, ["日期"])
        first = out[out["日期"] == "2026-10"].iloc[0]
        assert first["数值"] == 9.0
        assert first["备注"] == "old"   # 空字符串不覆盖
        assert first["单位"] == "亿元"  # None 不覆盖
        assert out[out["日期"] == "2026-09"].iloc[0]["数值"] == 2.0

    def test_nan_does_not_overwrite(self):
        main = pd.DataFrame([{"代码": "M2", "数值": 1.0}])
        patch = pd.DataFrame([{"代码": "M2", "数值": np.nan}])
        out = _merge_patch(main, patch, ["代码"])
        assert out.iloc[0]["数值"] == 1.0

    def test_appends_missing_key(self):
        main = pd.DataFrame([{"代码": "M2", "数值": 1.0}])
        patch = pd.DataFrame([{"代码": "CPI", "数值": 100.2}])
        out = _merge_patch(main, patch, ["代码"])
        assert len(out) == 2
        assert set(out["代码"]) == {"M2", "CPI"}

    def test_compares_keys_as_strings(self):
        # 模拟 CSV 读回的字符串键与补丁的数值键（Series 含字符串列，保持 object dtype）
        main = pd.DataFrame([{"日期": "202610", "数值": 1.0, "单位": "亿元"}])
        patch = pd.DataFrame([{"日期": 202610, "数值": 5.0, "单位": "亿元"}])
        out = _merge_patch(main, patch, ["日期"])
        assert len(out) == 1 and out.iloc[0]["数值"] == 5.0


class TestPaged:
    def test_pagination_metadata(self):
        df = pd.DataFrame([{"n": i} for i in range(55)])
        result = web_app._paged(df, page=2, page_size=25)
        assert result["total"] == 55
        assert result["pages"] == 3
        assert result["page"] == 2
        assert result["page_size"] == 25
        assert [r["n"] for r in result["rows"]] == list(range(25, 50))

    def test_page_size_capped_at_100(self):
        df = pd.DataFrame([{"n": i} for i in range(150)])
        result = web_app._paged(df, page=1, page_size=1000)
        assert result["page_size"] == 100
        assert result["pages"] == 2
        assert len(result["rows"]) == 100

    def test_invalid_page_corrected(self):
        df = pd.DataFrame([{"n": i} for i in range(10)])
        assert web_app._paged(df, page=0, page_size=5)["page"] == 1
        assert web_app._paged(df, page=99, page_size=5)["page"] == 2
        assert web_app._paged(df, page=1, page_size=0)["page_size"] == 1

    def test_nan_becomes_none_in_rows(self):
        df = pd.DataFrame([{"v": float("nan")}, {"v": 1.0}])
        rows = web_app._paged(df, 1, 10)["rows"]
        assert rows[0]["v"] is None
        assert rows[1]["v"] == 1.0


class TestFrameToRecords:
    def test_empty_frame(self):
        assert web_app._frame_to_records(pd.DataFrame()) == []

    def test_cleans_nan_and_inf(self):
        df = pd.DataFrame([{
            "nan": float("nan"), "inf": float("inf"),
            "ninf": float("-inf"), "ok": 3.0,
        }])
        row = web_app._frame_to_records(df)[0]
        assert row["nan"] is None and row["inf"] is None and row["ninf"] is None
        assert row["ok"] == 3.0
        assert math.isfinite(row["ok"])

    def test_numpy_types_are_native(self):
        df = pd.DataFrame([{"i": np.int64(7), "b": np.bool_(True), "s": "x"}])
        row = web_app._frame_to_records(df)[0]
        assert isinstance(row["i"], int) and row["i"] == 7
        assert isinstance(row["b"], bool) and row["b"] is True
        assert row["s"] == "x"

    def test_limit(self):
        df = pd.DataFrame([{"n": i} for i in range(10)])
        assert len(web_app._frame_to_records(df, limit=3)) == 3
