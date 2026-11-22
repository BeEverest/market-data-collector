"""
ETF 日频份额与规模采集（口径：每个交易日）。

数据源：
  沪市份额 - 上海证券交易所 query.sse.com.cn/commonQuery.do（按交易日查询）
  深市份额 - 深圳证券交易所 fund.szse.cn/api/report/ShowReport（按日期区间查询）
  单位净值 - 天天基金 api.fund.eastmoney.com/f10/lsjz
  规模计算 - 规模(亿元) = 份额(亿份) × 单位净值(元/份)

输出字段：基金代码 / 基金简称 / 交易市场 / 交易日期 / 份额(亿份) / 单位净值(元) /
          规模(亿元) / 数据源 / 采集时间

运维监控：每个接口调用经 monitor 记录，包含运行日期、失败原因、重试次数、耗时。
        collect_etf 支持 sse_dates / szse_range / nav_codes 部分重跑。
"""
from __future__ import annotations

import io
import logging
import time
from datetime import datetime

import pandas as pd
import requests

from monitor import describe_failure

logger = logging.getLogger(__name__)

SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_URL = "https://fund.szse.cn/api/report/ShowReport"
NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
SSE_HEADERS = {"Referer": "https://www.sse.com.cn/", "User-Agent": _UA}
SZSE_HEADERS = {"Referer": "https://fund.szse.cn/marketdata/fundslist/index.html", "User-Agent": _UA}
NAV_HEADERS = {"Referer": "https://fundf10.eastmoney.com/", "User-Agent": _UA}

OUTPUT_COLUMNS = [
    "基金代码", "基金简称", "交易市场", "交易日期",
    "份额(亿份)", "单位净值(元)", "规模(亿元)", "数据源", "采集时间",
]
_SHARE_COLUMNS = ["基金代码", "基金简称", "份额(亿份)", "交易日期", "交易市场"]
_NAV_COLUMNS = ["基金代码", "交易日期", "单位净值(元)"]

_name_map_cache: dict[str, str] | None = None


def _empty_shares() -> pd.DataFrame:
    return pd.DataFrame(columns=_SHARE_COLUMNS)


def _empty_nav() -> pd.DataFrame:
    return pd.DataFrame(columns=_NAV_COLUMNS)


def _report(monitor, *, key: str, name: str, status: str, params: dict,
            started: float, attempts: int, rows: int = 0, error: str = "") -> None:
    if monitor is None:
        return
    monitor.record(
        category="ETF", interface_key=key, interface_name=name,
        status=status, params=params, attempts=attempts, rows=rows,
        error=error, duration_sec=time.time() - started,
    )


def get_fund_name_map() -> dict[str, str]:
    """代码 -> 完整简称，优先使用交易所简称，此处仅作补全。"""
    global _name_map_cache
    if _name_map_cache is not None:
        return _name_map_cache
    _name_map_cache = {}
    try:
        import akshare as ak
        df = ak.fund_etf_fund_daily_em()
        if {"基金代码", "基金简称"}.issubset(df.columns):
            _name_map_cache = dict(
                zip(df["基金代码"].astype(str).str.zfill(6), df["基金简称"].astype(str))
            )
    except Exception as exc:
        logger.debug("ETF 名称映射获取失败（不影响主流程）：%s", exc)
    return _name_map_cache


