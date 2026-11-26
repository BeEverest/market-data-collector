"""交易历史补数：按交易日补齐 2020-01-01 至今的交易所概况与两融原始数据。

设计要点：
- 只补缺口：已落库且字段有效的日期/字段不重复请求；幂等 upsert，保留已有有效值。
- 交易所数据直接解析官方接口原始字段（沪：亿元/亿股；深：元），不做单位换算，
  单位换算与 11 项指标计算仍由 processors/trade.py 负责。
- 上交所官方查询自 2021-12-27 起才有历史存档；此前日期沪市字段留空并在报告中列出，
  避免用口径不一致的数据冒充交易所原始值。
- 全A个股快照接口仅返回当日，无法回溯历史，本脚本不处理（历史快照只能逐日积累）。

用法：
    python tools/backfill_trade.py --start 2020-01-01 --end 2026-11-24
    python tools/backfill_trade.py --start 2020-01-01 --force-exchange --no-recompute
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storage  # noqa: E402
from collectors.trade_calendar import last_completed_trade_day  # noqa: E402
from processors.trade import recompute_trade  # noqa: E402

logger = logging.getLogger("backfill_trade")

SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_HEADERS = {
    "Referer": "https://www.sse.com.cn/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
SOURCE = "交易所每日概况（历史补数）"
# 上交所官方每日概况接口仅存 2021-12-27 起的历史；此前日期不再逐日空请求
SSE_MIN_DATE = "2021-12-27"

EX_COLS = ["sse_amount", "sse_volume", "sse_float_mv", "sse_total_mv",
           "szse_amount", "szse_float_mv", "szse_total_mv"]


def _num(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _trade_days(start: str, end: str) -> list[str]:
    import akshare as ak
    cal = ak.tool_trade_date_hist_sina()
    days = pd.to_datetime(cal["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return sorted(d for d in days.tolist() if isinstance(d, str) and start <= d <= end)


def fetch_sse(day: str, retries: int = 3) -> dict:
    """上交所每日概况：直接解析官方 JSON，兼容 3/5 条产品结构。"""
    ymd = day.replace("-", "")
    params = {
        "sqlId": "COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C",
        "PRODUCT_CODE": "01,02,03,11,17",
        "type": "inParams",
        "SEARCH_DATE": day,
    }
    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.get(SSE_URL, params=params, headers=SSE_HEADERS, timeout=20)
            r.raise_for_status()
            result = r.json().get("result") or []
            if not result:
                return {}
            by_code = {str(x.get("PRODUCT_CODE", "")).strip(): x for x in result}
            # 新结构含 17=股票合计；旧结构（2021-12-27 前）只有 01/02/03/11 分项
            total = by_code.get("17") or {}
            if not total:
                merged = {}
                codes = [c for c in ("01", "02", "03", "11") if c in by_code]
                if not codes:
                    return {}
                for field in ("TRADE_AMT", "TRADE_VOL", "NEGO_VALUE", "TOTAL_VALUE"):
                    vals = [_num(by_code[c].get(field)) for c in codes]
                    if all(v is not None for v in vals):
                        merged[field] = sum(vals)
                total = merged
            return {
                "sse_amount": _num(total.get("TRADE_AMT")),
                "sse_volume": _num(total.get("TRADE_VOL")),
                "sse_float_mv": _num(total.get("NEGO_VALUE")),
                "sse_total_mv": _num(total.get("TOTAL_VALUE")),
            }
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"上交所 {ymd} 请求失败：{last_exc}")


def fetch_szse(day: str, retries: int = 3) -> dict:
    """深交所证券类别统计（股票行）：原始单位为元。"""
    import akshare as ak
    last_exc = None
    for attempt in range(retries):
        try:
            df = ak.stock_szse_summary(date=day.replace("-", ""))
            row = df[df["证券类别"] == "股票"]
            if row.empty:
                return {}
            z = row.iloc[0]
            return {
                "szse_amount": _num(z.get("成交金额")),
                "szse_float_mv": _num(z.get("流通市值")),
                "szse_total_mv": _num(z.get("总市值")),
            }
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"深交所 {day} 请求失败：{last_exc}")


def _existing_exchange(db: str) -> dict[str, dict]:
    df = storage.read_trade_raw(db, "exchange")
    if df.empty:
        return {}
    return {str(r["trade_date"])[:10]: r.to_dict() for _, r in df.iterrows()}


def _merge_row(day: str, fresh: dict, old: dict | None) -> dict | None:
    """已有有效字段优先保留，避免补数请求失败时覆盖旧值。"""
    row = {"trade_date": day, "source": SOURCE,
           "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    for col in EX_COLS:
        v = _num(fresh.get(col))
        if v is None and old is not None:
            v = _num(old.get(col))
        row[col] = v
    if all(row.get(c) is None for c in EX_COLS):
        return None
    return row


def backfill_exchange(db: str, days: list[str], existing: dict[str, dict],
                      *, workers: int, force: bool) -> dict:
    """并发补交易所概况，返回统计。"""
    tasks = {}  # day -> (need_sse, need_szse)
    for day in days:
        old = existing.get(day)
        if force or old is None:
            tasks[day] = (day >= SSE_MIN_DATE, True)
        else:
            need_sse = day >= SSE_MIN_DATE and any(_num(old.get(c)) is None for c in EX_COLS[:4])
            need_szse = any(_num(old.get(c)) is None for c in EX_COLS[4:])
            if need_sse or need_szse:
                tasks[day] = (need_sse, need_szse)

    stats = {"days": len(days), "requested": len(tasks), "sse_ok": 0, "szse_ok": 0,
             "errors": [], "written": 0}
    if not tasks:
        return stats

    rows: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for day, (need_sse, need_szse) in tasks.items():
            futs = []
            if need_sse:
                futs.append(("sse", pool.submit(fetch_sse, day)))
            if need_szse:
                futs.append(("szse", pool.submit(fetch_szse, day)))
            futures[day] = (futs, need_sse, need_szse)

        for day, (futs, need_sse, need_szse) in futures.items():
            fresh: dict = {}
            for src, fut in futs:
                try:
                    data = fut.result()
                    if data:
                        fresh.update(data)
                        stats[f"{src}_ok"] += 1
                except Exception as exc:  # noqa: BLE001
                    stats["errors"].append(f"{day} {src}: {exc}")
            merged = _merge_row(day, fresh, None if force else existing.get(day))
            if merged is not None:
                rows[day] = merged

    run_id = f"backfill_exchange_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    for i in range(0, len(rows), 200):
        chunk = list(rows.values())[i:i + 200]
        stats["written"] += storage.upsert_trade_exchange(db, pd.DataFrame(chunk), run_id)
    return stats


def backfill_margin(db: str, start: str, end: str) -> dict:
    """两融接口一次返回全历史，仅补库里缺失的交易日。"""
    import akshare as ak
    stats = {"requested": 1, "written": 0, "errors": []}
    try:
        raw = ak.stock_margin_account_info().copy()
    except Exception as exc:  # noqa: BLE001
        stats["errors"].append(str(exc))
        return stats
    raw["日期"] = pd.to_datetime(raw["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    raw = raw.dropna(subset=["日期"])
    have = set(storage.trade_raw_dates(db, "margin"))
    raw = raw[(raw["日期"] >= start) & (raw["日期"] <= end) & (~raw["日期"].isin(have))]
    if raw.empty:
        return stats
    rename = {
        "日期": "trade_date", "融资余额": "financing_balance", "融券余额": "securities_balance",
        "融资买入额": "financing_buy", "融券卖出额": "securities_sell",
        "平均维持担保比例": "margin_ratio", "有融资融券负债的投资者数量": "debt_investors",
        "担保物总价值": "collateral_value", "参与交易的投资者数量": "active_investors",
        "个人投资者数量": "individual_investors", "机构投资者数量": "institutional_investors",
        "证券公司数量": "broker_count", "营业部数量": "branch_count",
    }
    out = raw.rename(columns=rename)
    out = out[[c for c in rename.values() if c in out.columns]].copy()
    out["source"] = "中国证券金融两融账户（akshare 历史补数）"
    out["collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_id = f"backfill_margin_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    stats["written"] = storage.upsert_trade_margin(db, out, run_id)
    return stats


def coverage(db: str, start: str, end: str, days: list[str]) -> dict:
    ex = storage.read_trade_raw(db, "exchange", start, end)
    mg = storage.read_trade_raw(db, "margin", start, end)
    obs = storage.read_trade(db, start, end)
    ex_sse = {str(d)[:10] for d in ex.loc[ex["sse_amount"].notna(), "trade_date"]}
    ex_szse = {str(d)[:10] for d in ex.loc[ex["szse_amount"].notna(), "trade_date"]}
    mg_days = {str(d)[:10] for d in mg["trade_date"]}
    both = ex_sse & ex_szse
    return {
        "trade_days": len(days),
        "exchange_rows": len(ex),
        "sse_days": len(ex_sse),
        "szse_days": len(ex_szse),
        "both_days": len(both),
        "margin_days": len(mg_days),
        "observation_days": len(obs),
        "sse_missing": sorted(set(days) - ex_sse)[:20],
        "both_missing": sorted(set(days) - both)[:20],
        "sse_missing_count": len(set(days) - ex_sse),
        "both_missing_count": len(set(days) - both),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="交易历史补数（2020 至今）")
    parser.add_argument("--db", default=str(ROOT / "output" / "market_data.db"))
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None, help="默认最近已结束交易日")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--force-exchange", action="store_true", help="强制重拉交易所区间")
    parser.add_argument("--no-recompute", action="store_true", help="只补原始数据，不重算指标")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    end = args.end or last_completed_trade_day()
    days = _trade_days(args.start, end)
    logger.info("补数区间 %s ~ %s，交易日 %d 天", args.start, end, len(days))
    if not days:
        return 1

    existing = _existing_exchange(args.db)
    ex_stats = backfill_exchange(args.db, days, existing,
                                 workers=args.workers, force=args.force_exchange)
    logger.info("交易所补数完成：%s", json.dumps(ex_stats, ensure_ascii=False)[:800])
    mg_stats = backfill_margin(args.db, args.start, end)
    logger.info("两融补数完成：%s", mg_stats)

    calc_days = 0
    if not args.no_recompute:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
        calc_days = recompute_trade(args.db, config=config, full=True)

    cov = coverage(args.db, args.start, end, days)
    report = {
        "range": [args.start, end],
        "exchange": {k: v for k, v in ex_stats.items() if k != "errors"},
        "exchange_errors": ex_stats["errors"][:20],
        "margin": mg_stats,
        "recomputed_days": calc_days,
        "coverage": cov,
    }
    report_path = ROOT / "output" / "trade_backfill_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("补数报告：%s", report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
