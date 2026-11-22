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

from monitor import describe_failure, describe_http_failure
from collectors.rate_limit import wait as throttle_wait
from collectors.trade_calendar import resolve_default_end

logger = logging.getLogger(__name__)


def _response_snippet(exc: Exception, limit: int = 200) -> str:
    """从请求异常的 response 中提取可读响应片段，用于失败原因补充消息。"""
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        text = " ".join(str(getattr(resp, "text", "") or "").split())
    except Exception:
        return ""
    return text[:limit]

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


_listing_date_cache: dict[str, str] = {}


def get_listing_dates(
    codes: list[str],
    *,
    monitor=None,
    retries: int = 2,
    timeout: float = 12.0,
) -> dict[str, str]:
    """解析 ETF 上市/成立日期（YYYY-MM-DD）。

    数据源：天天基金基金档案页（fundf10.eastmoney.com/jbgk_<code>.html）的「成立日期」。
    ETF 的交易所上市日通常紧随成立日，成立之前必然无有效份额/净值，故以成立日作为
    采集下界基准，保守且可靠。解析结果带模块级缓存，避免重复请求。
    返回 {6位基金代码: 日期}，解析失败的基金不纳入（采集时不过滤）。
    """
    import re

    todo = [str(c).strip().zfill(6) for c in codes if str(c).strip()]
    result = {c: _listing_date_cache[c] for c in todo if c in _listing_date_cache}
    missing = [c for c in todo if c not in result]
    if not missing:
        return result

    h = {
        "User-Agent": _UA,
        "Referer": "https://fundf10.eastmoney.com/",
    }
    pattern = re.compile(r"成立日期[：:]\s*<span>(\d{4}-\d{2}-\d{2})</span>")
    started = time.time()
    for code in missing:
        rec_ok = False
        for attempt in range(1, retries + 1):
            try:
                throttle_wait()
                resp = requests.get(
                    f"https://fundf10.eastmoney.com/jbgk_{code}.html",
                    headers=h, timeout=timeout,
                )
                resp.encoding = "utf-8"
                text = re.sub(r"\s+", " ", resp.text)
                m = pattern.search(text)
                if m:
                    _listing_date_cache[code] = m.group(1)
                    result[code] = m.group(1)
                    rec_ok = True
                    break
            except Exception as exc:
                if attempt == retries:
                    logger.debug("基金 %s 上市日期解析失败：%s", code, exc)
            time.sleep(0.2)
        if not rec_ok:
            _report(monitor, key=f"etf.listing_date.{code}",
                    name=f"基金上市日期({code})", status="failed",
                    params={"code": code}, started=started, attempts=retries,
                    error="档案页未解析到成立日期")
        else:
            _report(monitor, key=f"etf.listing_date.{code}",
                    name=f"基金上市日期({code})", status="success",
                    params={"code": code, "listing_date": result[code]},
                    started=started, attempts=attempt, rows=1)
    return result


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
    target_codes: list[str] | None = None,
) -> pd.DataFrame:
    """获取上交所某一交易日全部 ETF 份额。原始 TOT_VOL 单位为万份，换算为亿份。

    target_codes 仅用于在失败时标注受影响标的，不影响请求与返回。
    """
    sess = session or requests.Session()
    targets_label = ", ".join(sorted(set(str(c).zfill(6) for c in (target_codes or [])))) or "未指定"
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
            throttle_wait()
            response = sess.get(SSE_URL, params=params, headers=SSE_HEADERS, timeout=timeout)
            response.raise_for_status()
            records = response.json().get("result") or []
            sse_params = {
                "trade_date": trade_date, "market": "SH", "url": SSE_URL,
                "sqlId": params["sqlId"], "pageSize": params["pageHelp.pageSize"],
                "受影响标的": targets_label,
            }
            if not records:
                snippet = " ".join(str(getattr(response, "text", "") or "").split())[:200]
                _report(monitor, key="etf.sse_shares", name=f"上交所ETF日份额({trade_date})",
                        status="failed", params=sse_params, started=started,
                        attempts=attempt,
                        error=describe_failure(
                            None, empty=True,
                            extra=" | ".join(p for p in (
                                f"响应片段={snippet}" if snippet else "",
                                f"受影响标的={targets_label}",
                            ) if p),
                        ))
                return _empty_shares()
            df = pd.DataFrame(records)
            out = df[["SEC_CODE", "SEC_NAME", "TOT_VOL"]].copy()
            out.columns = ["基金代码", "基金简称", "份额(亿份)"]
            out["基金代码"] = out["基金代码"].astype(str).str.strip().str.zfill(6)
            out["份额(亿份)"] = pd.to_numeric(out["份额(亿份)"], errors="coerce") / 10000
            out["交易日期"] = trade_date
            out["交易市场"] = "SH"
            out = out[_SHARE_COLUMNS].dropna(subset=["份额(亿份)"])
            _report(monitor, key="etf.sse_shares", name=f"上交所ETF日份额({trade_date})",
                    status="success", params=sse_params, started=started,
                    attempts=attempt, rows=len(out))
            return out
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                # 指数退避 + 抖动，避免固定节律被反爬识别
                time.sleep(retry_interval * (2 ** (attempt - 1)))

    _report(monitor, key="etf.sse_shares", name=f"上交所ETF日份额({trade_date})",
            status="failed", params={
                "trade_date": trade_date, "market": "SH", "url": SSE_URL,
                "sqlId": params["sqlId"], "pageSize": params["pageHelp.pageSize"],
                "受影响标的": targets_label,
            }, started=started,
            attempts=retries,
            error=describe_http_failure(
                last_exc, url=SSE_URL,
                response_text=_response_snippet(last_exc),
                extra=f"受影响标的={targets_label}",
            ))
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
    target_codes: list[str] | None = None,
) -> pd.DataFrame:
    """获取深交所日期区间内全部 ETF 每日份额（一次请求可返回多日）。

    target_codes 为本批次关注的标的，仅用于在失败时明确列出「受影响的标的」，
    便于监控看板定位问题范围；不参与请求参数，也不过滤返回结果。
    """
    sess = session or requests.Session()
    targets_label = ", ".join(sorted(set(str(c).zfill(6) for c in (target_codes or [])))) or "未指定"
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
            throttle_wait()
            response = sess.get(SZSE_URL, params=params, headers=SZSE_HEADERS, timeout=timeout)
            response.raise_for_status()
            df = pd.read_excel(io.BytesIO(response.content), engine="openpyxl", dtype={"基金代码": str})
            out = df[["日期", "基金代码", "基金简称", "基金规模(份)"]].copy()
            out.columns = ["交易日期", "基金代码", "基金简称", "份额(份)"]
            # 深交所部分交易日（如 2026-11-20）导出的基金代码带尾随空格，
            # 需先 strip 再补零，否则后续按 target_codes 匹配会整批丢失。
            out["基金代码"] = out["基金代码"].astype(str).str.strip().str.zfill(6)
            out["交易日期"] = pd.to_datetime(out["交易日期"], errors="coerce").dt.strftime("%Y-%m-%d")
            out["份额(亿份)"] = pd.to_numeric(out["份额(份)"], errors="coerce") / 1e8
            out["交易市场"] = "SZ"
            out = out[_SHARE_COLUMNS].dropna(subset=["交易日期", "份额(亿份)"])
            szse_params = {
                "start": start, "end": end, "market": "SZ", "url": SZSE_URL,
                "catalog_id": params["CATALOGID"], "showtype": params["SHOWTYPE"],
                "jjlb": params["jjlb"], "受影响标的": targets_label,
            }
            if out.empty:
                resp_hint = f"响应=xlsx 空表（0 行有效记录，{len(response.content)} 字节）"
                _report(monitor, key="etf.szse_shares", name=f"深交所ETF份额({start}~{end})",
                        status="failed", params=szse_params, started=started,
                        attempts=attempt, rows=0,
                        error=f"接口返回空数据（无有效记录） | 受影响标的={targets_label} | {resp_hint}")
            else:
                _report(monitor, key="etf.szse_shares", name=f"深交所ETF份额({start}~{end})",
                        status="success", params=szse_params, started=started,
                        attempts=attempt, rows=len(out))
                logger.info("深交所份额采集成功：%s ~ %s，返回 %d 行（受影响标的：%s）",
                            start, end, len(out), targets_label)
            return out
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_interval * (2 ** (attempt - 1)))

    _report(monitor, key="etf.szse_shares", name=f"深交所ETF份额({start}~{end})",
            status="failed", params={
                "start": start, "end": end, "market": "SZ", "url": SZSE_URL,
                "catalog_id": params["CATALOGID"], "showtype": params["SHOWTYPE"],
                "jjlb": params["jjlb"], "受影响标的": targets_label,
            }, started=started,
            attempts=retries,
            error=describe_http_failure(
                last_exc, url=SZSE_URL,
                response_text=_response_snippet(last_exc),
                extra=f"受影响标的={targets_label}",
            ))
    logger.warning("深交所 %s ~ %s 份额采集失败（受影响标的：%s）：%s",
                   start, end, targets_label, last_exc)
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
                throttle_wait()
                query = sess.get(NAV_URL, params=params, headers=NAV_HEADERS, timeout=timeout)
                query.raise_for_status()
                payload = query.json()
                page_rows = (payload.get("Data") or {}).get("LSJZList") or []
                records.extend(page_rows)
                total = int(payload.get("TotalCount") or 0)
                if not page_rows or params["pageIndex"] * params["pageSize"] >= total:
                    break
                params["pageIndex"] += 1

            nav_params = {
                "code": code, "start": start, "end": end, "url": NAV_URL,
                "source": "天天基金", "page_size": params["pageSize"],
                "受影响标的": str(code).zfill(6),
            }
            if not records:
                snippet = " ".join(str(getattr(query, "text", "") or "").split())[:200]
                _report(monitor, key=f"etf.nav.{code}", name=f"天天基金日净值({code})",
                        status="failed", params=nav_params,
                        started=started, attempts=attempt,
                        error=describe_failure(
                            None, empty=True,
                            extra=" | ".join(p for p in (
                                f"响应片段={snippet}" if snippet else "",
                                f"受影响标的={str(code).zfill(6)}",
                            ) if p),
                        ))
                return _empty_nav()
            df = pd.DataFrame(records)[["FSRQ", "DWJZ"]].copy()
            df.columns = ["交易日期", "单位净值(元)"]
            df["基金代码"] = code
            df["单位净值(元)"] = pd.to_numeric(df["单位净值(元)"], errors="coerce")
            df = df[_NAV_COLUMNS].dropna(subset=["交易日期"])
            _report(monitor, key=f"etf.nav.{code}", name=f"天天基金日净值({code})",
                    status="success", params=nav_params,
                    started=started, attempts=attempt, rows=len(df))
            return df
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_interval * (2 ** (attempt - 1)))

    _report(monitor, key=f"etf.nav.{code}", name=f"天天基金日净值({code})",
            status="failed", params={
                "code": code, "start": start, "end": end, "url": NAV_URL,
                "source": "天天基金", "page_size": params["pageSize"],
                "受影响标的": str(code).zfill(6),
            }, started=started, attempts=retries,
            error=describe_http_failure(
                last_exc, url=NAV_URL, response_text=_response_snippet(last_exc),
                extra=f"受影响标的={str(code).zfill(6)}",
            ))
    logger.warning("基金 %s 净值采集失败：%s", code, last_exc)
    return _empty_nav()


