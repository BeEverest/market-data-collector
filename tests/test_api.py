"""Web API 集成测试：基于临时 SQLite，验证看板查询、监控与下载链路。"""
from __future__ import annotations

import zipfile

import pytest
from fastapi.testclient import TestClient

import storage
import web_app


@pytest.fixture
def client(base, config, config_file, db_path, macro_df, etf_df, run_records, monkeypatch):
    storage.init_schema(db_path)
    storage.upsert_macro(db_path, macro_df, "test_run")
    storage.upsert_etf(db_path, etf_df, "test_run")
    storage.append_interface_runs(db_path, run_records)
    storage.save_etf_config(db_path, config["etf"])
    # 所有路径重定向到临时目录，真实 output/ 不受影响
    monkeypatch.setattr(web_app, "CONFIG_PATH", config_file)
    monkeypatch.setattr(web_app, "BASE_DIR", base)
    # 避免 ETF 名称解析触发外部网络请求
    monkeypatch.setattr(web_app, "get_fund_name_map", lambda: {})
    with TestClient(web_app.app) as c:
        yield c


class TestReadOnlyApis:
    def test_health(self, client):
        data = client.get("/api/health").json()
        assert data["status"] == "ok"
        assert "time" in data

    def test_status_counts(self, client):
        data = client.get("/api/status").json()
        assert data["macro_rows"] == 3
        assert data["macro_indicators"] == 2
        assert data["etf_rows"] == 3
        assert data["etf_codes"] == 2
        assert data["failed_total"] == 2
        assert data["scheduler"]["enabled"] is False
        assert data["running"] is False

    def test_macro_pagination_and_date_filter(self, client):
        r = client.get("/api/data/macro", params={"page": 1, "page_size": 2})
        data = r.json()
        assert r.status_code == 200
        assert data["total"] == 3 and data["pages"] == 2
        assert len(data["rows"]) == 2

        # 前端传 YYYY-MM-DD，后端按月截取
        filtered = client.get("/api/data/macro", params={
            "code": "M2", "start": "2026-09-01", "end": "2026-10-31", "page_size": 100,
        }).json()
        assert filtered["total"] == 2
        assert {row["日期"] for row in filtered["rows"]} == {"2026-09", "2026-10"}

    def test_macro_page_clamped_and_page_size_capped(self, client):
        data = client.get("/api/data/macro", params={"page": 99, "page_size": 999}).json()
        assert data["page"] == data["pages"] == 1  # 3 行 / 每页 100 行 = 1 页
        assert data["page_size"] == 100

    def test_etf_filter_by_code(self, client):
        data = client.get("/api/data/etf", params={"code": "510050"}).json()
        assert data["total"] == 2
        assert all(row["基金代码"] == "510050" for row in data["rows"])

    def test_monitor_report_and_failed(self, client):
        report = client.get("/api/monitor/report").json()
        assert set(report) == {"运行总览", "失败明细", "接口健康度", "调用明细", "采集量统计"}
        assert len(report["调用明细"]) == 4
        assert "采集量统计" in report  # 新增统计 sheet
        tasks = client.get("/api/monitor/failed").json()["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["interface_key"] == "etf.sse_shares"

    def test_catalog_and_etf_config(self, client):
        catalog = client.get("/api/catalog").json()
        assert len(catalog["macro"]) == 25
        assert {item["code"] for item in catalog["etf"]} == {"510050", "159915"}
        cfg = client.get("/api/etf/config").json()
        assert set(cfg["codes"]) == {"510050", "159915"}
        listing = {item["code"]: item["listing_date"] for item in cfg["items"]}
        assert listing["510050"] == "2004-12-30"


class TestExportApis:
    def test_export_macro_returns_zip_and_download(self, client, base):
        files = client.post("/api/export/macro").json()["files"]
        assert len(files) == 4
        assert files[-1].endswith(".zip")
        resp = client.get("/api/download", params={"name": files[-1]})
        assert resp.status_code == 200
        with zipfile.ZipFile(__import__("io").BytesIO(resp.content)) as zf:
            assert len(zf.namelist()) == 3

    def test_export_etf_monitor_and_full(self, client):
        for url, count in (
            ("/api/export/etf", 4),
            ("/api/export/monitor", 4),
            ("/api/export/full", 10),
        ):
            files = client.post(url).json()["files"]
            assert len(files) == count
            assert files[-1].endswith(".zip")
            resp = client.get("/api/download", params={"name": files[-1]})
            assert resp.status_code == 200
            assert resp.content[:2] == b"PK"

    def test_files_listing_excludes_exports(self, client):
        files = client.post("/api/export/macro").json()["files"]
        assert files[-1].endswith(".zip")  # 导出实际生成
        names = [f["name"] for f in client.get("/api/files").json()["files"]]
        assert not any(name.startswith("exports/") for name in names)  # 但不在输出文件展示

    def test_download_rejects_unknown_file(self, client):
        assert client.get("/api/download", params={"name": "missing.zip"}).status_code == 404
