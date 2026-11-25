"""用乐咕乐股「大盘拥挤度」补齐成交集中度（前5%）历史。

数据源与优先级：
1. `https://legulegu.com/api/stockdata/ashares-congestion`（网页同源接口）：
   返回 2011 年至今的完整日序列；接口需要当天 MD5 token、页面 cookie 与 csrf。
   最近约 50 个交易日接口只返回日期占位、不返回数值。
2. `https://legulegu.com/stockdata/charts/180`（乐咕发布图表页）：
   表格公开可见最近约 100 个交易日，可补接口隐藏的近期段；直连偶发安全验证，
   脚本内置一份同页快照 `CHART_FALLBACK` 保证补数可重复执行。
3. `https://legulegu.com/stockdata/ashares-congestion` 页面表格：
   最新 30 个交易日含收盘价、前5%成交额、全部A股成交额、拥挤度。
4. 接口/页面均缺失的个别历史日（如 2022-07-06/07 等）：按相邻交易日线性插值，
   source 明确标注为插值，避免伪造原始来源。

落库表：`trade_congestion_raw`，一笔交易日一行，供计算层在快照缺失日兜底
`trade_observation.concentration5`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storage  # noqa: E402
from processors.trade import recompute_trade  # noqa: E402

logger = logging.getLogger("backfill_congestion")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
PAGE_URL = "https://legulegu.com/stockdata/ashares-congestion"
CHART_URL = "https://legulegu.com/stockdata/charts/180"
API_URL = "https://legulegu.com/api/stockdata/ashares-congestion"

SOURCE_API = "乐咕乐股·大盘拥挤度(前5%)"
SOURCE_CHART = "乐咕乐股·大盘拥挤度(发布图表历史补数)"
SOURCE_PAGE = "乐咕乐股·大盘拥挤度(最新数据表)"
SOURCE_INTERP = "乐咕乐股·大盘拥挤度(缺口线性插值)"

# 乐咕发布图表页 180 的同源快照：接口最近 50 日隐藏数值时的兜底。
# 数值为百分比，来自 2026-11-24 可见的 charts/180 表格（congestion 字段 ×100）。
CHART_FALLBACK: dict[str, float] = {
    "2026-09-08": 45.72, "2026-09-09": 44.76, "2026-09-10": 44.33,
    "2026-09-11": 45.89, "2026-09-14": 45.94, "2026-09-15": 46.93,
    "2026-09-16": 48.42, "2026-09-17": 49.02, "2026-09-18": 48.58,
    "2026-09-21": 44.68, "2026-09-22": 46.42, "2026-09-23": 43.28,
    "2026-09-24": 42.81, "2026-09-28": 43.92, "2026-09-29": 43.09,
    "2026-09-30": 42.96, "2026-10-08": 43.63, "2026-10-09": 42.62,
    "2026-10-12": 41.95, "2026-10-13": 42.76,
}


def _today_token() -> str:
    """与网页 JS 一致：当天日期 yyyy-MM-dd 的 MD5。"""
    return hashlib.md5(date.today().isoformat().encode("utf-8")).hexdigest()


def _fresh_page(url: str, tries: int = 8, timeout: int = 30):
    """遇到安全验证时换新 session 重试，返回 (session, response)。"""
    last = None
    for _ in range(tries):
        session = requests.Session()
        try:
            resp = session.get(url, headers={"User-Agent": UA}, timeout=timeout)
        except requests.RequestException as exc:  # pragma: no cover - 网络异常由调用方兜底
            last = exc
            continue
        if "human-challenge" not in resp.url:
            return session, resp
        last = None
    raise RuntimeError(f"无法获取页面 {url}（安全验证或网络异常）: {last}")


def fetch_api_items() -> tuple[dict, str]:
    """调用同源接口，返回 (items, 页面 HTML)。"""
    session, page = _fresh_page(PAGE_URL)
    soup = BeautifulSoup(page.text, "html.parser")
    csrf_tag = soup.find("meta", attrs={"name": "_csrf"})
    if not csrf_tag:
        raise RuntimeError("乐咕页面缺少 _csrf，接口调用被拒绝")
    headers = {
        "User-Agent": UA,
        "Referer": PAGE_URL,
        "Accept": "application/json, text/plain, */*",
        "X-CSRF-TOKEN": csrf_tag.get("content", ""),
    }
    resp = session.get(
        API_URL,
        params={"token": _today_token()},
        headers=headers,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise RuntimeError(f"乐咕接口返回异常: {resp.text[:200]}")
    return data["items"], page.text


def parse_api_items(items: list[dict], start: str, end: str) -> dict[str, dict]:
    """解析接口 items；congestion 为小数比例，统一转成百分数。"""
    out: dict[str, dict] = {}
    for item in items:
        day = str(item.get("date", ""))[:10]
        if not (start <= day <= end) or item.get("congestion") is None:
            continue
        ratio = float(item["congestion"])
        pct = item.get("congestionPercent")
        concentration = round(float(pct) if pct is not None else ratio * 100, 2)
        out[day] = {
            "trade_date": day,
            "close_price": item.get("close"),
            "top5_amount_yi": item.get("top5AmountYi"),
            "total_amount_yi": item.get("totalAmountYi"),
            "concentration5": concentration,
            "source": SOURCE_API,
        }
    return out


def parse_latest_table(html: str, start: str, end: str) -> dict[str, dict]:
    """解析大盘拥挤度页面的最新 30 日表格。"""
    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find(id="ashares-congestion-data")
    if not tbody:
        return {}
    out: dict[str, dict] = {}
    for tr in tbody.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 5 or not cells[0]:
            continue
        day = cells[0][:10]
        if not (start <= day <= end):
            continue

        def number(value: str) -> float | None:
            try:
                return float(value.replace(",", ""))
            except ValueError:
                return None

        concentration = number(cells[4])
        if concentration is None:
            continue
        out[day] = {
            "trade_date": day,
            "close_price": number(cells[1]),
            "top5_amount_yi": number(cells[2]),
            "total_amount_yi": number(cells[3]),
            "concentration5": round(concentration, 2),
            "source": SOURCE_PAGE,
        }
    return out


def parse_chart_table(html: str, start: str, end: str) -> dict[str, dict]:
    """解析发布图表页表格；第一数值列为小数比例，需 ×100。"""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [th.get_text(" ", strip=True) for th in rows[0].find_all(["th", "td"])]
        if not header or "时间" not in header[0]:
            continue
        for tr in rows[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) < 2:
                continue
            day = cells[0][:10]
            if not (start <= day <= end):
                continue
            try:
                concentration = round(float(cells[1]) * 100, 2)
            except ValueError:
                continue
            out[day] = {"trade_date": day, "concentration5": concentration,
                        "source": SOURCE_CHART}
    return out


def fetch_chart_history(start: str, end: str) -> dict[str, dict]:
    """抓取发布图表页；直连失败时用同源快照兜底。"""
    try:
        _, resp = _fresh_page(CHART_URL, tries=5, timeout=25)
        got = parse_chart_table(resp.text, start, end)
        if got:
            logger.info("发布图表页解析 %d 个交易日", len(got))
            return got
    except RuntimeError as exc:
        logger.warning("发布图表页直连失败，使用同源快照：%s", exc)
    return {
        day: {"trade_date": day, "concentration5": value, "source": SOURCE_CHART}
        for day, value in CHART_FALLBACK.items()
        if start <= day <= end
    }


def interpolate_gaps(days: list[str], values: dict[str, dict], enabled: bool) -> dict[str, dict]:
    """接口/页面缺失的孤立交易日按相邻交易日线性插值，并标注来源。"""
    if not enabled or not days:
        return {}
    series = pd.Series([values.get(d, {}).get("concentration5") for d in days], dtype=float)
    filled = series.interpolate(method="linear", limit_area="inside")
    result: dict[str, dict] = {}
    for day, value in zip(days, filled):
        if day in values or pd.isna(value):
            continue
        result[day] = {
            "trade_date": day,
            "concentration5": round(float(value), 2),
            "source": SOURCE_INTERP,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="乐咕大盘拥挤度历史补数")
    parser.add_argument("--db", default=str(ROOT / "output" / "market_data.db"))
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default=None, help="默认库中最后交易日")
    parser.add_argument("--no-recompute", action="store_true")
    parser.add_argument("--no-interpolate", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    trade_days = [d for d in storage.trade_raw_dates(args.db, "exchange")
                  if d >= args.start and (not args.end or d <= args.end)]
    if not trade_days:
        raise SystemExit("没有可补的交易日，请检查 --start/--end 或交易原始表")
    end = args.end or trade_days[-1]

    items, html = fetch_api_items()
    api = parse_api_items(items, args.start, end)
    chart = fetch_chart_history(args.start, end)
    latest = parse_latest_table(html, args.start, end)
    logger.info("接口 %d 日 / 图表 %d 日 / 最新表 %d 日", len(api), len(chart), len(latest))

    values: dict[str, dict] = {}
    values.update(api)          # 官方同源接口优先
    values.update(chart)        # 接口最近段隐藏值，由图表页补
    values.update(latest)       # 最新 30 日取页面原始四列
    gaps = interpolate_gaps(trade_days, values, not args.no_interpolate)
    values.update(gaps)

    missing = [d for d in trade_days if d not in values]
    rows = [values[d] for d in trade_days if d in values]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for row in rows:
        row["collected_at"] = now

    run_id = f"backfill_congestion_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    written = storage.upsert_trade_congestion(
        args.db, pd.DataFrame(rows, columns=[
            "trade_date", "close_price", "top5_amount_yi", "total_amount_yi",
            "concentration5", "source", "collected_at",
        ]), run_id,
    )

    calc = 0
    if not args.no_recompute:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
        calc = recompute_trade(args.db, config=config, full=True)

    source_counts: dict[str, int] = {}
    for row in rows:
        source_counts[row["source"]] = source_counts.get(row["source"], 0) + 1
    report = {
        "written": written,
        "trade_days": len(trade_days),
        "missing": missing,
        "source_counts": source_counts,
        "interpolated_days": sorted(gaps),
        "range": [args.start, end],
        "recomputed_days": calc,
        "run_id": run_id,
    }
    (ROOT / "output" / "trade_congestion_backfill_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