def collect_etf(
    codes: list[str],
    start_date: str | None = None,
    end_date: str | None = None,
    default_trade_days: int = 1,
    retries: int = 3,
    retry_interval: float = 2.0,
    timeout: float = 15.0,
    *,
    monitor=None,
    sse_dates: list[str] | None = None,
    szse_range: tuple[str, str] | None = None,
    nav_codes: list[str] | None = None,
    listing_dates: dict[str, str] | None = None,
) -> pd.DataFrame:
    """按交易日采集指定 ETF 的份额、单位净值与规模。

    部分重跑参数（供失败接口重跑使用）：
      sse_dates  - 仅采集这些交易日的沪市份额（None=区间内全部交易日）
      szse_range - 深市份额采集区间 (start, end)（None=默认区间）
      nav_codes  - 仅采集这些基金的日净值（None=全部目标基金）
      listing_dates - {基金代码: 上市/成立日}；早于该日期的交易日记录会被忽略。
                      传入的 dict 会被原地补全解析结果，供调用方持久化。
    """
    fetch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 默认锚定到最近一个已结束交易日：凌晨运行时"今天"数据尚未披露，采当天必为空。
    # 显式 end_date 始终最高优先。
    end = resolve_default_end(end_date)
    logger.info("ETF 采集区间结束日：%s%s", end, "（显式指定）" if (end_date or "").strip() else "（最近已结束交易日）")

    if start_date:
        start = start_date
    else:
        history = get_trade_dates("2000-01-01", end, monitor=monitor)
        start = history[-default_trade_days] if len(history) >= default_trade_days else (history[0] if history else end)

    target_codes = {str(code).strip().zfill(6) for code in codes if str(code).strip()}
    session = requests.Session()

    # 解析并补全上市/成立日期；调用方传入的 dict 会被原地更新，便于持久化到配置
    if listing_dates is None:
        listing_dates = {}
    resolved = get_listing_dates(sorted(target_codes), monitor=monitor)
    for code, date in resolved.items():
        listing_dates[code] = date
    if listing_dates:
        logger.info("ETF 上市日期基准：%d 只已解析", len(listing_dates))

    # 抓取下界裁剪到「目标基金中最早的上市日」，避免请求全部基金都未上市的交易日
    effective_start = start
    if listing_dates:
        earliest = min(listing_dates.values())
        if effective_start is None or earliest > effective_start:
            logger.info("采集区间起点由上市日裁剪：%s -> %s", effective_start or "默认", earliest)
            effective_start = earliest

    # 裁剪后起点已晚于结束日：用户指定范围全部早于上市日，无需任何接口调用
    if effective_start and end and effective_start > end:
        logger.info("采集区间 %s ~ %s 全部早于基金上市日，忽略全部记录", start, end)
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    trade_dates = get_trade_dates(effective_start or "2000-01-01", end, monitor=monitor)
    logger.info("ETF 日频采集区间：%s ~ %s，共 %d 个交易日", effective_start or start, end, len(trade_dates))
    if not trade_dates:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    # 沪市：逐交易日拉取份额（部分重跑时只取指定交易日）
    sse_target_dates = (
        [d for d in trade_dates if d in set(sse_dates)] if sse_dates is not None else trade_dates
    )
    # 按市场拆分目标标的，失败时用于明确列出「受影响的标的」
    sh_targets = [c for c in sorted(target_codes) if c.startswith(("5", "6"))]
    sz_targets = [c for c in sorted(target_codes) if c.startswith(("1", "8"))]

    sse_frames = []
    for index, trade_date in enumerate(sse_target_dates, start=1):
        frame = fetch_sse_shares(trade_date, retries, retry_interval, timeout, session,
                                 monitor=monitor, target_codes=sh_targets)
        if not frame.empty:
            sse_frames.append(frame)
        if index % 20 == 0:
            logger.info("上交所份额采集进度：%d/%d", index, len(sse_target_dates))

    # 深市：一次拉取整个区间（部分重跑时用指定区间）
    sz_start, sz_end = szse_range if szse_range else (effective_start or start, end)
    logger.info("开始采集深交所份额：%s ~ %s，深市标的 %d 只（%s）",
                sz_start, sz_end, len(sz_targets), ", ".join(sz_targets) or "无")
    szse_frame = fetch_szse_shares(sz_start, sz_end, retries, retry_interval, timeout, session,
                                   monitor=monitor, target_codes=sz_targets)

    frames = sse_frames + ([szse_frame] if not szse_frame.empty else [])
    if not frames:
        logger.warning("未采集到任何 ETF 份额数据")
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    shares = pd.concat(frames, ignore_index=True)
    # 兜底统一代码格式：去除交易所导出数据偶带的空白/非数字字符后补零，
    # 避免任何来源的格式差异导致按 target_codes 匹配时整批丢失。
    shares["基金代码"] = (
        shares["基金代码"].astype(str).str.strip().str.replace(r"\D", "", regex=True).str.zfill(6)
    )
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
        nav_start = start
        listing = listing_dates.get(code)
        if listing and start and listing > start:
            nav_start = listing  # 净值只从上市/成立日起取
        frame = fetch_nav(code, nav_start, end, retries, retry_interval, timeout, session, monitor=monitor)
        if not frame.empty:
            nav_frames.append(frame)
    nav_all = pd.concat(nav_frames, ignore_index=True) if nav_frames else _empty_nav()

    merged = shares.merge(nav_all, on=["基金代码", "交易日期"], how="left")

    # 忽略早于该基金上市/成立日的交易日记录（成立前无有效份额与净值）
    before = len(merged)
    if listing_dates:
        floor = merged["基金代码"].map(listing_dates)
        mask = floor.isna() | (merged["交易日期"] >= floor)
        ignored = int((~mask).sum())
        merged = merged[mask]
        if ignored:
            logger.info("已忽略早于上市日的记录 %d 行", ignored)
    merged["规模(亿元)"] = merged["份额(亿份)"] * merged["单位净值(元)"]
    merged["数据源"] = "份额：交易所（上交所/深交所）；净值：天天基金（东方财富）"
    merged["采集时间"] = fetch_time

    result = merged[OUTPUT_COLUMNS].sort_values(
        ["基金代码", "交易日期"], ascending=[True, False]
    ).reset_index(drop=True)
    logger.info("ETF 日频数据合并完成：%d 行（过滤前 %d 行）", len(result), before)
    return result
