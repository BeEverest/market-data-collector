"""全量采集日期范围：验证前端参数贯穿 API 层与三类采集器。"""
from __future__ import annotations

from unittest.mock import Mock

import pandas as pd
import pytest

import main
import web_app


@pytest.fixture
def range_config():
    return {
        "fetch": {"retry": 1, "retry_interval_sec": 0, "timeout_sec": 5},
        "macro": {"start_date": "2020-01-01", "end_date": "2020-12-31"},
        "etf": {
            "codes": ["510050"], "start_date": "2020-01-01", "end_date": "2020-12-31",
            "listing_dates": {"510050": "2004-12-30"},
        },
        "index": {
            "codes": ["000001"], "start_date": "2020-01-01", "end_date": "2020-12-31",
            "lookback_days": 365,
        },
    }


def _capture_collectors(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "_save_listing_dates", lambda cfg: None)
    for name in ("collect_macro", "collect_etf", "collect_index"):
        monkeypatch.setattr(
            main, name,
            lambda _n=name, **kw: (captured.update({_n: kw}) or pd.DataFrame()),
        )
    return captured


class TestCollectDateOverride:
    def test_override_applies_to_all_three_categories(self, monkeypatch, range_config):
        captured = _capture_collectors(monkeypatch)
        main._collect(
            range_config, Mock(), start_date="2024-01-01", end_date="2024-06-30"
        )
        for name in ("collect_macro", "collect_etf", "collect_index"):
            assert captured[name]["start_date"] == "2024-01-01"
            assert captured[name]["end_date"] == "2024-06-30"

    def test_falls_back_to_config_when_no_override(self, monkeypatch, range_config):
        captured = _capture_collectors(monkeypatch)
        main._collect(range_config, Mock())
        for name in ("collect_macro", "collect_etf", "collect_index"):
            assert captured[name]["start_date"] == "2020-01-01"
            assert captured[name]["end_date"] == "2020-12-31"

    def test_partial_override_only_replaces_given_side(self, monkeypatch, range_config):
        captured = _capture_collectors(monkeypatch)
        main._collect(range_config, Mock(), start_date="2023-05-01")
        assert captured["collect_macro"]["start_date"] == "2023-05-01"
        assert captured["collect_macro"]["end_date"] == "2020-12-31"
        assert captured["collect_etf"]["start_date"] == "2023-05-01"
        assert captured["collect_etf"]["end_date"] == "2020-12-31"

    def test_single_category_still_respects_its_own_flags(self, monkeypatch, range_config):
        captured = _capture_collectors(monkeypatch)
        main._collect(range_config, Mock(), etf_only=True, start_date="2024-01-01")
        assert "collect_macro" not in captured
        assert "collect_index" not in captured
        assert captured["collect_etf"]["start_date"] == "2024-01-01"


class TestApiCollectDateRange:
    def test_passes_range_to_collect_job(self, monkeypatch):
        calls = {}

        def fake_submit(kind, func):
            calls["kind"] = kind
            calls["func"] = func
            return "job-123"

        monkeypatch.setattr(web_app, "_submit", fake_submit)
        monkeypatch.setattr(
            web_app, "_do_collect",
            lambda *args: calls.update(args=args) or "done",
        )

        result = web_app.api_collect({"start": "2024-01-01", "end": "2024-06-30"})
        assert result == {"job_id": "job-123"}
        calls["func"]()
        assert calls["kind"] == "collect"
        assert calls["args"] == (False, False, False, "2024-01-01", "2024-06-30")

    def test_blank_range_becomes_none(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(
            web_app, "_submit", lambda kind, func: calls.update(func=func) or "job-456"
        )
        monkeypatch.setattr(
            web_app, "_do_collect", lambda *args: calls.update(args=args) or "done"
        )

        web_app.api_collect({"start": "  ", "end": ""})
        calls["func"]()
        assert calls["args"] == (False, False, False, None, None)

    def test_full_payload_without_range_still_works(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(
            web_app, "_submit", lambda kind, func: calls.update(func=func) or "job-789"
        )
        monkeypatch.setattr(
            web_app, "_do_collect", lambda *args: calls.update(args=args) or "done"
        )

        web_app.api_collect({"macro_only": True})
        calls["func"]()
        assert calls["args"] == (True, False, False, None, None)
