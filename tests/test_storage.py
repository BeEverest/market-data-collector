"""storage.py 单元测试：业务主键、幂等写入、筛选分页、统计与配置。"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

import storage


class TestBizKeys:
    def test_macro_key_from_month(self):
        assert storage.macro_biz_key("M2", "2026-10") == "M2_202610"

    def test_macro_key_from_full_date(self):
        assert storage.macro_biz_key("M2", "2026-10-20") == "M2_202610"

    def test_etf_key_pads_code_and_strips_dashes(self):
        assert storage.etf_biz_key("51050", "2026-10-20") == "051050_20261020"
        assert storage.etf_biz_key("510050", "20261020") == "510050_20261020"

    def test_run_key_includes_sequence(self):
        key = storage.run_biz_key("run1", "iface", "2026-11-02 09:00:10", 7)
        assert key == "run1_iface_20261102_090010_007"

    def test_db_path_relative_and_absolute(self, base, config):
        relative = storage.db_path({"storage": {"db_path": "output/x.db"}}, base)
        assert relative == base / "output" / "x.db"
        absolute = storage.db_path(config, base)
        assert absolute.is_absolute() and absolute.name == "test.db"


class TestClean:
    def test_clean_nan_and_none(self):
        assert storage._clean(None) is None
        assert storage._clean(np.nan) is None
        assert storage._clean(float("nan")) is None
        assert storage._clean(0.0) == 0.0
        assert storage._clean("abc") == "abc"


class TestMeta:
    def test_schema_and_meta_roundtrip(self, db_path):
        storage.init_schema(db_path)
        assert storage.get_meta(db_path, "schema_version") == storage.SCHEMA_VERSION
        storage.set_meta(db_path, "migrated_at", "2026-11-22")
        assert storage.get_meta(db_path, "migrated_at") == "2026-11-22"
        assert storage.get_meta(db_path, "missing", "fallback") == "fallback"


class TestUpsert:
    def test_upsert_macro_is_idempotent_and_updates_values(self, db_path, macro_df):
        assert storage.upsert_macro(db_path, macro_df, "run_a") == 3
        assert storage.upsert_macro(db_path, macro_df, "run_b") == 3
        total, df = storage.read_macro(db_path)
        assert total == 3
        assert len(df) == 3
        m2_latest = df[(df["指标代码"] == "M2") & (df["日期"] == "2026-10")].iloc[0]
        assert m2_latest["数值"] == 3150000.0
        assert m2_latest["同比(%)"] == 8.2

    def test_upsert_macro_updates_same_key(self, db_path, macro_df):
        storage.upsert_macro(db_path, macro_df, "run_a")
        patch = macro_df.head(1).copy()
        patch["数值"] = 999.0
        patch["同比(%)"] = 9.9
        storage.upsert_macro(db_path, patch, "run_b")
        total, df = storage.read_macro(db_path)
        assert total == 3
        row = df[(df["指标代码"] == "M2") & (df["日期"] == "2026-10")].iloc[0]
        assert row["数值"] == 999.0
        assert row["同比(%)"] == 9.9

    def test_upsert_macro_ignores_empty(self, db_path):
        assert storage.upsert_macro(db_path, pd.DataFrame(), "run") == 0
        assert storage.upsert_macro(db_path, pd.DataFrame([{"指标代码": "", "日期": "2026-10"}])) == 0

    def test_upsert_etf_pads_code_and_is_idempotent(self, db_path, etf_df):
        assert storage.upsert_etf(db_path, etf_df, "run_a") == 3
        storage.upsert_etf(db_path, etf_df, "run_b")
        total, df = storage.read_etf(db_path)
        assert total == 3
        assert set(df["基金代码"]) == {"510050", "159915"}

    def test_append_interface_runs_uses_generated_biz_key(self, db_path, run_records):
        assert storage.append_interface_runs(db_path, run_records) == 4
        storage.append_interface_runs(db_path, run_records)
        df = storage.read_interface_runs(db_path)
        assert len(df) == 4
        assert set(df["status"]) == {"success", "failed"}

    def test_append_interface_runs_handles_nan_numbers(self, db_path):
        rec = {
            "run_id": "r1", "run_time": "2026-11-22 10:00:00",
            "interface_key": "k", "status": "success",
            "attempts": np.nan, "rows": np.nan, "duration_sec": np.nan,
        }
        storage.append_interface_runs(db_path, [rec])
        df = storage.read_interface_runs(db_path)
        assert df.iloc[0]["attempts"] == 0
        assert df.iloc[0]["rows"] == 0
        assert df.iloc[0]["duration_sec"] == 0.0


class TestRead:
    def test_read_macro_filters_and_paginates(self, db_path, macro_df):
        storage.upsert_macro(db_path, macro_df)
        total, page1 = storage.read_macro(db_path, code="M2", limit=1, offset=0)
        assert total == 2
        assert len(page1) == 1
        assert page1.iloc[0]["日期"] == "2026-10"  # DATE DESC
        _, page2 = storage.read_macro(db_path, code="M2", limit=1, offset=1)
        assert page2.iloc[0]["日期"] == "2026-09"
        by_range, _ = storage.read_macro(db_path, start="2026-09", end="2026-09")
        assert by_range == 1

    def test_read_etf_filters_code_and_date(self, db_path, etf_df):
        storage.upsert_etf(db_path, etf_df)
        total, df = storage.read_etf(db_path, code="510050", start="2026-10-20")
        assert total == 1
        assert df.iloc[0]["交易日期"] == "2026-10-20"

    def test_date_order_asc_desc(self, db_path, etf_df, macro_df):
        """日期排序：默认降序，asc 时升序；三类读取均支持。"""
        storage.upsert_etf(db_path, etf_df)
        _, etf_desc = storage.read_etf(db_path, code="510050")
        _, etf_asc = storage.read_etf(db_path, code="510050", date_order="asc")
        assert etf_desc.iloc[0]["交易日期"] == "2026-10-20"
        assert etf_asc.iloc[0]["交易日期"] == "2026-10-19"
        storage.upsert_macro(db_path, macro_df)
        _, macro_asc = storage.read_macro(db_path, code="M2", date_order="asc")
        assert macro_asc.iloc[0]["日期"] == "2026-09"
        # 非法值安全回退到降序（SQL 注入防护）
        _, fallback = storage.read_etf(db_path, code="510050", date_order="desc; DROP TABLE")
        assert fallback.iloc[0]["交易日期"] == "2026-10-20"
        # 未补零的代码也能命中
        assert storage.read_etf(db_path, code="510050")[0] == 2

    def test_empty_read_returns_named_columns(self, db_path):
        total, macro = storage.read_macro(db_path)
        assert total == 0 and list(macro.columns) == storage.MACRO_COLUMNS
        total, etf = storage.read_etf(db_path)
        assert total == 0 and list(etf.columns) == storage.ETF_COLUMNS

    def test_stats_and_status_counts(self, db_path, macro_df, etf_df, run_records):
        storage.upsert_macro(db_path, macro_df)
        storage.upsert_etf(db_path, etf_df)
        storage.append_interface_runs(db_path, run_records)
        mstats = storage.macro_stats(db_path)
        assert mstats["M2"]["rows"] == 2
        assert mstats["M2"]["last_date"] == "2026-10"
        estats = storage.etf_stats(db_path)
        assert estats["510050"]["first_date"] == "2026-10-19"
        counts = storage.status_counts(db_path)
        assert counts["macro_rows"] == 3
        assert counts["etf_rows"] == 3
        assert counts["macro_indicators"] == 2
        assert counts["failed_total"] == 2
        assert counts["last_run"]["run_type"] == "retry"


class TestEtfConfigAndExportLog:
    def test_etf_config_roundtrip_and_default_merge(self, db_path):
        default = {"codes": ["000001"], "default_trade_days": 20, "start_date": ""}
        cfg = storage.get_etf_config(db_path, default)
        assert cfg == default
        assert cfg is not default
        storage.save_etf_config(db_path, {"codes": ["510050"], "listing_dates": {"510050": "2004-12-30"}})
        loaded = storage.get_etf_config(db_path, default)
        assert loaded["codes"] == ["510050"]
        assert loaded["listing_dates"]["510050"] == "2004-12-30"
        assert loaded["default_trade_days"] == 20  # default 补齐未保存的字段
        assert default["codes"] == ["000001"]  # 不修改传入的 default

    def test_log_export_writes_row(self, db_path):
        storage.init_schema(db_path)
        storage.log_export(db_path, "macro", "/tmp/macro.zip", {"rows": 10})
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT scope, file_path, row_counts FROM export_log").fetchone()
        assert row[0] == "macro"
        assert row[1] == "/tmp/macro.zip"
        assert "rows" in row[2]


class TestClearAll:
    def test_clear_all_protects_collected_data_but_keeps_schema(self, db_path, macro_df, etf_df, run_records):
        storage.init_schema(db_path)
        storage.upsert_macro(db_path, macro_df, "run1")
        storage.upsert_etf(db_path, etf_df, "run1")
        storage.upsert_trade_breadth(db_path, pd.DataFrame([
            {"trade_date": "2026-11-24", "up_count": 3000,
             "down_count": 2000, "flat_count": 100}
        ]), "run1")
        storage.append_interface_runs(db_path, run_records)
        storage.save_etf_config(db_path, {"codes": ["510050"]})
        storage.save_index_config(db_path, {"codes": ["000001"]})
        storage.log_export(db_path, "macro", "/tmp/x.zip", {"rows": 1})
        assert storage.read_macro(db_path)[0] == 3

        removed = storage.clear_all(db_path)

        # 采集原数据全部保留
        assert "macro_observation" not in removed
        assert "etf_observation" not in removed
        assert all(table not in removed for table in storage.COLLECTED_DATA_TABLES)
        assert storage.read_macro(db_path)[0] == 3
        assert storage.read_etf(db_path)[0] == 3
        assert len(storage.read_trade_raw(db_path, "breadth")) == 1

        # 可清理范围正常清空，表结构仍在
        assert removed["interface_run"] == len(run_records)
        assert removed["etf_config"] == 1
        assert removed["index_config"] == 1
        assert removed["export_log"] == 1
        assert storage.read_interface_runs(db_path).empty
        # 幂等：再次清空不报错
        storage.clear_all(db_path)

    def test_clear_all_on_fresh_db(self, db_path):
        removed = storage.clear_all(db_path)
        assert set(removed) == set(storage.CLEAR_TABLES)
        # init_schema 会在 meta 写入 schema_version=1 行，其余业务表应为空
        assert removed["meta"] == 1
        assert all(v == 0 for k, v in removed.items() if k != "meta")

    def test_collected_tables_never_in_clear_scope(self):
        assert not set(storage.COLLECTED_DATA_TABLES) & set(storage.CLEAR_TABLES)
        assert not set(storage.IMMUTABLE_RAW_TABLES) & {
            spec["table"] for spec in storage.CLEAN_TARGETS.values()
        }


class TestCollectionStat:
    def test_append_and_read(self, db_path):
        stats = [
            {"run_id": "r1", "run_type": "full", "run_date": "2026-11-22",
             "category": "宏观", "code": "M2", "name": "货币供应量", "rows": 24,
             "collected_at": "2026-11-22 09:00:00"},
            {"run_id": "r1", "run_type": "full", "run_date": "2026-11-22",
             "category": "ETF", "code": "510050", "name": "50ETF", "rows": 20,
             "collected_at": "2026-11-22 09:00:00"},
            {"run_id": "r1", "run_type": "full", "run_date": "2026-11-22",
             "category": "指数", "code": "000001", "name": "上证指数", "rows": 250,
             "collected_at": "2026-11-22 09:00:00"},
        ]
        assert storage.append_collection_stats(db_path, stats) == 3
        df = storage.read_collection_stats(db_path)
        assert len(df) == 3
        assert set(df["category"]) == {"宏观", "ETF", "指数"}

    def test_same_run_code_overwrites(self, db_path):
        base = {"run_id": "r1", "run_type": "full", "run_date": "2026-11-22",
                "category": "宏观", "code": "M2", "name": "货币供应量", "rows": 10,
                "collected_at": "2026-11-22 09:00:00"}
        storage.append_collection_stats(db_path, [base])
        storage.append_collection_stats(db_path, [{**base, "rows": 24}])
        df = storage.read_collection_stats(db_path)
        assert len(df) == 1 and df.iloc[0]["rows"] == 24

    def test_etf_code_left_padded_in_biz_key(self, db_path):
        stat = {"run_id": "r1", "run_type": "full", "run_date": "2026-11-22",
                "category": "ETF", "code": "50050", "name": "50ETF", "rows": 5,
                "collected_at": "2026-11-22 09:00:00"}
        storage.append_collection_stats(db_path, [stat])
        # 06 位补零后 biz_key 稳定，重复写入同码仍覆盖
        storage.append_collection_stats(db_path, [{**stat, "rows": 6}])
        assert len(storage.read_collection_stats(db_path)) == 1

    def test_today_new_counts_only_real_inserts(self, db_path):
        """净新增只计真实 INSERT；重复 upsert 与接口返回行数累计不得混入。"""
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        df = pd.DataFrame([{
            "指标代码": "M2", "指标名称": "货币供应量(M2)", "日期": "2026-11",
            "数值": 3000000, "单位": "亿元", "采集时间": f"{today} 09:00:00",
        }])
        storage.upsert_macro(db_path, df, "r1")
        storage.upsert_macro(db_path, df, "r2")  # 同业务键更新，不是新增
        counts = storage.status_counts(db_path)
        assert counts["today_new"].get("宏观") == 1
        assert counts["today_written"].get("宏观", 0) == 0

        storage.append_collection_stats(db_path, [{
            "run_id": "r2", "run_type": "interface", "run_date": today,
            "category": "宏观", "code": "M2", "name": "货币供应量", "rows": 24,
            "collected_at": f"{today} 09:00:00",
        }])
        counts = storage.status_counts(db_path)
        assert counts["today_new"].get("宏观") == 1
        assert counts["today_written"].get("宏观") == 24
