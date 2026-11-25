"""交易原始数据层采集（口径：每个交易日）。

职责：只调用接口并把原始数据**原样**落库，不换算单位、不计算任何指标。
计算逻辑统一放在数据处理模块的 processors/trade.py（从原始表派生 10 项指标）。

数据源（三级优先级：交易所 → akshare → 其他渠道）：
  - 交易所每日概况：上交所 stock_sse_deal_daily + 深交所 stock_szse_summary
    （一级源，成交额/成交量/流通市值；沪亿元/亿股，深元）
  - 两融账户：akshare stock_margin_account_info（二级源，13 列原样，T-1 更新）
  - 全A个股快照：akshare stock_zh_a_spot（二级源兜底，逐股量/额，仅当日快照不可回溯）
    成交集中度与涨跌家数无直采接口，由计算层从该快照派生。

范围：默认最近一个已结束交易日；可用 start_date/end_date 覆盖。
快照接口只返回当日数据，指定历史区间时只对当日快照落库（历史靠逐日采集积累）。
运维监控：每个接口一次调用，记录到 Monitor（状态/失败原因/重试次数/耗时）。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd

from collectors.trade_calendar import last_completed_trade_day
from monitor import describe_failure

logger = logging.getLogger(__name__)

SOURCE_EXCHANGE = "上交所/深交所每日概况（akshare 封装）"
SOURCE_MARGIN = "中国证券金融两融账户（akshare stock_margin_account_info）"
SOURCE_SNAPSHOT = "新浪财经全A快照（akshare stock_zh_a_spot）"

RAW_KEYS = ("exchange", "margin", "snapshot")


def catalog() -> list[dict]:
    """交易类别可采集的原始数据源清单，供 Web 精确采集下拉/预览。"""
    return [
        {"code": "exchange", "name": "交易所每日概况", "iface": "sse_deal_daily+szse_summary",
         "unit": "亿元/元", "granularity": "交易日", "source": SOURCE_EXCHANGE,
         "fields": "成交额/成交量/流通市值/总市值（沪深股票行）"},
        {"code": "margin", "name": "两融账户", "iface": "stock_margin_account_info",
         "unit": "亿元/人", "granularity": "交易日", "source": SOURCE_MARGIN,
         "fields": "13 列：融资/融券余额、维持担保比例、有负债投资者数等"},
        {"code": "snapshot", "name": "全A个股快照", "iface": "stock_zh_a_spot",
         "unit": "元", "granularity": "交易日", "source": SOURCE_SNAPSHOT,
         "fields": "逐股涨跌幅/成交量/成交额（派生集中度与涨跌家数）"},
        {"code": "breadth", "name": "市场宽度（涨跌家数）", "iface": "mx_comprehensive_finance_data",
         "unit": "家", "granularity": "交易日", "source": "东方财富MCP·全A涨跌家数",
         "fields": "上涨/下跌/平盘家数（历史补数）"},
        {"code": "congestion", "name": "成交集中度（前5%）", "iface": "legulegu api/stockdata/ashares-congestion",
         "unit": "%", "granularity": "交易日", "source": "乐咕乐股·大盘拥挤度",
         "fields": "前5%成交额/全部A股成交额/集中度（历史补数）"},
    ]


def _fetch_with_retry(fn, *, retries: int, retry_interval: float):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            df = fn()
            if df is not None and not df.empty:
                return df.copy()
            last_exc = None
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        if attempt < retries and retry_interval:
            time.sleep(retry_interval)
    if last_exc is not None:
        raise last_exc
    return pd.DataFrame()


def _report(monitor, *, key: str, name: str, status: str, params: dict,
            started: float, attempts: int, rows: int = 0, error: str = "") -> None:
    if monitor is None:
        return
    monitor.record(
        category="交易", interface_key=key, interface_name=name,
        status=status, params=params, attempts=attempts, rows=rows,
        error=error, duration_sec=time.time() - started,
    )


def _collect_exchange(start_date: str, end_date: str, *, retries: int,
                      retry_interval: float, monitor, collected_at: str) -> pd.DataFrame:
    """逐交易日采集沪深交易所概况原始字段。"""
    import akshare as ak

    dates = pd.date_range(start=start_date, end=end_date, freq="D")
    day_list = [d.strftime("%Y-%m-%d") for d in dates]
    # 精确采集可给长区间（如 2000-2020），逐日请求过多；限制为最近 60 个自然日内的工作日，
    # 长历史由计算层用已落库数据，不在一次采集中拉全量。
    if len(day_list) > 90:
        logger.warning("[交易·交易所] 区间 %s~%s 跨度过大，仅采集最近 90 自然日", start_date, end_date)
        day_list = day_list[-90:]
    frames = []
    for day in day_list:
        ymd = day.replace("-", "")
        sse_ok = szse_ok = False
        row = {"trade_date": day, "source": SOURCE_EXCHANGE, "collected_at": collected_at}
        # 上交所
        started = time.time()
        try:
            sse = _fetch_with_retry(lambda: ak.stock_sse_deal_daily(date=ymd),
                                    retries=retries, retry_interval=retry_interval)
            # 接口为长表：首列「单日情况」=[挂牌数/市价总值/流通市值/成交金额/成交量...]，
            # 「股票」等为数据列。按行名取值。
            if "单日情况" not in sse.columns or "股票" not in sse.columns:
                raise ValueError("上交所返回结构异常（缺「单日情况」或「股票」列）")
            def sse_val(item: str):
                matched = sse[sse["单日情况"] == item]
                if matched.empty:
                    return None
                return pd.to_numeric(matched.iloc[0]["股票"], errors="coerce")
            row.update({
                "sse_amount": sse_val("成交金额"),
                "sse_volume": sse_val("成交量"),
                "sse_float_mv": sse_val("流通市值"),
                "sse_total_mv": sse_val("市价总值"),
            })
            sse_ok = True
            _report(monitor, key="trade.sse_deal_daily", name="上交所每日概况",
                    status="success", params={"date": ymd}, started=started,
                    attempts=retries, rows=1)
        except Exception as exc:
            reason = describe_failure(exc, extra=f"接口=stock_sse_deal_daily | date={ymd}")
            _report(monitor, key="trade.sse_deal_daily", name="上交所每日概况",
                    status="failed", params={"date": ymd}, started=started,
                    attempts=retries, rows=0, error=reason)
            logger.warning("[交易·上交所] %s 失败：%s", day, reason)
        # 深交所
        started = time.time()
        try:
            szse = _fetch_with_retry(lambda: ak.stock_szse_summary(date=ymd),
                                     retries=retries, retry_interval=retry_interval)
            szse_stock = szse[szse["证券类别"] == "股票"]
            if szse_stock.empty:
                raise ValueError("深交所返回无「股票」行")
            z = szse_stock.iloc[0]
            row.update({
                "szse_amount": pd.to_numeric(z.get("成交金额"), errors="coerce"),
                "szse_float_mv": pd.to_numeric(z.get("流通市值"), errors="coerce"),
                "szse_total_mv": pd.to_numeric(z.get("总市值"), errors="coerce"),
            })
            szse_ok = True
            _report(monitor, key="trade.szse_summary", name="深交所证券类别统计",
                    status="success", params={"date": ymd}, started=started,
                    attempts=retries, rows=1)
        except Exception as exc:
            reason = describe_failure(exc, extra=f"接口=stock_szse_summary | date={ymd}")
            _report(monitor, key="trade.szse_summary", name="深交所证券类别统计",
                    status="failed", params={"date": ymd}, started=started,
                    attempts=retries, rows=0, error=reason)
            logger.warning("[交易·深交所] %s 失败：%s", day, reason)
        # 只有至少一个市场的核心成交额有效才落库，避免落全空/半空脏行
        if pd.notna(row.get("sse_amount")) or pd.notna(row.get("szse_amount")):
            frames.append(row)
        else:
            logger.warning("[交易·交易所] %s 沪深成交额均无效，跳过落库", day)
    if not frames:
        return pd.DataFrame()
    return pd.DataFrame(frames)


def _collect_margin(start_date: str, end_date: str, *, retries: int,
                    retry_interval: float, monitor, collected_at: str) -> pd.DataFrame:
    """采集两融账户 13 列原始数据（T-1，长历史接口）。"""
    import akshare as ak

    started = time.time()
    raw = _fetch_with_retry(ak.stock_margin_account_info,
                            retries=retries, retry_interval=retry_interval)
    if raw.empty:
        reason = "两融账户接口返回空数据"
        _report(monitor, key="trade.margin_account", name="两融账户信息",
                status="failed", params={"start": start_date, "end": end_date},
                started=started, attempts=retries, rows=0, error=reason)
        logger.warning("[交易·两融] %s", reason)
        return pd.DataFrame()
    raw = raw.copy()
    raw["日期"] = pd.to_datetime(raw["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    raw = raw.dropna(subset=["日期"])
    raw = raw[(raw["日期"] >= start_date) & (raw["日期"] <= end_date)]
    if raw.empty:
        _report(monitor, key="trade.margin_account", name="两融账户信息",
                status="success", params={"start": start_date, "end": end_date},
                started=started, attempts=retries, rows=0)
        return pd.DataFrame()
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
    out["source"] = SOURCE_MARGIN
    out["collected_at"] = collected_at
    _report(monitor, key="trade.margin_account", name="两融账户信息",
            status="success", params={"start": start_date, "end": end_date,
                                      "interface": "stock_margin_account_info"},
            started=started, attempts=retries, rows=len(out))
    logger.info("[交易·两融] 采集 %d 行（%s ~ %s）", len(out), start_date, end_date)
    return out


def _snapshot_trade_date(df: pd.DataFrame) -> str:
    """快照所属交易日：新浪快照时间戳多为纯时间（如 15:30:01），不含日期，
    故统一锚定「最近已结束交易日」。凌晨定时任务取到的是上一交易日收盘快照，
    与该锚定一致。若时间戳含日期（YYYY-MM-DD）则优先采用。"""
    if "时间戳" in df.columns:
        stamps = df["时间戳"].dropna().astype(str)
        if not stamps.empty:
            sample = stamps.iloc[0].strip()
            # 含完整日期才用；纯 HH:MM:SS 不可作交易日
            if len(sample) >= 10 and sample[4] in "-/" and sample[:4].isdigit():
                return sample[:10].replace("/", "-")
    return last_completed_trade_day()


def _collect_snapshot(start_date: str, end_date: str, *, retries: int,
                      retry_interval: float, monitor, collected_at: str) -> pd.DataFrame:
    """采集全A个股快照原始字段（仅当日，锚定快照实际交易日）。"""
    import akshare as ak

    started = time.time()
    raw = _fetch_with_retry(ak.stock_zh_a_spot,
                            retries=retries, retry_interval=retry_interval)
    if raw.empty:
        reason = "全A快照接口返回空数据"
        _report(monitor, key="trade.stock_snapshot", name="全A个股快照",
                status="failed", params={}, started=started, attempts=retries,
                rows=0, error=reason)
        logger.warning("[交易·快照] %s", reason)
        return pd.DataFrame()
    trade_date = _snapshot_trade_date(raw)
    # 快照只在落库日有效：若指定区间不含该交易日，跳过（不可回溯历史）
    if not (start_date <= trade_date <= end_date):
        _report(monitor, key="trade.stock_snapshot", name="全A个股快照",
                status="success", params={"snapshot_date": trade_date, "in_range": False},
                started=started, attempts=retries, rows=0)
        logger.info("[交易·快照] 快照日 %s 不在区间 %s~%s，跳过", trade_date, start_date, end_date)
        return pd.DataFrame()
    rename = {"代码": "code", "名称": "name", "涨跌幅": "pct_change",
              "成交量": "volume", "成交额": "amount", "最新价": "last_price"}
    out = raw.rename(columns=rename)
    keep = [c for c in rename.values() if c in out.columns]
    out = out[keep].copy()
    out["code"] = out["code"].astype(str).str.zfill(6)
    for col in ("pct_change", "volume", "amount", "last_price"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out["trade_date"] = trade_date
    out["source"] = SOURCE_SNAPSHOT
    out["collected_at"] = collected_at
    _report(monitor, key="trade.stock_snapshot", name="全A个股快照",
            status="success", params={"snapshot_date": trade_date,
                                      "interface": "stock_zh_a_spot"},
            started=started, attempts=retries, rows=len(out))
    logger.info("[交易·快照] 采集 %d 行（交易日 %s）", len(out), trade_date)
    return out


def _default_range() -> tuple[str, str]:
    day = last_completed_trade_day()
    return day, day


def collect_trade(codes: list[str] | None = None, start_date: str | None = None,
                  end_date: str | None = None, *, monitor=None, retries: int = 3,
                  retry_interval: float = 2.0, sources: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """采集交易原始数据，返回 {exchange/margin/snapshot: DataFrame}。

    codes    : 兼容接口，实际以 sources 选择原始数据源；codes 含 "exchange"/"margin"/"snapshot" 时等同 sources
    start/end: YYYY-MM-DD；留空时默认最近一个已结束交易日（单日）
    sources  : 要采集的原始源列表，留空采集全部三类
    """
    selected = list(sources or codes or RAW_KEYS)
    selected = [s for s in selected if s in RAW_KEYS]
    if not selected:
        selected = list(RAW_KEYS)

    if not start_date and not end_date:
        start_date, end_date = _default_range()
        logger.info("交易采集默认区间：%s（最近已结束交易日）", start_date)
    elif not start_date or not end_date:
        d_start, d_end = _default_range()
        start_date = start_date or d_start
        end_date = end_date or d_end
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    result: dict[str, pd.DataFrame] = {}
    if "exchange" in selected:
        result["exchange"] = _collect_exchange(start_date, end_date, retries=retries,
                                               retry_interval=retry_interval, monitor=monitor,
                                               collected_at=collected_at)
    if "margin" in selected:
        result["margin"] = _collect_margin(start_date, end_date, retries=retries,
                                           retry_interval=retry_interval, monitor=monitor,
                                           collected_at=collected_at)
    if "snapshot" in selected:
        result["snapshot"] = _collect_snapshot(start_date, end_date, retries=retries,
                                               retry_interval=retry_interval, monitor=monitor,
                                               collected_at=collected_at)
    return result
