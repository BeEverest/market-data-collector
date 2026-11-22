"""定时调度配置测试：验证 run_type 合法取值与持久化。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import web_app


@pytest.fixture
def client(base, config_file, monkeypatch):
    monkeypatch.setattr(web_app, "CONFIG_PATH", config_file)
    monkeypatch.setattr(web_app, "BASE_DIR", base)
    with TestClient(web_app.app) as c:
        yield c


class TestSchedulerRunType:
    @pytest.mark.parametrize("run_type", ["full", "macro", "etf", "index"])
    def test_all_ui_run_types_are_accepted(self, client, run_type):
        r = client.post("/api/scheduler", json={"run_type": run_type})
        assert r.status_code == 200
        assert r.json()["config"]["run_type"] == run_type
        # 确认已落盘
        cfg = json.loads(web_app.CONFIG_PATH.read_text(encoding="utf-8"))
        assert cfg["scheduler"]["run_type"] == run_type

    def test_invalid_run_type_rejected(self, client):
        r = client.post("/api/scheduler", json={"run_type": "unknown"})
        assert r.status_code == 400
        assert "full/macro/etf/index" in r.json()["detail"]

    def test_enable_and_time_roundtrip(self, client):
        r = client.post("/api/scheduler", json={"enabled": True, "time": "09:15", "run_type": "index"})
        assert r.status_code == 200
        data = client.get("/api/scheduler").json()
        assert data["enabled"] is True and data["time"] == "09:15" and data["run_type"] == "index"
