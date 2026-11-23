"""
社会融资规模存量抓取（中国人民银行官方口径）。

数据源
------
央行 调查统计司 > 统计数据 > <年份>年统计数据 > 社会融资规模 > 社会融资规模存量统计表。
列表页示例：https://www.pbc.gov.cn/diaochatongjisi/116219/116319/index.html
存量统计表 htm 示例：.../attachDir/2026/11/2026111317240340459.htm

背景
----
SFR_STOCK 原先采用「央行公布值锚点 + 社融增量插值」估算，该插值逻辑已删除。
现仅使用央行存量统计表公布的逐月「社会融资规模存量」（万亿）与「增速(%)」，
增速字段即官方同比，不派生、不估算。

本模块：
  - discover_latest_url()：从统计数据列表页发现最新年度的社会融资规模存量统计表 URL
  - parse_sfr_stock(html)：纯函数，从表 HTML 解析 {YYYY-MM: (存量万亿, 增速%)}
  - fetch_sfr_stock()：组合上面两步，带进程内缓存

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
_CACHE_TTL_SEC = 3600


def _absolute(url: str, href: str) -> str:
    return urljoin(url, href)


def discover_latest_url(timeout: float = 20.0) -> str | None:
    """从统计数据列表页发现最新年度「社会融资规模」栏目，再定位其中的存量统计表 htm。

    两步：
      1) 列表页找所有"社会融资规模"栏目链接，取年份最大的；
      2) 栏目页找所有 htm 链接，逐个抓标题含"社会融资规模存量统计表"者，取最新（URL 中日期最大）。
    """
    from bs4 import BeautifulSoup

    r = requests.get(LIST_URL, timeout=timeout, headers=UA)
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")

    # 1) 社会融资规模栏目链接（URL 形如 /.../<年份标识>/.../shr.../index.html）
    col_candidates = []
    for a in soup.find_all("a", href=True):
        if "社会融资规模" in a.get_text(" ", strip=True) and "存量" not in a.get_text(" ", strip=True):
            href = a["href"]
            if "index.html" in href:
                col_candidates.append(href)
    if not col_candidates:
        return None
    # URL 里含年份路径，如 2026ntjsj / 5570903；按出现顺序，列表页通常最新在前
    col_url = _absolute(LIST_URL, col_candidates[0])

    # 2) 栏目页抓 htm 附件，找标题为"社会融资规模存量统计表"的
    r2 = requests.get(col_url, timeout=timeout, headers=UA)
    r2.encoding = r2.apparent_encoding or "utf-8"
    soup2 = BeautifulSoup(r2.text, "html.parser")
    htm_links = []
    for a in soup2.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".htm"):
            htm_links.append(_absolute(col_url, href))
    # 去重保序
    seen = set()
    htm_links = [u for u in htm_links if not (u in seen or seen.add(u))]

    best_url, best_key = None, ""
    for u in htm_links:
        try:
            rr = requests.get(u, timeout=timeout, headers=UA)
            rr.encoding = rr.apparent_encoding or "utf-8"
            title = BeautifulSoup(rr.text, "html.parser").get_text(" ", strip=True)[:60]
            if "社会融资规模存量统计表" not in title:
                continue
            key = re.sub(r"\D", "", u)  # URL 里的数字串，含日期
            if key > best_key:
                best_key, best_url = key, u
        except Exception:
            continue
    return best_url


def parse_sfr_stock(html: str) -> dict[str, tuple[float, float]]:
    """从存量统计表 HTML 解析 {YYYY-MM: (存量万亿, 增速%)}。纯函数，便于单测。

    表结构：第4行为月份(YYYY.M)，第5行为"存量/增速"两层表头，
    "社会融资规模存量 AFRE(stock)"行每月两列：存量、增速。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return {}
    rows = table.find_all("tr")
    if len(rows) < 8:
        return {}

    def cells(tr):
        return [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]

    # 找月份行：含形如 2026.1 的单元格
    month_row_idx = None
    months: list[str] = []
    for i, tr in enumerate(rows[:8]):
        cs = cells(tr)
        found = []
        for c in cs:
            m = re.fullmatch(r"(20\d{2})\.(\d{1,2})", c.strip())
            if m:
                found.append(f"{m.group(1)}-{int(m.group(2)):02d}")
        if len(found) >= 6:
            month_row_idx, months = i, found
            break
    if month_row_idx is None:
        return {}

    # 找存量数据行
    stock_cells = None
    for tr in rows:
        cs = cells(tr)
        if cs and "社会融资规模存量" in cs[0] and "AFRE" in cs[0]:
            stock_cells = cs
            break
    if stock_cells is None:
        for tr in rows:
            cs = cells(tr)
            if cs and "社会融资规模存量" in cs[0]:
                stock_cells = cs
                break
    if stock_cells is None:
        return {}

    out: dict[str, tuple[float, float]] = {}
    for i, mon in enumerate(months):
        idx = 1 + i * 2  # 第0列是项目名，每月占2列
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


def fetch_sfr_stock(timeout: float = 20.0, url: str | None = None) -> tuple[dict[str, tuple[float, float]], str | None]:
    """抓取官方存量/增速。返回 ({YYYY-MM: (存量万亿, 增速%)}, 来源URL)。"""
    target = url or discover_latest_url(timeout)
    if not target:
        raise RuntimeError("未能发现央行社会融资规模存量统计表 URL")
    now = datetime.now()
    cached = _CACHE.get(target)
    if cached and (now - cached[0]).total_seconds() < _CACHE_TTL_SEC:
        return cached[1], target
    r = requests.get(target, timeout=timeout, headers=UA)
    r.encoding = r.apparent_encoding or "utf-8"
    data = parse_sfr_stock(r.text)
    if not data:
        raise ValueError(f"存量统计表解析为空：{target}")
    _CACHE[target] = (now, data)
    return data, target
