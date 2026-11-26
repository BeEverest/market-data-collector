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

    def test_derived_api(self, client):
        r = client.get("/api/derived")
        assert r.status_code == 200
        data = r.json()
        assert {"month", "months", "rows", "count"}.issubset(data)
        recalc = client.post("/api/derived/recompute")
        assert recalc.status_code == 200 and recalc.json()["ok"] is True
        after = client.get("/api/derived").json()
        assert after["count"] >= data["count"]
        csv = client.get("/api/derived/export")
        assert csv.status_code == 200 and "text/csv" in csv.headers["content-type"]

    def test_derived_recompute_partial(self, client):
        """局部重算接受 month/code，跳过非法参数，且不影响其他派生行。"""
        client.post("/api/derived/recompute")  # 先全量建立基线
        baseline = client.get("/api/derived").json()["count"]
        assert baseline > 0

        r = client.post("/api/derived/recompute", json={"month": "2026-10"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["month"] == "2026-10"
        assert client.get("/api/derived").json()["count"] == baseline

        r = client.post("/api/derived/recompute", json={"code": "M2_YOY"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["code"] == "M2_YOY"
        assert client.get("/api/derived").json()["count"] == baseline

        r = client.post("/api/derived/recompute", json={"month": "2026-10", "code": "M2_YOY"})
        assert r.status_code == 200 and r.json()["ok"] is True

        assert client.post("/api/derived/recompute", json={"month": "bad"}).status_code == 400
        assert client.post("/api/derived/recompute", json={"code": "NOPE"}).status_code == 400

    def test_macro_config_roundtrip(self, client):
        """宏观配置日期区间可真实读写，非法月份被拒绝。"""
        cfg = client.get("/api/macro/config").json()
        assert cfg["start_date"] == "2024-01"
        saved = client.post("/api/macro/config", json={
            "start_date": "2025-03-01", "end_date": "2025-06-30",
        })
        assert saved.status_code == 200
        after = client.get("/api/macro/config").json()
        assert after["start_date"] == "2025-03-01"
        assert after["end_date"] == "2025-06-30"
        bad = client.post("/api/macro/config", json={"start_date": "not-month"})
        assert bad.status_code == 400

    def test_etf_tags_and_change_columns(self, client):
        cfg = client.get("/api/etf/config").json()
        assert cfg["tag_options"] == ["宽基", "汇金", "黄金"]
        code = cfg["codes"][0]
        saved = client.post("/api/etf/config", json={"tags": {code: ["黄金", "宽基"]}})
        assert saved.status_code == 200
        item = next(x for x in saved.json()["config"]["items"] if x["code"] == code)
        assert item["tags"] == ["宽基", "黄金"]  # 按选项顺序归一
        bad = client.post("/api/etf/config", json={"tags": {code: ["未知标签"]}})
        assert bad.status_code == 400

        data = client.get("/api/data/etf", params={"code": "510050", "page_size": 100}).json()
        row = next(r for r in data["rows"] if r["交易日期"] == "2026-10-20")
        assert row["标签"] == "宽基、黄金"
        assert row["份额变化(亿份)"] == pytest.approx(2.0)
        assert row["份额变化率(%)"] == pytest.approx(2 / 98 * 100, abs=1e-3)
        assert row["规模变化(亿元)"] == pytest.approx(8.96)
        assert row["规模变化率(%)"] == pytest.approx(8.96 / 341.04 * 100, abs=1e-3)

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

    def test_data_date_order_asc(self, client):
        """order=asc 时交易日期升序，默认 desc 降序。"""
        desc = client.get("/api/data/etf", params={"code": "510050"}).json()
        asc = client.get("/api/data/etf", params={"code": "510050", "order": "asc"}).json()
        assert desc["rows"][0]["交易日期"] == "2026-10-20"
        assert asc["rows"][0]["交易日期"] == "2026-10-19"
        # 宏观接口同样支持
        m_asc = client.get("/api/data/macro", params={"code": "M2", "order": "asc"}).json()
        assert m_asc["rows"][0]["日期"] == "2026-09"

    def test_monitor_report_and_failed(self, client):
        report = client.get("/api/monitor/report").json()
        assert set(report) == {"运行总览", "失败明细", "接口健康度", "调用明细", "采集量统计"}
        assert len(report["调用明细"]) == 4
        assert "采集量统计" in report  # 新增统计 sheet
        tasks = client.get("/api/monitor/failed").json()["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["interface_key"] == "etf.sse_shares"

    def test_monitor_failure_detail_has_task_id(self, client):
        fails = client.get("/api/monitor/report").json()["失败明细"]
        assert all("任务ID" in row for row in fails)
        tid = next(row["任务ID"] for row in fails
                   if row["运行ID"] == "20261102_090000_full")
        assert len(tid) == 12

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


class TestRetryTaskApi:
    def test_missing_task_id_rejected(self, client):
        resp = client.post("/api/retry/task", json={})
        assert resp.status_code == 400
        assert "task_id" in resp.json()["detail"]

    def test_retry_task_looks_up_single_failure_and_runs(self, client, monkeypatch):
        import main as main_mod
        from monitor import failure_task_id

        captured = {}

        def fake_retry(config, monitor_cfg, tasks=None):
            captured["tasks"] = tasks
            return 0

        monkeypatch.setattr(main_mod, "_retry_failed", fake_retry)
        tid = failure_task_id("etf.sse_shares", '{"date": "2026-11-01"}')

        result = web_app._do_retry_task(tid)
        assert result == "单条重跑完成"
        assert len(captured["tasks"]) == 1
        assert captured["tasks"][0]["interface_key"] == "etf.sse_shares"
        assert captured["tasks"][0]["task_id"] == tid

    def test_retry_task_unknown_id(self, client):
        result = web_app._do_retry_task("not-exist")
        assert "未找到" in result

    def test_retry_task_endpoint_submits_job(self, client, monkeypatch):
        called = {}

        def fake_submit(kind, func):
            called["kind"] = kind
            called["func"] = func
            return "fake-job-id"

        monkeypatch.setattr(web_app, "_submit", fake_submit)
        resp = client.post("/api/retry/task", json={"task_id": "abc123"})
        assert resp.status_code == 200
        assert resp.json()["job_id"] == "fake-job-id"
        assert called["kind"] == "retry"
        assert callable(called["func"])


class TestTradeRawApi:
    """采集系统·日交易预览：5 张交易原始表均可预览，非法表名被拒。"""

    def test_all_five_tables_supported(self, client, db_path):
        import pandas as pd
        d = "2026-11-24"
        storage.upsert_trade_exchange(db_path, pd.DataFrame([
            {"trade_date": d, "sse_amount": 5000.0, "szse_amount": 6e11}]), "t")
        storage.upsert_trade_margin(db_path, pd.DataFrame([
            {"trade_date": d, "financing_balance": 1.8e12}]), "t")
        storage.upsert_trade_snapshot(db_path, pd.DataFrame([
            {"trade_date": d, "code": "000001", "name": "平安银行", "pct_change": 1.2}]), "t")
        storage.upsert_trade_breadth(db_path, pd.DataFrame([
            {"trade_date": d, "up_count": 3000, "down_count": 2000, "flat_count": 100}]), "t")
        storage.upsert_trade_congestion(db_path, pd.DataFrame([
            {"trade_date": d, "concentration5": 47.64, "close_price": 3548.37}]), "t")
        for table in ("exchange", "margin", "snapshot", "breadth", "congestion"):
            r = client.get(f"/api/trade/raw?table={table}")
            assert r.status_code == 200, table
            assert r.json()["total"] >= 1, table
            assert r.json()["rows"], table
        breadth = client.get("/api/trade/raw?table=breadth").json()["rows"][0]
        assert breadth["up_count"] == 3000
        cong = client.get("/api/trade/raw?table=congestion").json()["rows"][0]
        assert cong["concentration5"] == 47.64

    def test_invalid_table_rejected(self, client):
        r = client.get("/api/trade/raw?table=not_a_table")
        assert r.status_code == 400


class TestTradeDataPreview:
    """采集数据·日交易 Tab：中文字段表头、隐藏 run_id；首页静态资源版本号注入。"""

    def test_chinese_headers_and_no_run_id(self, client, db_path):
        import pandas as pd
        d = "2026-11-24"
        storage.upsert_trade_breadth(db_path, pd.DataFrame([
            {"trade_date": d, "up_count": 3000, "down_count": 2000, "flat_count": 100}
        ]), "run_should_not_leak")
        data = client.get(
            f"/api/data/trade?code=breadth&start={d}&end={d}"
        ).json()
        assert data["total"] >= 1
        row = data["rows"][0]
        assert row["交易日期"] == d
        assert row["上涨家数"] == 3000
        assert row["下跌家数"] == 2000
        assert row["平盘家数"] == 100
        assert "run_id" not in row
        assert "trade_date" not in row
        assert "业务键" in row

    def test_chinese_date_sortable(self, client, db_path):
        import pandas as pd
        for d in ("2026-11-20", "2026-11-24"):
            storage.upsert_trade_breadth(db_path, pd.DataFrame([
                {"trade_date": d, "up_count": 1}
            ]), "t")
        desc = client.get("/api/data/trade?code=breadth&order=desc").json()["rows"]
        asc = client.get("/api/data/trade?code=breadth&order=asc").json()["rows"]
        assert desc[0]["交易日期"] > desc[-1]["交易日期"]
        assert asc[0]["交易日期"] < asc[-1]["交易日期"]

    def test_amount_fields_in_trillion_two_decimals(self, client, db_path):
        """金额字段统一换算为万亿、两位小数；非金额字段保持原样。"""
        import pandas as pd
        d = "2026-11-24"
        # 亿元 ÷ 1e4 → 万亿；元 ÷ 1e12 → 万亿
        storage.upsert_trade_exchange(db_path, pd.DataFrame([{
            "trade_date": d,
            "sse_amount": 7745.44,        # 亿元 → 0.77 万亿
            "sse_float_mv": 560511.01,    # 亿元 → 56.05 万亿
            "szse_amount": 883979787524.54,  # 元 → 0.88 万亿
        }]), "t")
        row = client.get(f"/api/data/trade?code=exchange&start={d}&end={d}").json()["rows"][0]
        assert row["沪市成交额(万亿)"] == 0.77
        assert row["沪市流通市值(万亿)"] == 56.05
        assert row["深市成交额(万亿)"] == 0.88
        assert "沪市成交额(亿元)" not in row
        assert "run_id" not in row

    def test_snapshot_amount_in_million(self, client, db_path):
        """全A个股快照成交额用百万：元 ÷ 1e6，保留两位小数。"""
        import pandas as pd
        d = "2026-11-24"
        storage.upsert_trade_snapshot(db_path, pd.DataFrame([{
            "trade_date": d, "code": "000001", "name": "平安银行",
            "amount": 1511830.0,  # 元 → 1.51 百万
        }]), "t")
        row = client.get(f"/api/data/trade?code=snapshot&start={d}&end={d}").json()["rows"][0]
        assert row["成交额(百万)"] == 1.51
        assert "成交额(万亿)" not in row
        assert "成交额(亿)" not in row

    def test_non_amount_fields_unchanged(self, client, db_path):
        """成交量(手)、家数等非金额字段不做万亿换算。"""
        import pandas as pd
        d = "2026-11-24"
        storage.upsert_trade_exchange(db_path, pd.DataFrame([{
            "trade_date": d, "sse_volume": 496.59,
        }]), "t")
        row = client.get(f"/api/data/trade?code=exchange&start={d}&end={d}").json()["rows"][0]
        assert row["沪市成交量(手)"] == 496.59

    def test_index_injects_asset_versions(self, client, base):
        static = base / "static"
        static.mkdir(exist_ok=True)
        (static / "index.html").write_text(
            '<link rel="stylesheet" href="/static/style.css">'
            '<script src="/static/app.js"></script>',
            encoding="utf-8",
        )
        (static / "style.css").write_text("body{}", encoding="utf-8")
        (static / "app.js").write_text("//js", encoding="utf-8")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "/static/style.css?v=" in resp.text
        assert "/static/app.js?v=" in resp.text
        assert resp.headers["cache-control"] == "no-store"
