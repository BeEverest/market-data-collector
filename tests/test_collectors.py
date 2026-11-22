"""采集器纯逻辑单元测试：日期/数值归一化、指标目录与接口调用重试。"""
from __future__ import annotations

from unittest.mock import Mock

import pandas as pd

from collectors import macro


class TestNormalizeMonth:
    def test_supported_formats(self):
        assert macro._normalize_month("2026年10月份") == "2026-10"
        assert macro._normalize_month("2026-10") == "2026-10"
        assert macro._normalize_month("2026.10") == "2026-10"
        assert macro._normalize_month("2026/10") == "2026-10"
        assert macro._normalize_month("201610") == "2016-10"
        assert macro._normalize_month("2026-1") == "2026-01"

    def test_empty_value(self):
        assert macro._normalize_month(None) == ""
        assert macro._normalize_month("  ") == ""


class TestNormalizeQuarter:
    def test_range_and_single_quarter(self):
        assert macro._normalize_quarter("2026年第1-3季度") == "2026-09"
        assert macro._normalize_quarter("2025年第4季度") == "2025-12"
        assert macro._normalize_quarter("2026年第1季度") == "2026-03"

    def test_falls_back_to_month(self):
        assert macro._normalize_quarter("2026-10") == "2026-10"


class TestToFloat:
    def test_valid_values(self):
        assert macro._to_float("8.2%") == 8.2
        assert macro._to_float("1,234.5") == 1234.5
        assert macro._to_float(3.5) == 3.5
        assert macro._to_float("-2.1") == -2.1

    def test_invalid_values(self):
        for raw in (None, "", "-", "--", "nan", "None", "NaN", "abc"):
            assert macro._to_float(raw) is None


class TestCatalog:
    def test_codes_are_unique_and_complete(self):
        codes = [item["code"] for item in macro.catalog()]
        assert len(codes) == len(set(codes)) == 25
        for code in ("M2", "CN_GDP", "US_GDP", "US_CORE_CPI", "US_RATE_UPPER", "US_RATE_LOWER"):
            assert code in codes

    def test_catalog_returns_copies(self):
        first = macro.catalog()
        first[0]["name"] = "changed"
        assert macro.catalog()[0]["name"] != "changed"

    def test_code_to_iface_mapping(self):
        assert macro.CODE_TO_IFACE["M2"] == "macro_china_money_supply"
        assert macro.CODE_TO_IFACE["US_RATE_LOWER"] == "fred_DFEDTARL"


class TestFetch:
    def test_only_filter_skips_call_and_monitor(self):
        ak = Mock()
        mon = Mock()
        result = macro._fetch(ak, "my_fn", monitor=mon, only={"other_fn"})
        assert result.empty
        ak.my_fn.assert_not_called()
        mon.record.assert_not_called()

    def test_success_records_monitor(self):
        ak = Mock()
        ak.my_fn.return_value = pd.DataFrame([{"x": 1}, {"x": 2}])
        mon = Mock()
        result = macro._fetch(ak, "my_fn", monitor=mon, params={"code": "M2"})
        assert len(result) == 2
        kwargs = mon.record.call_args.kwargs
        assert kwargs["status"] == "success"
        assert kwargs["rows"] == 2
        assert kwargs["params"] == {"code": "M2"}

    def test_retry_then_failure_is_recorded(self):
        ak = Mock()
        ak.my_fn.side_effect = RuntimeError("boom")
        mon = Mock()
        result = macro._fetch(ak, "my_fn", monitor=mon, retries=2, retry_interval=0)
        assert result.empty
        assert ak.my_fn.call_count == 2
        kwargs = mon.record.call_args.kwargs
        assert kwargs["status"] == "failed"
        assert kwargs["attempts"] == 2
        assert "RuntimeError: boom" in kwargs["error"]

    def test_empty_dataframe_counts_as_failure_reason(self):
        ak = Mock()
        ak.my_fn.return_value = pd.DataFrame()
        mon = Mock()
        result = macro._fetch(ak, "my_fn", monitor=mon, retries=1)
        assert result.empty
        assert "接口返回空数据" in mon.record.call_args.kwargs["error"]

    def test_retry_can_succeed_on_second_attempt(self):
        ak = Mock()
        ak.my_fn.side_effect = [RuntimeError("once"), pd.DataFrame([{"x": 1}])]
        mon = Mock()
        result = macro._fetch(ak, "my_fn", monitor=mon, retries=2, retry_interval=0)
        assert len(result) == 1
        assert mon.record.call_args.kwargs["attempts"] == 2
        assert mon.record.call_args.kwargs["status"] == "success"
