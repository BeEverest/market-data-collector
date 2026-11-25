"""定时调度配置测试：验证 run_type 合法取值与持久化。"""
from __future__ import annotations

import json

import pandas as pd
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


def test_scheduled_collection_recomputes_derived(base, config_file, monkeypatch):
    """派生指标由定时调度触发；手动/普通采集不触发。"""
    monkeypatch.setattr(web_app, "CONFIG_PATH", config_file)
    monkeypatch.setattr(web_app, "BASE_DIR", base)
    monkeypatch.setattr(web_app, "_collect", lambda *a, **k: (pd.DataFrame(), pd.DataFrame(), pd.DataFrame()))
    monkeypatch.setattr(web_app, "_finish", lambda *a, **k: None)
    monkeypatch.setattr(web_app.storage, "status_counts", lambda path: {
        "macro_rows": 0, "etf_rows": 0, "index_rows": 0,
    })
    called = []
    # 定时调度以增量模式触发，必须传入本次采集 run_id
    monkeypatch.setattr(web_app, "_recompute_derived",
                        lambda cfg, trigger="手动", run_id=None: called.append((trigger, run_id)) or 3)

    web_app._do_collect(trigger="定时调度")
    assert len(called) == 1
    assert called[0][0] == "定时调度"
    assert called[0][1]  # run_id 非空，供增量识别本次变更
    web_app._do_collect(trigger="Web")
    assert len(called) == 1  # 手动/普通采集不触发派生重算


def test_scheduled_macro_uses_current_month_only(base, config_file, monkeypatch):
    """定时调度传 macro_current_month=True，手动采集传 False。"""
    monkeypatch.setattr(web_app, "CONFIG_PATH", config_file)
    monkeypatch.setattr(web_app, "BASE_DIR", base)
    captured = {}
    monkeypatch.setattr(
        web_app, "_collect",
        lambda *a, **k: captured.update(k) or (pd.DataFrame(), pd.DataFrame(), pd.DataFrame()),
    )
    monkeypatch.setattr(web_app, "_finish", lambda *a, **k: None)
    monkeypatch.setattr(web_app.storage, "status_counts", lambda path: {
        "macro_rows": 0, "etf_rows": 0, "index_rows": 0,
    })
    monkeypatch.setattr(web_app, "_recompute_derived", lambda *a, **k: 0)

    web_app._do_collect(trigger="定时调度")
    assert captured["macro_current_month"] is True
    web_app._do_collect(trigger="Web")
    assert captured["macro_current_month"] is False