def get_trade_dates(start: str, end: str | None = None, *, monitor=None) -> list[str]:
    """返回区间内交易日列表，格式 YYYY-MM-DD。"""
    import akshare as ak

    started = time.time()
    try:
        calendar = ak.tool_trade_date_hist_sina()
        dates = pd.to_datetime(calendar["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        mask = dates >= start
        if end:
            mask &= dates <= end
        result = dates[mask].tolist()
        _report(monitor, key="etf.trade_calendar", name="交易日历（新浪）",
                status="success" if result else "failed",
                params={"start": start, "end": end or ""}, started=started,
                attempts=1, rows=len(result),
                error="" if result else "交易日区间内无交易日")
        return result
    except Exception as exc:
        _report(monitor, key="etf.trade_calendar", name="交易日历（新浪）",
                status="failed", params={"start": start, "end": end or ""},
                started=started, attempts=1, error=describe_failure(exc))
        logger.error("交易日历获取失败：%s", exc)
        return []


def fetch_sse_shares(
    trade_date: str,
    retries: int = 3,
    retry_interval: float = 2.0,
    timeout: float = 15.0,
    session: requests.Session | None = None,
    *,
    monitor=None,
) -> pd.DataFrame:
    """获取上交所某一交易日全部 ETF 份额。原始 TOT_VOL 单位为万份，换算为亿份。"""
    sess = session or requests.Session()
    params = {
        "isPagination": "true",
        "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
        "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
        "STAT_DATE": trade_date,
    }
    started = time.time()
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = sess.get(SSE_URL, params=params, headers=SSE_HEADERS, timeout=timeout)
            response.raise_for_status()
            records = response.json().get("result") or []
            if not records:
                _report(monitor, key="etf.sse_shares", name=f"上交所ETF日份额({trade_date})",
                        status="failed", params={"trade_date": trade_date}, started=started,
                        attempts=attempt, error="接口返回空数据（无有效记录）")
                return _empty_shares()
            df = pd.DataFrame(records)
            out = df[["SEC_CODE", "SEC_NAME", "TOT_VOL"]].copy()
            out.columns = ["基金代码", "基金简称", "份额(亿份)"]
            out["基金代码"] = out["基金代码"].astype(str).str.zfill(6)
            out["份额(亿份)"] = pd.to_numeric(out["份额(亿份)"], errors="coerce") / 10000
            out["交易日期"] = trade_date
            out["交易市场"] = "SH"
            out = out[_SHARE_COLUMNS].dropna(subset=["份额(亿份)"])
            _report(monitor, key="etf.sse_shares", name=f"上交所ETF日份额({trade_date})",
                    status="success", params={"trade_date": trade_date}, started=started,
                    attempts=attempt, rows=len(out))
            return out
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_interval)

    _report(monitor, key="etf.sse_shares", name=f"上交所ETF日份额({trade_date})",
            status="failed", params={"trade_date": trade_date}, started=started,
            attempts=retries, error=describe_failure(last_exc))
    logger.warning("上交所 %s 份额采集失败：%s", trade_date, last_exc)
    return _empty_shares()


def fetch_szse_shares(
    start: str,
    end: str,
    retries: int = 3,
    retry_interval: float = 2.0,
    timeout: float = 20.0,
    session: requests.Session | None = None,
    *,
    monitor=None,
) -> pd.DataFrame:
    """获取深交所日期区间内全部 ETF 每日份额（一次请求可返回多日）。"""
    sess = session or requests.Session()
    params = {
        "CATALOGID": "scsj_fund_jjgm",
        "SHOWTYPE": "xlsx",
        "TABKEY": "tab1",
        "jjlb": "ETF",
        "txtStart": start,
        "txtEnd": end,
        "random": "0.123456789",
    }
    started = time.time()
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = sess.get(SZSE_URL, params=params, headers=SZSE_HEADERS, timeout=timeout)
            response.raise_for_status()
            df = pd.read_excel(io.BytesIO(response.content), engine="openpyxl", dtype={"基金代码": str})
            out = df[["日期", "基金代码", "基金简称", "基金规模(份)"]].copy()
            out.columns = ["交易日期", "基金代码", "基金简称", "份额(份)"]
            out["基金代码"] = out["基金代码"].astype(str).str.zfill(6)
            out["交易日期"] = pd.to_datetime(out["交易日期"], errors="coerce").dt.strftime("%Y-%m-%d")
            out["份额(亿份)"] = pd.to_numeric(out["份额(份)"], errors="coerce") / 1e8
            out["交易市场"] = "SZ"
            out = out[_SHARE_COLUMNS].dropna(subset=["交易日期", "份额(亿份)"])
            _report(monitor, key="etf.szse_shares", name=f"深交所ETF份额({start}~{end})",
                    status="success" if not out.empty else "failed",
                    params={"start": start, "end": end}, started=started,
                    attempts=attempt, rows=len(out),
                    error="" if not out.empty else "接口返回空数据（无有效记录）")
            return out
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_interval)

    _report(monitor, key="etf.szse_shares", name=f"深交所ETF份额({start}~{end})",
            status="failed", params={"start": start, "end": end}, started=started,
            attempts=retries, error=describe_failure(last_exc))
    logger.warning("深交所 %s ~ %s 份额采集失败：%s", start, end, last_exc)
    return _empty_shares()


def fetch_nav(
    code: str,
    start: str,
    end: str,
    retries: int = 3,
    retry_interval: float = 2.0,
    timeout: float = 15.0,
    session: requests.Session | None = None,
    *,
    monitor=None,
) -> pd.DataFrame:
    """获取单只基金在日期区间内的每日单位净值。"""
    sess = session or requests.Session()
    params = {
        "fundCode": code,
        "pageIndex": 1,
        "pageSize": 100,
        "startDate": start,
        "endDate": end,
    }
    started = time.time()
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            records: list[dict] = []
            while True:
                query = sess.get(NAV_URL, params=params, headers=NAV_HEADERS, timeout=timeout)
                query.raise_for_status()
                payload = query.json()
                page_rows = (payload.get("Data") or {}).get("LSJZList") or []
                records.extend(page_rows)
                total = int(payload.get("TotalCount") or 0)
                if not page_rows or params["pageIndex"] * params["pageSize"] >= total:
                    break
                params["pageIndex"] += 1

            if not records:
                _report(monitor, key=f"etf.nav.{code}", name=f"天天基金日净值({code})",
                        status="failed", params={"code": code, "start": start, "end": end},
                        started=started, attempts=attempt, error="接口返回空数据（无有效记录）")
                return _empty_nav()
            df = pd.DataFrame(records)[["FSRQ", "DWJZ"]].copy()
            df.columns = ["交易日期", "单位净值(元)"]
            df["基金代码"] = code
            df["单位净值(元)"] = pd.to_numeric(df["单位净值(元)"], errors="coerce")
            df = df[_NAV_COLUMNS].dropna(subset=["交易日期"])
            _report(monitor, key=f"etf.nav.{code}", name=f"天天基金日净值({code})",
                    status="success", params={"code": code, "start": start, "end": end},
                    started=started, attempts=attempt, rows=len(df))
            return df
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_interval)

    _report(monitor, key=f"etf.nav.{code}", name=f"天天基金日净值({code})",
            status="failed", params={"code": code, "start": start, "end": end},
            started=started, attempts=retries, error=describe_failure(last_exc))
    logger.warning("基金 %s 净值采集失败：%s", code, last_exc)
    return _empty_nav()


