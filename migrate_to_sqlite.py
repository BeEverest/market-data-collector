#!/usr/bin/env python3
"""把现有 CSV / JSONL / config.json 中的 ETF 配置一次性迁移到 SQLite。

幂等：使用业务主键 upsert，重复执行不会产生重复数据。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402
from main import _latest_csv  # noqa: E402
from monitor import Monitor  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate")


def main() -> int:
    raw = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    db_path = storage.db_path(raw, BASE_DIR)
    storage.init_schema(db_path)

    macro_path = _latest_csv("macro", raw)
    if macro_path:
        macro_df = pd.read_csv(macro_path, dtype={"基金代码": str})
        n = storage.upsert_macro(db_path, macro_df, "migration")
        logger.info("宏观数据迁移：%d 行 <- %s", n, macro_path.name)

    etf_path = _latest_csv("etf", raw)
    if etf_path:
        etf_df = pd.read_csv(etf_path, dtype={"基金代码": str})
        n = storage.upsert_etf(db_path, etf_df, "migration")
        logger.info("ETF 数据迁移：%d 行 <- %s", n, etf_path.name)

    history_path = BASE_DIR / raw.get("monitor", {}).get(
        "history_file", "output/monitor/interface_runs.jsonl")
    history = Monitor.load(history_path)
    if not history.empty:
        n = storage.append_interface_runs(db_path, history.to_dict("records"))
        logger.info("接口监控迁移：%d 条 <- %s", n, history_path.name)

    raw_etf = raw.get("etf", {}) or {}
    storage.save_etf_config(db_path, {
        "codes": raw_etf.get("codes", []),
        "listing_dates": raw_etf.get("listing_dates", {}),
        "start_date": raw_etf.get("start_date", ""),
        "end_date": raw_etf.get("end_date", ""),
        "default_trade_days": raw_etf.get("default_trade_days", 20),
    })
    storage.set_meta(db_path, "migrated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    counts = storage.status_counts(db_path)
    logger.info("迁移完成：宏观 %d 行 / %d 指标，ETF %d 行 / %d 标的，监控失败累计 %d",
                counts["macro_rows"], counts["macro_indicators"],
                counts["etf_rows"], counts["etf_codes"], counts["failed_total"])
    print(json.dumps({"db": str(db_path), **counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
