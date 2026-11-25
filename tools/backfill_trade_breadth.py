"""用东方财富 MCP 补齐全A涨跌家数历史（2015 年至今）。

数据源：mx_comprehensive_finance_data → 东方财富全A(沪深京)(800004.EI) 的
上涨家数 / 下跌家数 / 平盘家数，按交易日一行落 trade_breadth_raw。
口径提示：该指数含北交所，与本项目快照（沪深）口径略有差异，source 已标注；
计算层仅在快照缺失的历史日用它兜底，采集日起的快照日仍优先用快照派生值。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import storage  # noqa: E402
from backfill_trade_eastmoney import mcp_call  # noqa: E402
from processors.trade import recompute_trade  # noqa: E402

logger = logging.getLogger("backfill_breadth")
SOURCE = "东方财富MCP·东方财富全A(沪深京)涨跌家数"


def parse_breadth(data: dict) -> dict[str, dict]:
    """解析 MCP 返回，提取全A指数的上涨/下跌/平盘家数。"""
    out: dict[str, dict] = {}
    for sheet in data.get("data", []):
        name = str(sheet.get("sheetName", ""))
        if "800004" not in name and "东方财富全A" not in name:
            continue
        cols = sheet.get("columns", [])
        rows = sheet.get("items", [])
        if not cols or not rows:
            continue
        fields = {}
        for row in rows:
            key = str(row[0])
            if "上涨家数" in key:
                fields["up_count"] = row[1:]
            elif "下跌家数" in key:
                fields["down_count"] = row[1:]
            elif "平盘家数" in key:
                fields["flat_count"] = row[1:]
        if not fields:
            continue
        for idx, col in enumerate(cols[1:], start=1):
            day = str(col).split("(")[0][:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                continue
            record = {}
            for field, values in fields.items():
                if idx - 1 >= len(values):
                    continue
                m = re.search(r"(\d+)", str(values[idx - 1]))
                if m:
                    record[field] = int(m.group(1))
            if record:
                out[day] = record
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP 补全A涨跌家数历史")
    parser.add_argument("--db", default=str(ROOT / "output" / "market_data.db"))
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default=None, help="默认库中最后交易日")
    parser.add_argument("--no-recompute", action="store_true")
    parser.add_argument("--fill-missing", action="store_true",
                        help="只补缺失交易日（按明确日期列表分批查询，修年度查询返回空）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    end = args.end or storage.trade_raw_dates(args.db, "exchange")[-1]
    breadth: dict[str, dict] = {}
    if args.fill_missing:
        have = set(storage.trade_raw_dates(args.db, "breadth"))
        missing = [d for d in storage.trade_raw_dates(args.db, "exchange")
                   if args.start <= d <= end and d not in have]
        logger.info("按明确日期补缺：%d 个交易日", len(missing))
        for i in range(0, len(missing), 20):
            batch = missing[i:i + 20]
            query = (f"东方财富全A(沪深京)(800004.EI)指数在{'、'.join(batch)}这些交易日的"
                     "上涨家数、下跌家数、平盘家数，按日期列出")
            got = parse_breadth(mcp_call(query, tool="mx_comprehensive_finance_data"))
            logger.info("批次 %d/%d 返回 %d 日", i // 20 + 1, (len(missing) + 19) // 20, len(got))
            breadth.update(got)
    else:
        for year in range(int(args.start[:4]), int(end[:4]) + 1):
            lo = max(args.start, f"{year}-01-01")
            hi = min(end, f"{year}-12-31")
            query = (f"东方财富全A(沪深京)(800004.EI)指数在{lo}至{hi}每个交易日的"
                     "上涨家数、下跌家数、平盘家数，请完整列出所有交易日")
            logger.info("MCP 查询 %s ~ %s 全A涨跌家数", lo, hi)
            got = parse_breadth(mcp_call(query, tool="mx_comprehensive_finance_data"))
            logger.info("返回 %d 个交易日", len(got))
            breadth.update(got)

    today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [{
        "trade_date": day,
        "up_count": rec.get("up_count"),
        "down_count": rec.get("down_count"),
        "flat_count": rec.get("flat_count"),
        "source": SOURCE,
        "collected_at": today,
    } for day, rec in sorted(breadth.items())]
    df = pd.DataFrame(rows)
    run_id = f"backfill_breadth_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    written = storage.upsert_trade_breadth(args.db, df, run_id)
    logger.info("落库 %d 个交易日市场宽度", written)

    calc = 0
    if not args.no_recompute:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
        calc = recompute_trade(args.db, config=config, full=True)

    report = {"written": written, "mcp_days": len(breadth),
              "recomputed_days": calc, "source": SOURCE,
              "range": [args.start, end]}
    (ROOT / "output" / "trade_breadth_backfill_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
