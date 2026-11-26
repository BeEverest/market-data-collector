"""按明确交易日，用东方财富 MCP 历史行情校正/补齐全A个股快照原始表。

背景：`ak.stock_zh_a_spot()` 是实时快照，盘中获取的是当日盘中值；旧逻辑在
时间戳只有 HH:MM:SS 时回退到「最近已结束交易日」，会把盘中值错标到前一个
交易日。本脚本只按明确日期查历史行情，按 biz_key 幂等 upsert，不删除任何
已采集原数据；北交所旧代码（43/83/87/88 开头）按 920 代码迁移规则映射后补值。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import storage  # noqa: E402
from backfill_trade_eastmoney import mcp_call  # noqa: E402
from processors.trade import recompute_trade  # noqa: E402

logger = logging.getLogger("backfill_snapshot")
SOURCE_MCP = "东方财富MCP·A股历史快照补数(北交所旧代码迁移)"
SOURCE_HIST = "东方财富历史行情API·A股快照补数(北交所旧代码迁移)"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_LOCAL = threading.local()


def to_mcp_code(db_code: str) -> str:
    """trade_snapshot_raw 代码 → 东方财富 MCP 代码。"""
    value = str(db_code or "").strip()
    if value[:2].lower() in ("sh", "sz", "bj"):
        market = value[:2].upper()
        number = value[2:]
    else:
        number = value
        market = "SH" if number.startswith(("5", "6", "9")) else "SZ"
    if market == "BJ" and not number.startswith("920"):
        # 北交所代码迁移：430017/830047/830779 → 920017/920047/920779
        number = "920" + number[-3:]
    return f"{number}.{market}"


def _number(value, field: str) -> float | None:
    text = str(value if value is not None else "").strip().replace(",", "")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(万亿|万股|亿股|万手|亿手|万元|亿元|万|亿|手|股|元|%)?", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or ""
    if field == "pct_change":
        return number
    if field == "last_price":
        return number
    if field in ("volume", "amount"):
        scales = {
            "volume": {"万亿": 1e12, "万股": 1e4, "亿股": 1e8, "万手": 1e6,
                       "亿手": 1e9, "手": 100, "股": 1, "万": 1e4, "亿": 1e8, "": 1},
            "amount": {"万亿": 1e12, "万元": 1e4, "亿元": 1e8,
                       "万": 1e4, "亿": 1e8, "元": 1, "": 1},
        }
        scaled = round(number * scales[field].get(unit, 1), 2)
        return int(scaled) if float(scaled).is_integer() else scaled
    return None


def parse_snapshot_response(data: dict, code_map: dict[str, list[dict]]) -> dict[str, dict]:
    """解析 MCP 批量返回；code_map: MCP代码 → 该代码对应的原始表行。"""
    out: dict[str, dict] = {}
    for sheet in data.get("data", []):
        columns = sheet.get("columns", [])
        rows = sheet.get("items", [])
        if not columns or not rows:
            continue
        col_codes: dict[int, str] = {}
        names: dict[str, str] = {}
        for idx, col in enumerate(columns):
            if idx == 0:
                continue
            label = str(col)
            match = re.search(r"(\d{6})\.(SH|SZ|BJ)", label, flags=re.I)
            if not match:
                continue
            code = f"{match.group(1)}.{match.group(2).upper()}"
            if code in code_map:
                col_codes[idx] = code
                names[code] = label.split("(")[0].strip()
        if not col_codes:
            continue
        for row in rows:
            if not isinstance(row, list) or not row:
                continue
            key = str(row[0])
            if "成交量" in key:
                field = "volume"
            elif "成交额" in key:
                field = "amount"
            elif "涨跌幅" in key:
                field = "pct_change"
            elif "收盘价" in key or "最新价" in key:
                field = "last_price"
            else:
                continue
            for idx, code in col_codes.items():
                if idx >= len(row):
                    continue
                value = _number(row[idx], field)
                if value is None:
                    continue
                out.setdefault(code, {})[field] = value
                out[code]["name"] = names.get(code, "")
    return out


def parse_kline_row(line: str, date: str) -> dict | None:
    """解析东方财富历史 K 线；成交量字段单位为手，落库统一为股。"""
    parts = str(line).split(",")
    if len(parts) < 9 or parts[0] != date:
        return None
    try:
        return {
            "last_price": float(parts[2]),
            "volume": int(round(float(parts[5]) * 100)),
            "amount": float(parts[6]),
            "pct_change": float(parts[8]),
        }
    except (TypeError, ValueError):
        return None


def fetch_history(mcp_code: str, date: str, retries: int = 3) -> dict | None:
    """按明确日期从东方财富历史行情 API 获取单股日线。"""
    symbol = mcp_code.split(".")[0]
    market = "1" if symbol.startswith("6") else "0"
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": "101", "fqt": "0", "secid": f"{market}.{symbol}",
        "beg": date.replace("-", ""), "end": date.replace("-", ""),
    }
    session = getattr(_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _LOCAL.session = session
    last_error = None
    for attempt in range(retries):
        try:
            resp = session.get(KLINE_URL, params=params, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            result = None
            for line in ((payload.get("data") or {}).get("klines") or []):
                value = parse_kline_row(line, date)
                if value:
                    result = value
                    break
            time.sleep(0.12)  # 东方财富按源 IP 限流，控制突发请求
            return result
        except Exception as exc:  # noqa: BLE001 - 单股失败由调用方记录为缺口径
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.4 * (attempt + 1))
    logger.debug("历史行情获取失败 %s %s: %s", mcp_code, date, last_error)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="A股历史快照补数（MCP / 东方财富历史行情 API）")
    parser.add_argument("--db", default=str(ROOT / "output" / "market_data.db"))
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--date", required=True, help="明确交易日 YYYY-MM-DD")
    parser.add_argument("--source", choices=("mcp", "history"), default="mcp")
    parser.add_argument("--only-incomplete", action="store_true",
                        help="只处理尚未由本工具修正的行")
    parser.add_argument("--batch-size", type=int, default=400)
    parser.add_argument("--workers", type=int, default=8, help="history 模式并发数")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个代码，用于小样本验证")
    parser.add_argument("--no-recompute", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    with storage.connect(args.db) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_snapshot_raw WHERE trade_date=? ORDER BY code",
            (args.date,))]
    if not rows:
        raise SystemExit(f"{args.date} 无快照行，请确认日期或先采集快照")
    if args.only_incomplete:
        before = len(rows)
        rows = [r for r in rows if not str(r.get("source") or "").startswith(
            ("东方财富MCP·A股历史快照补数", "东方财富历史行情API·A股快照补数"))]
        logger.info("仅处理未修正行：%d/%d", len(rows), before)

    code_map: dict[str, list[dict]] = {}
    for row in rows:
        code_map.setdefault(to_mcp_code(row["code"]), []).append(row)
    codes = list(code_map)
    if args.limit:
        codes = codes[:args.limit]
        code_map = {code: code_map[code] for code in codes}
    values: dict[str, dict] = {}
    if args.source == "mcp":
        batch_size = max(1, min(500, args.batch_size))
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            query = (f"{'、'.join(batch)}在{args.date}的收盘价、涨跌幅、成交量、成交额，"
                     "按股票和日期列出")
            logger.info("MCP 批量 %d/%d：%d 只代码", i // batch_size + 1,
                        (len(codes) + batch_size - 1) // batch_size, len(batch))
            data = mcp_call(query, tool="mx_ashare_finance_data", timeout=180)
            got = parse_snapshot_response(data, code_map)
            logger.info("返回 %d 只有效行情", len(got))
            values.update(got)
    else:
        workers = max(1, min(32, args.workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_history, code, args.date): code
                       for code in codes}
            done = 0
            for future in as_completed(futures):
                code = futures[future]
                done += 1
                try:
                    value = future.result()
                except Exception as exc:  # noqa: BLE001 - 单股失败计入缺失
                    logger.debug("历史行情异常 %s: %s", code, exc)
                    value = None
                if value:
                    values[code] = value
                if done % 200 == 0 or done == len(codes):
                    logger.info("历史行情进度 %d/%d，已获取 %d 只", done, len(codes), len(values))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_id = f"backfill_snapshot_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    source = SOURCE_MCP if args.source == "mcp" else SOURCE_HIST
    output_rows = []
    missing_codes = []
    for mcp_code, db_rows in code_map.items():
        value = values.get(mcp_code)
        if not value:
            missing_codes.extend(str(r["code"]) for r in db_rows)
            continue
        for db_row in db_rows:
            output_rows.append({
                "trade_date": args.date,
                "code": str(db_row["code"]),
                "name": str(db_row.get("name") or value.get("name") or ""),
                "pct_change": value.get("pct_change"),
                "volume": value.get("volume"),
                "amount": value.get("amount"),
                "last_price": value.get("last_price"),
                "source": source,
                "collected_at": now,
            })
    written = storage.upsert_trade_snapshot(args.db, pd.DataFrame(output_rows), run_id)

    calc = 0
    if not args.no_recompute:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
        calc = recompute_trade(args.db, config=config, full=True)

    report = {
        "date": args.date, "target_rows": len(rows), "mcp_codes": len(codes),
        "priced_codes": len(values), "written": written,
        "missing_codes": missing_codes, "recomputed_days": calc,
        "source": source, "run_id": run_id,
    }
    (ROOT / "output" / "trade_snapshot_backfill_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "missing_codes": len(missing_codes)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
