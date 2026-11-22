"""全局实时日志缓冲测试：覆盖环形上限、增量序号、调度日志捕获与 API。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import web_app


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

    def test_buffer_caps_at_200(self, clean_buffer):
        for i in range(250):
            _emit(f"line-{i}")
        with web_app._live_lock:
            logs = list(web_app._live_logs)
        assert len(logs) == web_app.LIVE_LOG_LIMIT == 200
        # 保留最新的 200 行：首行应为 line-50
        assert logs[0]["message"] == "line-50"
        assert logs[-1]["message"] == "line-249"

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
        assert data["limit"] == 200
        assert data["latest"] >= max(e["seq"] for e in data["logs"])
        assert data["total"] == 3

    def test_live_logs_empty_buffer(self, client, clean_buffer):
        data = client.get("/api/live-logs", params={"after": 0}).json()
        assert data["logs"] == []
        assert data["latest"] == 0
        assert data["total"] == 0
