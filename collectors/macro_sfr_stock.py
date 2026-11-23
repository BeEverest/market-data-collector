"""
社会融资规模存量抓取（中国人民银行官方口径）。

数据源
------
央行 调查统计司 > 统计数据 > <年份>年统计数据 > 社会融资规模 > 社会融资规模存量统计表。
列表页：https://www.pbc.gov.cn/diaochatongjisi/116219/116319/index.html
每年一张存量表（2016~2026 已验证），因此必须按请求区间抓取对应年份的表，
不能只取最新一年，否则跨年精确采集会缺失历史月份。

背景
----
SFR_STOCK 原先采用「央行公布值锚点 + 社融增量插值」估算，该插值逻辑已删除。
现仅使用央行存量统计表公布的逐月/逐季末「社会融资规模存量」（万亿）与「增速(%)」，
增速字段即官方同比，不派生、不估算。

本模块：
  - discover_stock_tables()：发现列表页中各年度的存量统计表 URL
  - parse_sfr_stock(html)：纯函数，从表 HTML 解析 {YYYY-MM: (存量万亿, 增速%)}
  - fetch_sfr_stock()：按 start_month/end_month 抓取对应年份并合并，带进程内缓存

所有网络/解析异常向上抛，由调用方记录失败；不再回退到任何插值口径。
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin

import requests

LIST_URL = "https://www.pbc.gov.cn/diaochatongjisi/116219/116319/index.html"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

_CACHE: dict[str, tuple[datetime, dict[str, tuple[float, float]]]] = {}
_TABLES_CACHE: dict[str, tuple[datetime, dict[str, str]]] = {}
_CACHE_TTL_SEC = 3600


def _absolute(url: str, href: str) -> str:
    return urljoin(url, href)


def _get_text(url: str, timeout: float) -> str:
    r = requests.get(url, timeout=timeout, headers=UA)
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def parse_sfr_stock(html: str) -> dict[str, tuple[float, float]]:
    """从存量统计表 HTML 解析 {YYYY-MM: (存量万亿, 增速%)}。纯函数，便于单测。

    兼容两种官方表头：
      - 月度：2026.1 / 2026.2 ...
      - 季末（早年如 2015）：2015.Q1 / 2015.Q2 ...，映射为 03/06/09/12 月
    数据行首列可能是「社会融资规模存量 AFRE(stock)」，也可能只有中文「社会融资规模存量」；
    标题行「社会融资规模存量统计表」必须排除，否则老表会解析为空。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return {}
    rows = table.find_all("tr")
    if len(rows) < 5:
        return {}

    def cells(tr):
        return [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]

    # 找月份/季度行：形如 2026.1 或 2015.Q1
    months: list[str] = []
    for tr in rows[:10]:
        cs = cells(tr)
        found: list[str] = []
        for c in cs:
            c = c.strip()
            m = re.fullmatch(r"(20\d{2})\.(\d{1,2})", c)
            if m:
                found.append(f"{m.group(1)}-{int(m.group(2)):02d}")
                continue
            m = re.fullmatch(r"(20\d{2})\.Q([1-4])", c, re.IGNORECASE)
            if m:
                found.append(f"{m.group(1)}-{int(m.group(2)) * 3:02d}")
        if len(found) >= 3:
            months = found
            break
    if not months:
        return {}

    # 找存量数据行；排除标题行「...统计表」
    stock_cells = None
    for tr in rows:
        cs = cells(tr)
        if not cs:
            continue
        head = cs[0]
        if "社会融资规模存量" in head and "统计表" not in head and len(cs) > 3:
            stock_cells = cs
            break
    if stock_cells is None:
        return {}

    out: dict[str, tuple[float, float]] = {}
    for i, mon in enumerate(months):
        idx = 1 + i * 2  # 第0列是项目名，每月/季占2列：存量、增速
        if idx + 1 >= len(stock_cells):
            break
        sv, gv = stock_cells[idx].strip(), stock_cells[idx + 1].strip()
        if not sv or not gv:
            continue
        try:
            stock = float(sv)
            growth = float(gv)
        except ValueError:
            continue
        out[mon] = (stock, growth)
    return out


