"""配置权威源测试：验证采集范围从 SQLite 读取，且旧版硬编码值能自动迁移。"""
from __future__ import annotations

import json

import pytest

import main
import storage


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    """构造一个临时 config.json + SQLite，隔离真实数据。"""
    db = tmp_path / "market.db"
    cfg = {
        "storage": {"backend": "sqlite", "db_path": str(db)},
        "etf": {"codes": ["510050"], "default_trade_days": 1,
                "start_date": "", "end_date": "", "listing_dates": {}},
        "index": {"codes": ["000001"], "lookback_days": 1,
                  "start_date": "", "end_date": ""},
        "macro": {"start_date": ""},
    }
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    storage.init_schema(db)
    monkeypatch.setattr(main, "BASE_DIR", tmp_path)
    return cfg_file, db, cfg


class TestConfigFromSqlite:
    def test_etf_days_read_from_sqlite_authority(self, cfg_env):
        cfg_file, db, _ = cfg_env
        # SQLite 写入用户显式值 3（与 config.json 的 1 不同）
        storage.save_etf_config(db, {"codes": ["510050"], "default_trade_days": 3,
                                     "start_date": "", "end_date": "", "listing_dates": {}})
        loaded = main.load_config(str(cfg_file))
        assert loaded["etf"]["default_trade_days"] == 3  # 以 SQLite 为准，而非 config.json

    def test_stale_20_migrated_to_default(self, cfg_env):
        cfg_file, db, _ = cfg_env
        storage.save_etf_config(db, {"codes": ["510050"], "default_trade_days": 20,
                                     "start_date": "", "end_date": "", "listing_dates": {}})
        loaded = main.load_config(str(cfg_file))
        assert loaded["etf"]["default_trade_days"] == storage.DEFAULT_ETF_CONFIG["default_trade_days"]
        # 迁移结果已写回 SQLite，重启后仍生效
        assert storage.get_etf_config(db)["default_trade_days"] == storage.DEFAULT_ETF_CONFIG["default_trade_days"]

    def test_stale_365_index_migrated(self, cfg_env):
        cfg_file, db, _ = cfg_env
        storage.save_index_config(db, {"codes": ["000001"], "lookback_days": 365,
                                       "start_date": "", "end_date": ""})
        loaded = main.load_config(str(cfg_file))
        assert loaded["index"]["lookback_days"] == storage.DEFAULT_INDEX_CONFIG["lookback_days"]

    def test_explicit_user_value_not_overridden(self, cfg_env):
        """非遗留值（如 5）不会被迁移逻辑覆盖，尊重用户显式配置。"""
        cfg_file, db, _ = cfg_env
        storage.save_etf_config(db, {"codes": ["510050"], "default_trade_days": 5,
                                     "start_date": "", "end_date": "", "listing_dates": {}})
        loaded = main.load_config(str(cfg_file))
        assert loaded["etf"]["default_trade_days"] == 5
