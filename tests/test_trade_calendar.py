"""最近一个已结束交易日锚点测试（默认采集范围）。"""
from __future__ import annotations

from datetime import date

import pytest

from collectors import trade_calendar as tc


class TestLastCompletedTradeDay:
    def test_returns_latest_strictly_before_reference(self, monkeypatch):
        # 日历含参考日当天，必须跳过当天取前一交易日（凌晨采当天必空）
        monkeypatch.setattr(
            tc, "_calendar_dates",
            lambda: ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23"],
        )
        got = tc.last_completed_trade_day(date(2026, 11, 23))
        assert got == "2026-11-20"

    def test_skips_weekend_in_fallback(self, monkeypatch):
        # 日历不可用：周一回退到上周五，跳过周六/周日
        monkeypatch.setattr(tc, "_calendar_dates", lambda: [])
        got = tc.last_completed_trade_day(date(2026, 11, 23))  # 周一
        assert got == "2026-11-20"

    def test_fallback_workday(self, monkeypatch):
        monkeypatch.setattr(tc, "_calendar_dates", lambda: [])
        got = tc.last_completed_trade_day(date(2026, 11, 19))  # 周四
        assert got == "2026-11-18"

    def test_explicit_end_wins(self, monkeypatch):
        monkeypatch.setattr(
            tc, "_calendar_dates",
            lambda: ["2026-11-18", "2026-11-20", "2026-11-23"],
        )
        assert tc.resolve_default_end("2026-01-02") == "2026-01-02"
        assert tc.resolve_default_end("  2026-01-02  ") == "2026-01-02"

    def test_blank_end_uses_last_completed(self, monkeypatch):
        monkeypatch.setattr(
            tc, "_calendar_dates",
            lambda: ["2026-11-18", "2026-11-20", "2026-11-23"],
        )
        assert tc.resolve_default_end("", date(2026, 11, 23)) == "2026-11-20"
        assert tc.resolve_default_end(None, date(2026, 11, 23)) == "2026-11-20"


class TestEtfUsesAnchor:
    def test_collect_etf_resolves_end_via_anchor(self, monkeypatch):
        """ETF 采集器显式 end 仍优先，空 end 走 T-1 锚点。"""
        from collectors import etf as etf_mod

        seen = {}

        def fake_resolve(end_date, reference=None):
            seen["end_date"] = end_date
            return "2026-11-20"

        monkeypatch.setattr(etf_mod, "resolve_default_end", fake_resolve)
        monkeypatch.setattr(etf_mod, "get_listing_dates", lambda codes, monitor=None, known=None: {})
        monkeypatch.setattr(
            etf_mod, "get_trade_dates",
            lambda start, end, monitor=None: ["2026-11-20"] if "2000" not in start else ["2026-11-20"],
        )
        # 让份额抓取返回空，提前结束，避免外部请求
        monkeypatch.setattr(etf_mod, "fetch_sse_shares", lambda *a, **k: __import__("pandas").DataFrame())
        monkeypatch.setattr(etf_mod, "fetch_szse_shares", lambda *a, **k: __import__("pandas").DataFrame())

        etf_mod.collect_etf(codes=["510300"], end_date=None)
        assert seen["end_date"] is None  # 空 end 交由锚点解析

        seen.clear()
        etf_mod.collect_etf(codes=["510300"], end_date="2026-01-05")
        assert seen["end_date"] == "2026-01-05"  # 显式优先传入


class TestListingDateCache:
    def test_known_cache_avoids_network(self, monkeypatch):
        """持久化缓存中的成立日期应直接命中，不再联网请求档案页。"""
        from collectors import etf as etf_mod

        called = []

        def fake_get(url, *a, **k):
            called.append(url)
            raise AssertionError("不应联网请求已缓存代码的档案页")

        monkeypatch.setattr(etf_mod.requests, "get", fake_get)
        etf_mod._listing_date_cache.clear()
        known = {"510050": "2004-12-30", "159915": "2011-09-20"}
        result = etf_mod.get_listing_dates(["510050", "159915"], known=known)
        assert result == known
        assert called == []
        # 模块缓存也被预热，后续无 known 调用同样不联网
        assert etf_mod.get_listing_dates(["510050"]) == {"510050": "2004-12-30"}
        etf_mod._listing_date_cache.clear()

    def test_only_missing_codes_hit_network(self, monkeypatch):
        """仅缺失的代码联网，已缓存代码直接返回。"""
        from collectors import etf as etf_mod

        called = []

        class Resp:
            status_code = 200
            encoding = "utf-8"
            text = '<label>成立日期：<span>2011-09-20</span></label>'

        def fake_get(url, *a, **k):
            called.append(url)
            return Resp()

        monkeypatch.setattr(etf_mod.requests, "get", fake_get)
        monkeypatch.setattr(etf_mod, "throttle_wait", lambda: None)
        etf_mod._listing_date_cache.clear()
        known = {"510050": "2004-12-30"}
        result = etf_mod.get_listing_dates(["510050", "159915"], known=known)
        assert result["510050"] == "2004-12-30"
        assert result["159915"] == "2011-09-20"
        assert len(called) == 1, "仅 159915 应联网"
        assert "159915" in called[0]
        etf_mod._listing_date_cache.clear()
