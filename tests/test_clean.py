"""数据清理单元测试：按类别/细分口径/时间范围预览与删除，且不触碰配置与监控。"""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import storage
import web_app


@pytest.fixture
def seeded(base, db_path, macro_df, etf_df, run_records):
    storage.init_schema(db_path)
    storage.upsert_macro(db_path, macro_df, "seed")
    storage.upsert_etf(db_path, etf_df, "seed")
    storage.upsert_index(db_path, pd.DataFrame([
        {"指数代码": "000001", "指数名称": "上证指数", "交易日期": "2026-10-20",
         "开盘价": 3000.0, "最高价": 3050.0, "最低价": 2990.0, "收盘价": 3020.0,
         "成交量": 1e8, "涨跌幅(%)": 0.5, "数据源": "akshare", "采集时间": "2026-10-21 08:00:00"},
    ]), "seed")
    storage.append_interface_runs(db_path, run_records)
    storage.save_etf_config(db_path, {"codes": ["510050", "159915"]})
    return db_path


class TestPreviewClean:
    def test_macro_all(self, seeded):
        r = storage.preview_clean(seeded, "macro")
        assert r["matched"] == r["total"] == 3

    def test_macro_by_code(self, seeded):
        r = storage.preview_clean(seeded, "macro", code="M2")
        assert r["matched"] == 2 and r["total"] == 3

    def test_macro_by_month_range(self, seeded):
        r = storage.preview_clean(seeded, "macro", start="2026-10-01", end="2026-10-31")
        assert r["matched"] == 2  # 宏观按月截取，10 月两条

    def test_etf_by_code_and_date(self, seeded):
        r = storage.preview_clean(seeded, "etf", code="510050", start="2026-10-20")
        assert r["matched"] == 1 and r["total"] == 3
        assert r["sample"][0]["code"] == "510050"

    def test_etf_code_exact(self, seeded):
        assert storage.preview_clean(seeded, "etf", code="510050")["matched"] == 2

    def test_code_left_padded(self, seeded):
        # 指数代码库中为 000001，传入不足 6 位时自动补零
        assert storage.preview_clean(seeded, "index", code="1")["matched"] == 1

    def test_index_by_code(self, seeded):
        assert storage.preview_clean(seeded, "index", code="000001")["matched"] == 1

    def test_invalid_category(self, seeded):
        with pytest.raises(ValueError):
            storage.preview_clean(seeded, "unknown")


class TestCleanData:
    def test_delete_macro_subset_keeps_others(self, seeded):
        result = storage.clean_data(seeded, "macro", code="M2")
        assert result["before"] == 3 and result["after"] == 1 and result["deleted"] == 2
        total, df = storage.read_macro(seeded)
        assert total == 1 and df.iloc[0]["指标代码"] == "CPI"

    def test_delete_etf_by_date_range(self, seeded):
        result = storage.clean_data(seeded, "etf", start="2026-10-20", end="2026-10-20")
        assert result["deleted"] == 2 and result["after"] == 1
        total, df = storage.read_etf(seeded)
        assert total == 1 and df.iloc[0]["基金代码"] == "510050"

    def test_delete_index_all(self, seeded):
        result = storage.clean_data(seeded, "index")
        assert result["deleted"] == 1 and result["after"] == 0
        assert storage.read_index(seeded)[0] == 0

    def test_no_match_deletes_nothing(self, seeded):
        result = storage.clean_data(seeded, "etf", code="999999")
        assert result["deleted"] == 0 and result["after"] == result["before"]

    def test_config_monitor_meta_untouched(self, seeded):
        before_runs = len(storage.read_interface_runs(seeded))
        before_cfg = storage.get_etf_config(seeded, {})
        storage.clean_data(seeded, "macro")
        assert len(storage.read_interface_runs(seeded)) == before_runs
        assert storage.get_etf_config(seeded, {}) == before_cfg