def _fetch_table(url: str, timeout: float) -> dict[str, tuple[float, float]]:
    """抓单张存量表，带进程内缓存。"""
    now = datetime.now()
    cached = _CACHE.get(url)
    if cached and (now - cached[0]).total_seconds() < _CACHE_TTL_SEC:
        return cached[1]
    data = parse_sfr_stock(_get_text(url, timeout))
    if not data:
        raise ValueError(f"存量统计表解析为空：{url}")
    _CACHE[url] = (now, data)
    return data


def discover_stock_tables(
    timeout: float = 20.0,
    start_month: str | None = None,
    end_month: str | None = None,
) -> dict[str, str]:
    """发现列表页各年度的「社会融资规模存量统计表」，返回 {年份: URL}。

    start_month / end_month 为 YYYY-MM，用于过滤年份，避免无谓请求早年表。
    年份优先取解析出的表内月份；解析失败时退化为栏目顺序推断（列表按年份倒序）。
    """
    from bs4 import BeautifulSoup

    now = datetime.now()
    cached = _TABLES_CACHE.get(LIST_URL)
    if cached and (now - cached[0]).total_seconds() < _CACHE_TTL_SEC:
        tables = dict(cached[1])
    else:
        soup = BeautifulSoup(_get_text(LIST_URL, timeout), "html.parser")
        cols: list[str] = []
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            if "社会融资规模" in text and "存量" not in text and "index.html" in a["href"]:
                url = _absolute(LIST_URL, a["href"])
                if url not in cols:
                    cols.append(url)

        tables = {}
        for idx, col_url in enumerate(cols):
            m = re.search(r"(20\d{2})ntjsj", col_url)
            guessed_year = int(m.group(1)) if m else now.year - idx
            try:
                col_soup = BeautifulSoup(_get_text(col_url, timeout), "html.parser")
            except Exception:
                continue
            htm_links: list[str] = []
            for a in col_soup.find_all("a", href=True):
                if a["href"].lower().endswith(".htm"):
                    url = _absolute(col_url, a["href"])
                    if url not in htm_links:
                        htm_links.append(url)
            for htm_url in htm_links:
                try:
                    html = _get_text(htm_url, timeout)
                except Exception:
                    continue
                title = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:100]
                if "社会融资规模存量统计表" not in title:
                    continue
                data = parse_sfr_stock(html)
                _CACHE[htm_url] = (now, data)
                years = {k[:4] for k in data} or {str(guessed_year)}
                for y in years:
                    tables[y] = htm_url
                break
        _TABLES_CACHE[LIST_URL] = (now, dict(tables))

    if start_month or end_month:
        sy = int(start_month[:4]) if start_month else 0
        ey = int(end_month[:4]) if end_month else 9999
        tables = {y: u for y, u in tables.items() if sy <= int(y) <= ey}
    return tables


def discover_latest_url(timeout: float = 20.0) -> str | None:
    """兼容旧调用：返回最新年度存量表 URL。"""
    tables = discover_stock_tables(timeout=timeout)
    if not tables:
        return None
    return tables[max(tables)]


def fetch_sfr_stock(
    timeout: float = 20.0,
    url: str | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
) -> tuple[dict[str, tuple[float, float]], str]:
    """抓取官方存量/增速，返回 ({YYYY-MM: (存量万亿, 增速%)}, 来源说明)。

    传 url 时只抓该张表（兼容显式指定）；否则按 start_month/end_month 覆盖的年份
    抓取对应年度表并合并，较新年份的表在月份重复时优先。
    """
    if url:
        return _fetch_table(url, timeout), url

    tables = discover_stock_tables(
        timeout=timeout, start_month=start_month, end_month=end_month,
    )
    if not tables:
        raise RuntimeError(
            f"未能发现央行社会融资规模存量统计表（区间 {start_month or '起'} ~ {end_month or '今'}）"
        )
    merged: dict[str, tuple[float, float]] = {}
    for year in sorted(tables):
        merged.update(_fetch_table(tables[year], timeout))
    if not merged:
        raise ValueError("央行存量统计表全部解析为空")
    years = sorted({k[:4] for k in merged})
    note = "央行存量统计表（" + "、".join(years) + "）"
    return merged, note
