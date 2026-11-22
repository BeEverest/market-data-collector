"""外部接口请求节流器。

背景：交易所与数据接口（尤其深交所 fund.szse.cn）对短时间内密集请求会
直接断开连接（RemoteDisconnected），表现为偶发采集失败。实测单次请求正常，
连续请求即被拒绝，故在请求层统一施加「最小间隔 + 随机抖动」，把请求节律
从「紧密连发」降为「近似人工节奏」，降低被反爬识别的概率。

设计：
    - 进程内单例 + 互斥锁，线程安全；Web 采集线程与调度线程共用同一节流器。
    - 默认 min_interval=0（不节流），完全兼容既有测试与本地Mock；
      仅在 main._collect / _retry_failed 从 config.fetch 显式配置后生效。
    - 抖动 (jitter) 让间隔不再机械固定，进一步规避规律性识别。
"""
from __future__ import annotations

import random
import threading
import time


class RateLimiter:
    """相邻请求至少间隔 min_interval 秒，并追加 0~jitter 秒随机抖动。"""

    def __init__(self, min_interval: float = 0.0, jitter: float = 0.0):
        self.min_interval = max(0.0, float(min_interval))
        self.jitter = max(0.0, float(jitter))
        self._last = 0.0
        self._lock = threading.Lock()

    def configure(self, min_interval: float | None = None,
                  jitter: float | None = None) -> None:
        with self._lock:
            if min_interval is not None:
                self.min_interval = max(0.0, float(min_interval))
            if jitter is not None:
                self.jitter = max(0.0, float(jitter))

    def wait(self) -> None:
        """在发起请求前调用；睡眠到满足最小间隔后再放行。"""
        if self.min_interval <= 0 and self.jitter <= 0:
            return
        with self._lock:
            now = time.monotonic()
            target = self._last + self.min_interval
            if now < target:
                time.sleep(target - now)
            if self.jitter > 0:
                time.sleep(random.uniform(0, self.jitter))
            self._last = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self._last = 0.0


# 进程内默认节流器；默认不节流，由采集入口按配置开启。
_default = RateLimiter()


def get_limiter() -> RateLimiter:
    return _default


def configure(min_interval: float | None = None, jitter: float | None = None) -> None:
    _default.configure(min_interval, jitter)


def wait() -> None:
    _default.wait()