class TestCleanApi:
    @pytest.fixture
    def client(self, base, config_file, monkeypatch, seeded):
        monkeypatch.setattr(web_app, "CONFIG_PATH", config_file)
        monkeypatch.setattr(web_app, "BASE_DIR", base)
        with TestClient(web_app.app) as c:
            yield c

    def test_preview_is_readonly(self, client):
        r = client.post("/api/clean/preview", json={"category": "macro", "code": "M2"})
        assert r.status_code == 200 and r.json()["matched"] == 2
        assert client.get("/api/status").json()["macro_rows"] == 3

    def test_execute_requires_confirm(self, client):
        r = client.post("/api/clean", json={"category": "macro", "code": "M2"})
        assert r.status_code == 400 and "confirm" in r.json()["detail"]

    def test_execute_deletes_and_reflects_in_status(self, client):
        r = client.post("/api/clean", json={
            "category": "macro", "code": "M2", "confirm": True
        })
        assert r.status_code == 200 and r.json()["deleted"] == 2
        assert client.get("/api/status").json()["macro_rows"] == 1

    def test_bad_category_rejected(self, client):
        assert client.post("/api/clean/preview", json={"category": "bad"}).status_code == 400
        assert client.post("/api/clean", json={"category": "bad", "confirm": True}).status_code == 400

    def test_full_scope_clears_category(self, client):
        r = client.post("/api/clean", json={"category": "etf", "confirm": True})
        assert r.status_code == 200 and r.json()["deleted"] == 3
        assert client.get("/api/status").json()["etf_rows"] == 0


class TestMonitorClean:
    def test_preview_by_status(self, seeded):
        r = storage.preview_monitor_clean(seeded, status="failed")
        assert r["matched"] == 2 and r["total"] == 4

    def test_preview_by_date_range(self, seeded):
        r = storage.preview_monitor_clean(seeded, start="2026-11-02", end="2026-11-02")
        assert r["matched"] == 2

    def test_preview_by_keyword(self, seeded):
        r = storage.preview_monitor_clean(seeded, keyword="sse_shares")
        assert r["matched"] == 2

    def test_preview_by_run_type_and_category(self, seeded):
        assert storage.preview_monitor_clean(seeded, run_type="retry")["matched"] == 1
        assert storage.preview_monitor_clean(seeded, category="ETF")["matched"] == 2

    def test_clean_deletes_only_interface_run(self, seeded):
        result = storage.clean_monitor_data(seeded, status="failed")
        assert result["deleted"] == 2 and result["after"] == 2
        # 业务数据不受影响
        assert storage.read_macro(seeded)[0] == 3
        assert storage.read_etf(seeded)[0] == 3

    def test_no_match_deletes_nothing(self, seeded):
        result = storage.clean_monitor_data(seeded, keyword="不存在")
        assert result["deleted"] == 0 and result["after"] == result["before"]


class TestMonitorCleanApi:
    @pytest.fixture
    def client(self, base, config_file, monkeypatch, seeded):
        monkeypatch.setattr(web_app, "CONFIG_PATH", config_file)
        monkeypatch.setattr(web_app, "BASE_DIR", base)
        with TestClient(web_app.app) as c:
            yield c

    def test_preview_readonly(self, client):
        r = client.post("/api/monitor/clean/preview", json={"status": "failed"})
        assert r.status_code == 200 and r.json()["matched"] == 2
        assert client.get("/api/monitor/report").json()["调用明细"]

    def test_execute_requires_confirm(self, client):
        r = client.post("/api/monitor/clean", json={"status": "failed"})
        assert r.status_code == 400 and "confirm" in r.json()["detail"]

    def test_execute_deletes(self, client):
        r = client.post("/api/monitor/clean", json={"status": "failed", "confirm": True})
        assert r.status_code == 200 and r.json()["deleted"] == 2
        assert client.get("/api/status").json()["failed_total"] == 0
