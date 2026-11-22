"""请求节流器单元测试：默认关闭、最小间隔、抖动与配置。"""
from __future__ import annotations

import time

from collectors.rate_limit import RateLimiter


class TestRateLimiter:
    def test_disabled_by_default_does_not_sleep(self, monkeypatch):
        calls = []
        monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))
        limiter = RateLimiter()  # min_interval=0, jitter=0
        limiter.wait()
        limiter.wait()
        assert calls == []

    def test_min_interval_enforced(self, monkeypatch):
        sleeps = []
        clock = {"t": 100.0}

        def fake_monotonic():
            return clock["t"]

        def fake_sleep(sec):
            sleeps.append(sec)
            clock["t"] += sec

        monkeypatch.setattr(time, "monotonic", fake_monotonic)
        monkeypatch.setattr(time, "sleep", fake_sleep)
        limiter = RateLimiter(min_interval=1.0)
        limiter.wait()          # 首次：_last=0，不等待
        clock["t"] += 0.3       # 仅过 0.3s
        limiter.wait()          # 应补睡 0.7s
        assert sleeps and abs(sleeps[0] - 0.7) < 1e-6

    def test_configure_updates_limits(self):
        limiter = RateLimiter()
        limiter.configure(min_interval=2.0, jitter=0.5)
        assert limiter.min_interval == 2.0
        assert limiter.jitter == 0.5
        limiter.configure(min_interval=-1)  # 负数归一为 0
        assert limiter.min_interval == 0.0
