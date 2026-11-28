"""指数采集扩展单元测试：采集器、存储、导出与 API。"""
from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import exporter
import storage
from collectors import index as idx_mod


# ---------------- 采集器 ----------------
class TestIndexCollector:
    def test_catalog_has_six_broad_indices_plus_benchmark(self):
        cat = idx_mod.catalog()
        codes = {it["code"] for it in cat}
        # 000905 中证500 为 Web 新增指数预设项，必须在目录内才能保存配置并采集。
        assert codes == {"000001", "000016", "000300", "000510", "000905", "000852", "000688", "399006"}
        names = {it["code"]: it["name"] for it in cat}
        assert names["000852"] == "中证1000"  # 对应看板沿用的「中证A1000」ETF 标签
        assert names["000905"] == "中证500"
        assert all(it["granularity"] == "交易日" for it in cat)

    def test_broad_index_codes_are_cataloged(self):
        """宽基看板使用的六个指数必须全部可采集，避免合计图退化为无指数叠加。"""
        from processors.dashboard import BROAD_INDEX_SPECS
        catalog_codes = {it["code"] for it in idx_mod.catalog()}
        spec_codes = {code for _, _, code in BROAD_INDEX_SPECS}
        assert spec_codes == {"000016", "000300", "000510", "000852", "000688", "399006"}
        assert spec_codes <= catalog_codes

    def test_output_columns_include_pct_change(self):
        assert "涨跌幅(%)" in idx_mod.OUTPUT_COLUMNS
        assert "成交量" in idx_mod.OUTPUT_COLUMNS
        assert idx_mod.OUTPUT_COLUMNS[-2:] == ["数据源", "采集时间"]

    def test_default_range_is_one_year(self):
        start, end = idx_mod._default_range(365)
        from datetime import date
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 365

    def test_collect_with_mocked_akshare(self, monkeypatch):
        """Mock 底层接口，验证范围过滤、字段与涨跌幅计算。"""
        dates = ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21"]
        raw = pd.DataFrame({
            "date": dates,
            "open": [10.0, 11.0, 12.0, 13.0],
            "high": [10.5, 11.5, 12.5, 13.5],
            "low": [9.5, 10.5, 11.5, 12.5],
            "close": [10.0, 11.0, 12.0, 12.0],
            "volume": [100, 110, 120, 130],
        })
        monkeypatch.setattr(idx_mod, "_fetch_index", lambda symbol, **kw: raw.copy())

        df = idx_mod.collect_index(
            codes=["000001"], start_date="2026-11-19", end_date="2026-11-20", retries=1
        )
        assert list(df.columns) == idx_mod.OUTPUT_COLUMNS
        assert len(df) == 2
        assert df["指数代码"].tolist() == ["000001", "000001"]
        # 涨跌幅基于完整历史：11-19 基于 11-18(10) -> 10%；11-20 基于 11-19(11) -> 9.09%
        assert round(df.iloc[0]["涨跌幅(%)"], 2) == 10.0
        assert round(df.iloc[1]["涨跌幅(%)"], 2) == 9.09

    def test_collect_unknown_code_returns_empty(self):
        df = idx_mod.collect_index(codes=["999999"])
        assert df.empty and list(df.columns) == idx_mod.OUTPUT_COLUMNS

    def test_default_range_is_last_completed_trade_day(self, monkeypatch):
        """两端日期留空时，默认采集最近一个已结束交易日（单日）。"""
        raw = pd.DataFrame({
            "date": ["2026-11-18", "2026-11-19", "2026-11-20"],
            "open": [10, 11, 12], "high": [11, 12, 13],
            "low": [9, 10, 11], "close": [10, 11, 12], "volume": [100, 110, 120],
        })
        monkeypatch.setattr(idx_mod, "_fetch_index", lambda symbol, **kw: raw.copy())
        monkeypatch.setattr(idx_mod, "last_completed_trade_day", lambda: "2026-11-20")

        df = idx_mod.collect_index(codes=["000001"])
        assert df["交易日期"].tolist() == ["2026-11-20"]
        # 单日采集也必须带涨跌幅（基于前一日 11 -> 12 = 9.09%），不能因裁剪成 1 行而变 NaN
        assert not pd.isna(df.iloc[0]["涨跌幅(%)"])
        assert round(df.iloc[0]["涨跌幅(%)"], 2) == 9.09

    def test_single_day_collect_keeps_pct_change(self, monkeypatch):
        """显式单日采集（定时调度口径）涨跌幅非空——回归 11-20 缺失问题。"""
        raw = pd.DataFrame({
            "date": ["2026-11-18", "2026-11-19", "2026-11-20"],
            "open": [10, 11, 12], "high": [11, 12, 13],
            "low": [9, 10, 11], "close": [10, 11, 12], "volume": [100, 110, 120],
        })
        monkeypatch.setattr(idx_mod, "_fetch_index", lambda symbol, **kw: raw.copy())

        df = idx_mod.collect_index(
            codes=["000001"], start_date="2026-11-20", end_date="2026-11-20"
        )
        assert len(df) == 1
        assert round(df.iloc[0]["涨跌幅(%)"], 2) == 9.09

    def test_explicit_date_still_wins(self, monkeypatch):
        """显式 start/end 优先于默认锚点。"""
        raw = pd.DataFrame({
            "date": ["2026-11-18", "2026-11-19", "2026-11-20"],
            "open": [10, 11, 12], "high": [11, 12, 13],
            "low": [9, 10, 11], "close": [10, 11, 12], "volume": [100, 110, 120],
        })
        monkeypatch.setattr(idx_mod, "_fetch_index", lambda symbol, **kw: raw.copy())
        monkeypatch.setattr(idx_mod, "last_completed_trade_day", lambda: "2026-11-20")

        df = idx_mod.collect_index(codes=["000001"], start_date="2026-11-18", end_date="2026-11-19")
        assert set(df["交易日期"]) == {"2026-11-18", "2026-11-19"}


