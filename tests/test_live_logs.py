"""全局实时日志缓冲测试：覆盖环形上限、增量序号、调度日志捕获与 API。"""
from __future__ import annotations

import itertools
import logging
from collections import deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import web_app


@pytest.fixture(autouse=True)
def _isolate_live_log_file(tmp_path, monkeypatch):
    """把实时日志重定向到临时文件，避免 root logger 的测试日志污染生产 live_monitor.log。

    LiveLogHandler 常驻挂载在 root logger，任何测试用 logger 产生的日志都会被写入
    LIVE_LOG_FILE 指向的文件。若不隔离，test_buffer_caps_at_limit 等会向上万条
    line-N 假日志写进真实 output/live_monitor.log，前端监控面板会显示这些垃圾数据。
    """
    monkeypatch.setattr(web_app, "LIVE_LOG_FILE", tmp_path / "live_monitor.log")
    monkeypatch.setattr(web_app, "LIVE_LOG_MAX_BYTES", 15 * 1024 * 1024)
    monkeypatch.setattr(web_app, "LIVE_LOG_LIMIT", 10000)
    monkeypatch.setattr(web_app, "_live_logs", deque(maxlen=10000))
    monkeypatch.setattr(web_app, "_live_seq", itertools.count(1))
    monkeypatch.setattr(web_app, "_live_loaded", False)
    monkeypatch.setattr(web_app, "_live_file_lines", 0)


def _emit(message: str, level: int = logging.INFO):
    rec = logging.LogRecord("market_data.web", level, __file__, 1, message, (), None)
    web_app._root_logger.handle(rec)


@pytest.fixture
def clean_buffer():
    """隔离测试：备份并清空全局缓冲，结束后还原，避免污染其它测试。"""
    with web_app._live_lock:
        backup = list(web_app._live_logs)
        web_app._live_logs.clear()
    yield
    with web_app._live_lock:
        web_app._live_logs.clear()
        web_app._live_logs.extend(backup)


class TestLiveLogBuffer:
    def test_handler_attached_to_root(self):
        assert any(isinstance(h, web_app.LiveLogHandler) for h in web_app._root_logger.handlers)

    def test_global_isolation_keeps_production_file_untouched(self, clean_buffer):
        """任意测试 logger 写入临时日志，不得污染生产实时日志。"""
        production = Path(web_app.BASE_DIR) / "output" / "live_monitor.log"
        before = production.read_bytes() if production.exists() else b""
        logging.getLogger("collectors.trade").warning("全局日志隔离校验")
        after = production.read_bytes() if production.exists() else b""
        assert after == before

    def test_emit_captures_all_threads_logger(self, clean_buffer):
        # 用任意 logger 名（模拟采集器子 logger），都应传播到 root 被捕获
        logger = logging.getLogger("collectors.etf")
        old_level, old_prop = logger.level, logger.propagate
        logger.setLevel(logging.INFO)
        logger.propagate = True
        try:
            logger.info("测试子logger日志")
        finally:
            logger.setLevel(old_level)
            logger.propagate = old_prop
        with web_app._live_lock:
            msgs = [e["message"] for e in web_app._live_logs]
        assert "测试子logger日志" in msgs

    def test_entry_has_expected_fields(self, clean_buffer):
        _emit("字段校验日志")
        with web_app._live_lock:
            entry = list(web_app._live_logs)[-1]
        assert set(entry) == {"seq", "time", "level", "message"}
        assert entry["message"] == "字段校验日志"
        assert entry["level"] == "INFO"

    def test_buffer_caps_at_limit(self, clean_buffer):
        limit = web_app.LIVE_LOG_LIMIT
        for i in range(limit + 50):
            _emit(f"line-{i}")
        with web_app._live_lock:
            logs = list(web_app._live_logs)
        assert len(logs) == limit
        # 保留最新的 limit 行：首行应为 line-50
        assert logs[0]["message"] == f"line-50"
        assert logs[-1]["message"] == f"line-{limit + 49}"

    def test_seq_monotonic_increasing(self, clean_buffer):
        for i in range(5):
            _emit(f"seq-{i}")
        with web_app._live_lock:
            seqs = [e["seq"] for e in web_app._live_logs]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)


class TestLiveLogsApi:
    @pytest.fixture
    def client(self):
        with TestClient(web_app.app) as c:
            yield c

    def test_live_logs_after_filter(self, client, clean_buffer):
        _emit("A")
        _emit("B")
        _emit("C")
        with web_app._live_lock:
            first_seq = list(web_app._live_logs)[0]["seq"]
        data = client.get("/api/live-logs", params={"after": first_seq}).json()
        assert {e["message"] for e in data["logs"]} == {"B", "C"}
        assert data["limit"] == web_app.LIVE_LOG_LIMIT
        assert data["latest"] >= max(e["seq"] for e in data["logs"])
        assert data["total"] == 3

    def test_live_logs_empty_buffer(self, client, clean_buffer):
        data = client.get("/api/live-logs", params={"after": 0}).json()
        assert data["logs"] == []
        assert data["latest"] == 0
        assert data["total"] == 0