def collect_etf(
    codes: list[str],
    start_date: str | None = None,
    end_date: str | None = None,
    default_trade_days: int = 60,
    retries: int = 3,
    retry_interval: float = 2.0,
    timeout: float = 15.0,
    *,
    monitor=None,
    sse_dates: list[str] | None = None,
    szse_range: tuple[str, str] | None = None,
    nav_codes: list[str] | None = None,
) -> pd.DataFrame:
    """按交易日采集指定 ETF 的份额、单位净值与规模。

    部分重跑参数（供失败接口重跑使用）：
      sse_dates  - 仅采集这些交易日的沪市份额（None=区间内全部交易日）
      szse_range - 深市份额采集区间 (start, end)（None=默认区间）
      nav_codes  - 仅采集这些基金的日净值（None=全部目标基金）
    """
    fetch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    end = end_date or datetime.now().strftime("%Y-%m-%d")

    if start_date:
        start = start_date
    else:
        history = get_trade_dates("2000-01-01", end, monitor=monitor)
        start = history[-default_trade_days] if len(history) >= default_trade_days else (history[0] if history else end)

    trade_dates = get_trade_dates(start, end, monitor=monitor)
    logger.info("ETF 日频采集区间：%s ~ %s，共 %d 个交易日", start, end, len(trade_dates))
    if not trade_dates:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    target_codes = {str(code).strip().zfill(6) for code in codes if str(code).strip()}
    session = requests.Session()

    # 沪市：逐交易日拉取份额（部分重跑时只取指定交易日）
    sse_target_dates = (
        [d for d in trade_dates if d in set(sse_dates)] if sse_dates is not None else trade_dates
    )
    sse_frames = []
    for index, trade_date in enumerate(sse_target_dates, start=1):
        frame = fetch_sse_shares(trade_date, retries, retry_interval, timeout, session, monitor=monitor)
        if not frame.empty:
            sse_frames.append(frame)
        if index % 20 == 0:
            logger.info("上交所份额采集进度：%d/%d", index, len(sse_target_dates))

    # 深市：一次拉取整个区间（部分重跑时用指定区间）
    sz_start, sz_end = szse_range if szse_range else (start, end)
    szse_frame = fetch_szse_shares(sz_start, sz_end, retries, retry_interval, timeout, session, monitor=monitor)

    frames = sse_frames + ([szse_frame] if not szse_frame.empty else [])
    if not frames:
        logger.warning("未采集到任何 ETF 份额数据")
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    shares = pd.concat(frames, ignore_index=True)
    shares = shares[shares["基金代码"].isin(target_codes)].drop_duplicates(
        subset=["基金代码", "交易日期"], keep="last"
    )
    if shares.empty:
        logger.warning("配置的 ETF 代码在区间内无份额数据，请检查代码是否正确")
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    name_map = get_fund_name_map()
    shares["基金简称"] = shares.apply(
        lambda row: name_map.get(row["基金代码"]) or row["基金简称"], axis=1
    )

    # 净值：按基金代码拉取区间日净值（部分重跑时只取指定基金）
    nav_target_codes = sorted(nav_codes) if nav_codes is not None else sorted(shares["基金代码"].unique())
    nav_frames = []
    for code in nav_target_codes:
        frame = fetch_nav(code, start, end, retries, retry_interval, timeout, session, monitor=monitor)
        if not frame.empty:
            nav_frames.append(frame)
    nav_all = pd.concat(nav_frames, ignore_index=True) if nav_frames else _empty_nav()

    merged = shares.merge(nav_all, on=["基金代码", "交易日期"], how="left")
    merged["规模(亿元)"] = merged["份额(亿份)"] * merged["单位净值(元)"]
    merged["数据源"] = "份额：交易所（上交所/深交所）；净值：天天基金（东方财富）"
    merged["采集时间"] = fetch_time

    result = merged[OUTPUT_COLUMNS].sort_values(
        ["基金代码", "交易日期"], ascending=[True, False]
    ).reset_index(drop=True)
    logger.info("ETF 日频数据合并完成：%d 行", len(result))
    return result