@pytest.fixture
def index_df() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "指数代码": "000001", "指数名称": "上证指数", "交易日期": "2026-11-20",
            "开盘价": 3000.0, "最高价": 3050.0, "最低价": 2990.0, "收盘价": 3040.0,
            "成交量": 1.0e8, "涨跌幅(%)": 1.5, "数据源": "新浪财经",
            "采集时间": "2026-11-22 21:00:00",
        },
        {
            "指数代码": "000300", "指数名称": "沪深300", "交易日期": "2026-11-20",
            "开盘价": 4000.0, "最高价": 4050.0, "最低价": 3980.0, "收盘价": 4020.0,
            "成交量": 2.0e8, "涨跌幅(%)": 0.8, "数据源": "新浪财经",
            "采集时间": "2026-11-22 21:00:00",
        },
        {
            "指数代码": "000001", "指数名称": "上证指数", "交易日期": "2026-11-19",
            "开盘价": 2980.0, "最高价": 3010.0, "最低价": 2970.0, "收盘价": 2995.0,
            "成交量": 9.0e7, "涨跌幅(%)": np.nan, "数据源": "新浪财经",
            "采集时间": "2026-11-22 21:00:00",
        },
    ])


# ---------------- 存储 ----------------
class TestIndexStorage:
    def test_biz_key(self):
        assert storage.index_biz_key("1", "2026-11-20") == "000001_20261120"
        assert storage.index_biz_key("399006", "20261120") == "399006_20261120"

    def test_upsert_idempotent(self, db_path, index_df):
        assert storage.upsert_index(db_path, index_df, "run1") == 3
        assert storage.upsert_index(db_path, index_df, "run2") == 3
        total, _ = storage.read_index(db_path)
        assert total == 3

    def test_read_filter_and_pagination(self, db_path, index_df):
        storage.upsert_index(db_path, index_df, "run1")
        total, df = storage.read_index(db_path, code="000001")
        assert total == 2 and set(df["交易日期"]) == {"2026-11-19", "2026-11-20"}
        total2, df2 = storage.read_index(db_path, start="2026-11-20", limit=1, offset=0)
        assert total2 == 2 and len(df2) == 1
        _, all_df = storage.read_index(db_path)
        assert list(all_df.columns) == storage.INDEX_COLUMNS
        assert "涨跌幅(%)" in all_df.columns

    def test_index_stats(self, db_path, index_df):
        storage.upsert_index(db_path, index_df, "run1")
        stats = storage.index_stats(db_path)
        assert stats["000001"]["rows"] == 2
        assert stats["000300"]["rows"] == 1
        assert stats["000001"]["last_date"] == "2026-11-20"

    def test_status_counts_include_index(self, db_path, index_df):
        storage.upsert_index(db_path, index_df, "run1")
        counts = storage.status_counts(db_path)
        assert counts["index_rows"] == 3
        assert counts["index_codes"] == 2

    def test_index_config_roundtrip(self, db_path):
        cfg = {"codes": ["000001", "399006"], "lookback_days": 180,
               "start_date": "", "end_date": ""}
        storage.save_index_config(db_path, cfg)
        got = storage.get_index_config(db_path)
        assert got["codes"] == ["000001", "399006"]
        assert got["lookback_days"] == 180

    def test_index_config_uses_default_when_missing(self, db_path):
        got = storage.get_index_config(db_path, {"codes": ["000001"], "lookback_days": 365})
        assert got["codes"] == ["000001"]
        assert got["lookback_days"] == 365


