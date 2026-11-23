"""
社会消费品零售总额「季调后环比」抓取（国家统计局官方口径）。

背景
----
akshare 的 macro_china_consumer_goods_retail 返回的「环比增长」是
**未季调名义环比** = 当月值/上月值 - 1，受春节/黄金周影响在 ±10% 波动，
与统计局官方公布的季调环比（通常 ±1% 以内）口径完全不同，会严重误导。

国家统计局每月社零新闻稿的「附注 → 5. 环比数据修订」给出官方季调环比，
并且每次会按季节调整模型回溯修订最近约 13 个月，因此必须以最新一期为准。

本模块：
  - discover_latest_url()：从「最新发布」列表页自动发现最新社零新闻稿 URL
  - parse_sa_mom(html)：从新闻稿文本解析出 {YYYY-MM: 季调环比%}
  - fetch_sa_mom()：组合上面两步，带进程内缓存（同一 URL 只抓一次）
  - apply_sa_mom(df)：把季调环比覆盖到 DataFrame 的 RETAIL_SALES 行

所有网络/解析异常都向上抛，由调用方决定降级策略，不在这里吞掉。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import pandas as pd
import requests

logger = logging.getLogger(__name__)

LIST_URL = "https://www.stats.gov.cn/sj/zxfb/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 进程内缓存：{url: (抓取时间, {月份: 环比})}
_CACHE: dict[str, tuple[datetime, dict[str, float]]] = {}
_CACHE_TTL_SEC = 3600

_REL_TITLE = "社会消费品零售总额"
_REL_TITLE_EXCLUDE = "解读"  # 跳过解读稿，只要数据稿


def discover_latest_url(timeout: float = 20.0) -> str | None:
    """从统计局「最新发布」列表页发现最新社零数据稿的完整 URL。"""
    r = requests.get(LIST_URL, timeout=timeout, headers=UA)
    r.encoding = r.apparent_encoding or "utf-8"
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if _REL_TITLE not in text or _REL_TITLE_EXCLUDE in text:
            continue
        href = a["href"]
        if "社会消费品零售总额" not in text:
            continue
        candidates.append((text, href))
    if not candidates:
        return None
    # 取标题里"年/月"最大者（列表本身按时间倒序，但显式比较更稳）
    def month_key(item):
        m = re.search(r"(20\d{2})年\s*1?\s*[—－-]\s*(\d{1,2})月", item[0])
        if m:
            return (int(m.group(1)), int(m.group(2)))
        m = re.search(r"(20\d{2})年\s*(\d{1,2})月", item[0])
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    _, href = max(candidates, key=month_key)
    from urllib.parse import urljoin
    return urljoin(LIST_URL, href)


def parse_sa_mom(text: str) -> dict[str, float]:
    """从新闻稿纯文本解析「5. 环比数据修订」表 → {YYYY-MM: 季调环比%}。

    典型文本片段（BeautifulSoup.get_text 后）::

        5.环比数据修订 根据季节调整模型... 年份 月份 环比增速（ % ）
        2025 年 10 月 0.11 11 月 -0.14 12 月 -0.08 2026 年 1 月 0.15 ...

    解析策略：定位"环比数据修订"锚点后，用正则扫描
    「(YYYY) 年 … (M) 月 … (-?数值)」序列，年份只在行首显式出现，
    后续月份沿用当前年份。数值取 0.01~1 量级，且严格带小数，避免误抓月份数字。
    """
    anchor = text.find("环比数据修订")
    if anchor < 0:
        return {}
    # 截到下一个章节（指标调整说明），避免把正文其他数字混进来
    end = text.find("指标调整说明", anchor)
    segment = text[anchor: end if end > anchor else anchor + 2000]

    # 归一化空格，便于正则
    seg = re.sub(r"\s+", " ", segment).strip()
    result: dict[str, float] = {}
    year = None
    # 匹配：年份 token / 月份 token / 数值 token（按出现顺序扫描）
    token = re.compile(r"(20\d{2})\s*年|(\d{1,2})\s*月|(-?\d+\.\d+)")
    cur_month = None
    for m in token.finditer(seg):
        if m.group(1):
            year = int(m.group(1))
        elif m.group(2):
            cur_month = int(m.group(2))
        elif m.group(3) and year and cur_month:
            val = float(m.group(3))
            if 1 <= cur_month <= 12:
                result[f"{year:04d}-{cur_month:02d}"] = val
            cur_month = None
    return result


def fetch_sa_mom(timeout: float = 20.0, *, no_cache: bool = False) -> tuple[dict[str, float], str]:
    """抓取最新社零季调环比。返回 (月份→环比%, 新闻稿URL)。异常向上抛。"""
    url = discover_latest_url(timeout)
    if not url:
        raise RuntimeError("未从统计局最新发布页发现社零新闻稿")
    now = datetime.now()
    cached = _CACHE.get(url)
    if cached and not no_cache and (now - cached[0]).total_seconds() < _CACHE_TTL_SEC:
        return dict(cached[1]), url
    r = requests.get(url, timeout=timeout, headers=UA)
    r.encoding = r.apparent_encoding or "utf-8"
    from bs4 import BeautifulSoup
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    data = parse_sa_mom(text)
    if not data:
        raise RuntimeError(f"新闻稿未解析出季调环比：{url}")
    _CACHE[url] = (now, dict(data))
    return data, url


def apply_sa_mom(df: pd.DataFrame, sa_map: dict[str, float], *,
                 source_label: str = "国家统计局（季调环比）") -> tuple[pd.DataFrame, int]:
    """把 sa_map 覆盖到 df 中 RETAIL_SALES 行的 环比(%)，返回 (新df, 覆盖行数)。

    只覆盖 sa_map 中存在的月份；其余月份保留原值（新闻稿只回溯约 13 个月）。
    """
    if df is None or df.empty or not sa_map:
        return df, 0
    out = df.copy()
    mask = out["指标代码"].astype(str).str.strip() == "RETAIL_SALES"
    if not mask.any():
        return out, 0
    months = out.loc[mask, "日期"].astype(str).str[:7]
    values = months.map(sa_map)
    hit = values.notna()
    count = int(hit.sum())
    if count:
        idx = values[hit].index
        out.loc[idx, "环比(%)"] = values[hit].values
        # 标注口径来源，便于审计（仅被覆盖的行）
        out.loc[idx, "数据源"] = source_label
    return out, count
