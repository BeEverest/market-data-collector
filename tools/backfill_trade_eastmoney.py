"""用东方财富 MCP（mx-ds-mcp）补齐 2020-2021 沪市成交额历史缺口。

背景：上交所官方每日概况接口只存 2021-12-27 起的历史；本脚本通过 MCP
`mx_index_block_finance_data` 查上证指数(000001.SH)日成交额（亿元）。实测与交易所
股票成交金额重叠期偏差 <0.4%，仅用于**补全沪市缺失字段**，不覆盖已有官方值。
source 字段会标注来源，便于复核。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storage  # noqa: E402
from processors.trade import recompute_trade  # noqa: E402

logger = logging.getLogger("backfill_em")
# 新环境 / Docker 部署时可用 MCP_URL 环境变量指向本机可达的 MCP 网关；
# 未设置时沿用内网默认地址（仅开发沙箱可解析）。
MCP_URL = os.environ.get(
    "MCP_URL", "http://mx-ds-mcp.agent-gateway.auth-proxy.local/mxds/v2/mcp"
)
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
SOURCE_ADD = "东方财富MCP·上证指数成交额(历史补数)"


def _check_mcp_payload(payload: dict) -> dict:
    """识别 MCP 业务错误（如积分耗尽）：有 message 无 data 时必须抛错，不能当空数据。"""
    if isinstance(payload, dict) and "data" not in payload and payload.get("message"):
        raise RuntimeError(f"东方财富MCP业务错误：{payload['message']}")
    return payload


def mcp_call(query: str, tool: str = "mx_index_block_finance_data", timeout: int = 120) -> dict:
    s = requests.Session()
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "trade-backfill", "version": "1.0"}}}
    r = s.post(MCP_URL, headers=HEADERS, json=init, timeout=30)
    r.raise_for_status()
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": {"query": query}}}
    r = s.post(MCP_URL, headers=HEADERS, json=call, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    _check_mcp_payload(payload)
    content = payload.get("result", {}).get("content", [])
    text = content[0].get("text", "") if content else ""
    if not text:
        raise RuntimeError("MCP 返回空 content")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MCP 返回非 JSON 内容：{text[:200]}") from exc
    # 业务错误也可能藏在 content.text 内层，必须同样识别
    _check_mcp_payload(result)
    return result


def parse_amount_safe(data: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for sheet in data.get("data", []):
        cols = sheet.get("columns", [])
        rows = sheet.get("items", [])
        if not cols or not rows or "成交额" not in str(rows[0][0]):
            continue
        for idx, value in enumerate(rows[0][1:], start=1):
            # MCP 大额返回形如 "1.065万亿"，小额形如 "9747亿"
            m = re.search(r"(-?[\d,]+(?:\.\d+)?)\s*(万亿|亿)", str(value))
            if not m or idx >= len(cols):
                continue
            date = str(cols[idx]).split("(")[0][:10]
            factor = 10000 if m.group(2) == "万亿" else 1
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                out[date] = float(m.group(1).replace(",", "")) * factor
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="东方财富 MCP 补沪市历史成交额")
    parser.add_argument("--db", default=str(ROOT / "output" / "market_data.db"))
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2021-12-26")
    parser.add_argument("--no-recompute", action="store_true")
    parser.add_argument("--fill-missing", action="store_true",
                        help="只补 sse_amount 为空的交易日（按明确日期列表查询，修年度查询漏日）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    amounts: dict[str, float] = {}
    if args.fill_missing:
        ex0 = storage.read_trade_raw(args.db, "exchange")
        missing = sorted(
            str(d)[:10] for _, r in ex0.iterrows()
            for d in [r["trade_date"]]
            if args.start <= str(d)[:10] <= args.end and pd.isna(r.get("sse_amount"))
        )
        logger.info("按明确日期补缺：%d 个交易日", len(missing))
        for i in range(0, len(missing), 10):
            batch = missing[i:i + 10]
            query = (f"上证指数(000001.SH)在{'、'.join(batch)}这些交易日的"
                     "成交金额（亿元），按日期列出")
            got = parse_amount_safe(mcp_call(query))
            logger.info("批次 %d/%d 返回 %d 日", i // 10 + 1, (len(missing) + 9) // 10, len(got))
            amounts.update(got)
    else:
        for year in range(int(args.start[:4]), int(args.end[:4]) + 1):
            lo = max(args.start, f"{year}-01-01")
            hi = min(args.end, f"{year}-12-31")
            query = (f"上证指数(000001.SH)在{lo}至{hi}每个交易日的成交金额（亿元），"
                     "请完整列出所有交易日")
            logger.info("MCP 查询 %s ~ %s 上证指数日成交额", lo, hi)
            raw = mcp_call(query)
            got = parse_amount_safe(raw)
            logger.info("MCP 返回 %d 个交易日", len(got))
            amounts.update(got)

    ex = storage.read_trade_raw(args.db, "exchange")
    if ex.empty:
        raise SystemExit("trade_exchange_raw 为空，请先运行 tools/backfill_trade.py")
    filled = 0
    rows = []
    today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for _, r in ex.iterrows():
        day = str(r["trade_date"])[:10]
        if args.start <= day <= args.end and pd.isna(r.get("sse_amount")) and day in amounts:
            r = r.copy()
            r["sse_amount"] = amounts[day]
            src = str(r.get("source", "") or "")
            r["source"] = f"{src}+{SOURCE_ADD}" if src else SOURCE_ADD
            r["collected_at"] = today
            filled += 1
        rows.append(r)
    run_id = f"backfill_em_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    storage.upsert_trade_exchange(args.db, pd.DataFrame(rows), run_id)
    logger.info("东方财富 MCP 补齐沪市成交额 %d 个交易日", filled)

    calc = 0
    if not args.no_recompute:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
        calc = recompute_trade(args.db, config=config, full=True)
    report = {"filled_days": filled, "mcp_days": len(amounts), "recomputed_days": calc,
              "source": SOURCE_ADD}
    (ROOT / "output" / "trade_eastmoney_backfill_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