# ---------------- 导出 ----------------
class TestIndexExport:
    def test_export_index_files_and_zip(self, base, config, index_df):
        files = exporter.export_index(index_df, base, config, stamp="test")
        names = [Path(f).name for f in files]
        assert names == [
            "指数数据_test.csv", "指数数据口径说明_test.csv",
            "指数数据_test.xlsx", "指数数据_test.zip",
        ]
        with zipfile.ZipFile(files[-1]) as zf:
            assert len(zf.namelist()) == 3

    def test_notes_include_pct_change(self):
        notes = exporter._notes_for({"指数", "数据源", "注意"})
        assert list(notes.columns) == ["类别", "字段 / 指标", "口径说明"]
        assert any("涨跌幅" in str(v) for v in notes["字段 / 指标"])


# ---------------- API ----------------
@pytest.fixture
def index_client(base, config, config_file, db_path, index_df, monkeypatch):
    config["index"] = {"codes": ["000001", "000300"],
                       "lookback_days": 365, "start_date": "", "end_date": ""}
    config_file.write_text(__import__("json").dumps(config, ensure_ascii=False), encoding="utf-8")
    storage.init_schema(db_path)
    storage.upsert_index(db_path, index_df, "test_run")
    storage.save_index_config(db_path, config["index"])
    monkeypatch.setattr(web_app_module, "CONFIG_PATH", config_file)
    monkeypatch.setattr(web_app_module, "BASE_DIR", base)
    with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(
        web_app_module.app
    ) as c:
        yield c


import web_app as web_app_module  # noqa: E402


class TestIndexApi:
    def test_data_index_pagination_and_filter(self, index_client):
        data = index_client.get("/api/data/index", params={"page": 1, "page_size": 2}).json()
        assert data["total"] == 3 and data["pages"] == 2
        assert len(data["rows"]) == 2
        assert all("涨跌幅(%)" in row for row in data["rows"])

        by_code = index_client.get("/api/data/index", params={"code": "000001"}).json()
        assert by_code["total"] == 2

    def test_status_includes_index(self, index_client):
        data = index_client.get("/api/status").json()
        assert data["index_rows"] == 3
        assert data["index_codes"] == 2

    def test_catalog_includes_index(self, index_client):
        data = index_client.get("/api/catalog").json()
        assert any(it["code"] == "000001" for it in data["index"])
        assert "index_lookback" in data["defaults"]

    def test_index_config_get_and_set(self, index_client):
        cfg = index_client.get("/api/index/config").json()
        assert set(cfg["codes"]) == {"000001", "000300"}

        res = index_client.post("/api/index/config", json={
            "codes": ["000001", "399006"], "lookback_days": 180,
        })
        assert res.status_code == 200
        cfg2 = index_client.get("/api/index/config").json()
        assert set(cfg2["codes"]) == {"000001", "399006"}
        assert cfg2["lookback_days"] == 180

    def test_index_config_rejects_unknown_code(self, index_client):
        res = index_client.post("/api/index/config", json={"codes": ["999999"]})
        assert res.status_code == 400

    def test_export_index_download(self, index_client):
        res = index_client.post("/api/export/index")
        assert res.status_code == 200
        files = res.json()["files"]
        assert files[-1].endswith(".zip")
        dl = index_client.get("/api/download", params={"name": files[-1]})
        assert dl.status_code == 200
