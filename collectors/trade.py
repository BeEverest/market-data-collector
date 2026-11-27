"""交易原始数据层采集（口径：每个交易日）。

职责：只调用接口并把原始数据**原样**落库，不换算单位、不计算任何指标。
计算逻辑统一放在数据处理模块的 processors/trade.py（从原始表派生 11 项指标）。

数据源（三级优先级：交易所 → akshare → 其他渠道）：
  - 交易所每日概况：上交所 stock_sse_deal_daily + 深交所 stock_szse_summary
    （一级源，成交额/成交量/流通市值；沪亿元/亿股，深元）
  - 两融账户：akshare stock_margin_account_info（二级源，13 列原样，T-1 更新）
  - 全A个股快照：akshare stock_zh_a_spot（二级源兜底，逐股量/额，仅当日快照不可回溯）
    成交集中度与涨跌家数无直采接口，由计算层从该快照派生。
  - 股债性价比：韭圈儿/投研工作台 api/equity-bond（三级源，沪深300 FED 风险溢价日频直采）

范围：默认最近一个已结束交易日；可用 start_date/end_date 覆盖。
快照接口只返回当日数据，指定历史区间时只对当日快照落库（历史靠逐日采集积累）。
运维监控：每个接口一次调用，记录到 Monitor（状态/失败原因/重试次数/耗时）。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd

import storage
from collectors.trade_calendar import last_completed_trade_day
from monitor import describe_failure

logger = logging.getLogger(__name__)

SOURCE_EXCHANGE = "上交所/深交所每日概况（akshare 封装）"
SOURCE_MARGIN = "中国证券金融两融账户（akshare stock_margin_account_info）"
SOURCE_SNAPSHOT = "新浪财经全A快照（akshare stock_zh_a_spot）"
SOURCE_BREADTH_SINA = "新浪财经全A快照（MCP失败降级派生）"
SOURCE_RISK_PREMIUM = "韭圈儿·股债性价比(FED风险溢价差值)"
FUNDDB_EQUITY_BOND_API = "https://ai2.jiucaishuo.com/api/equity-bond"

RAW_KEYS = ("exchange", "margin", "snapshot")
SOURCES_KEYS = RAW_KEYS + ("breadth", "congestion", "risk_premium")


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
         "fields": "上涨/下跌/平盘家数（支持精确采集与历史补数）"},
        {"code": "congestion", "name": "成交集中度（前5%）", "iface": "legulegu api/stockdata/ashares-congestion",
         "unit": "%", "granularity": "交易日", "source": "乐咕乐股·大盘拥挤度",
         "fields": "前5%成交额/全部A股成交额/集中度（支持精确采集与历史补数）"},
        {"code": "risk_premium", "name": "沪深300风险溢价率(FED差值)", "iface": "funddb api/equity-bond",
         "unit": "%", "granularity": "交易日", "source": SOURCE_RISK_PREMIUM,
         "fields": "value风险溢价率/ratio股债比/close指数点位/ma70/滚动均值与±1σ±2σ通道（数据源直采，日频全历史）"},
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


def _snapshot_market_closed(now: datetime | None = None) -> bool:
    """本地是否已过收盘且为工作日；盘中/周末不调用实时快照接口。"""
    now = now or datetime.now()
    if now.date().weekday() >= 5:
        return False
    return now.hour > 15 or (now.hour == 15 and now.minute >= 0)


def _snapshot_trade_date(df: pd.DataFrame, now: datetime | None = None) -> str:
    """快照所属交易日。

    新浪 `stock_zh_a_spot` 的时间戳多为纯时间（如 15:30:01），不含日期：
    - 含完整日期时优先采用；
    - 仅含时间时，必须确认本地已收盘（15:00 后）才标记为当天；
    - 盘中/凌晨取到的是实时或不稳定快照，禁止回退并错标到上一交易日。

    历史交易日请用 `tools/backfill_trade_snapshot.py` 按明确日期补数。
    """
    now = now or datetime.now()
    if "时间戳" in df.columns:
        stamps = df["时间戳"].dropna().astype(str)
        if not stamps.empty:
            sample = stamps.iloc[0].strip()
            if len(sample) >= 10 and sample[4] in "-/" and sample[:4].isdigit():
                return sample[:10].replace("/", "-")
    if (now.hour > 15 or (now.hour == 15 and now.minute >= 0)) and now.date().weekday() < 5:
        return now.date().isoformat()
    return ""


def _collect_snapshot(start_date: str, end_date: str, *, retries: int,
                      retry_interval: float, monitor, collected_at: str) -> pd.DataFrame:
    """采集全A个股快照原始字段（仅当日，锚定快照实际交易日）。"""
    import akshare as ak

    started = time.time()
    if not _snapshot_market_closed():
        # 盘中/周末的实时快照不稳定且不含日期；直接跳过，避免无效请求和错标交易日。
        reason = "未收盘或周末，跳过实时快照；不回退错标到上一交易日"
        _report(monitor, key="trade.stock_snapshot", name="全A个股快照",
                status="success", params={"skipped": True, "reason": reason},
                started=started, attempts=0, rows=0)
        logger.info("[交易·快照] %s", reason)
        return pd.DataFrame()
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
    if not trade_date:
        reason = "实时快照未收盘或时间戳无日期，禁止错标到上一交易日"
        _report(monitor, key="trade.stock_snapshot", name="全A个股快照",
                status="failed", params={}, started=started, attempts=retries,
                rows=0, error=reason)
        logger.warning("[交易·快照] %s", reason)
        return pd.DataFrame()
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


def _sina_snapshot_trade_date() -> str:
    """推断新浪快照对应的交易日：收盘后取当日（含当日），否则取最近已结束交易日。"""
    now = datetime.now()
    ref = now.date() + timedelta(days=1) if now.hour >= 15 else now.date()
    return last_completed_trade_day(ref)


def _breadth_from_snapshot(start_date: str, end_date: str, *, db_path,
                           collected_at: str) -> pd.DataFrame:
    """MCP 失败时从快照派生涨跌家数：优先已落库快照，其次实时新浪（仅当日）。"""
    have = set()
    if db_path:
        existing = storage.read_trade_raw(db_path, "breadth", start=start_date, end=end_date)
        if not existing.empty:
            have = {str(d)[:10] for d in existing["trade_date"]}
    requested = {d.strftime("%Y-%m-%d") for d in pd.date_range(start_date, end_date)
                 if d.weekday() < 5}
    missing_days = sorted(requested - have)
    if not missing_days:
        return pd.DataFrame()

    rows = []
    if db_path:
        snap = storage.read_trade_raw(db_path, "snapshot", start=start_date, end=end_date)
        if not snap.empty:
            for day, group in snap.groupby("trade_date"):
                day = str(day)[:10]
                if day in have:  # 已有原数据不覆盖
                    continue
                pct = pd.to_numeric(group.get("pct_change"), errors="coerce")
                rows.append({
                    "trade_date": day,
                    "up_count": int((pct > 0).sum()),
                    "down_count": int((pct < 0).sum()),
                    "flat_count": int((pct == 0).sum() + pct.isna().sum()),
                    "source": SOURCE_BREADTH_SINA,
                    "collected_at": collected_at,
                })
    if rows:
        return pd.DataFrame(rows)

    # 已落库快照无覆盖时才走实时新浪；快照只有当日，不能补历史
    import akshare as ak

    snap = ak.stock_zh_a_spot()
    day = _sina_snapshot_trade_date()
    if not (start_date <= day <= end_date):
        raise RuntimeError(
            f"新浪快照仅覆盖 {day}，不在请求区间 {start_date}~{end_date}；历史日期禁止跨日降级"
        )
    if day in have:
        return pd.DataFrame()
    pct = pd.to_numeric(snap.get("涨跌幅"), errors="coerce")
    return pd.DataFrame([{
        "trade_date": day,
        "up_count": int((pct > 0).sum()),
        "down_count": int((pct < 0).sum()),
        "flat_count": int((pct == 0).sum() + pct.isna().sum()),
        "source": SOURCE_BREADTH_SINA,
        "collected_at": collected_at,
    }])


def _breadth_all_present(db_path, start_date: str, end_date: str) -> bool:
    """请求区间内的工作日涨跌家数是否均已落库，避免已有时误报失败。"""
    if not db_path:
        return False
    requested = {d.strftime("%Y-%m-%d") for d in pd.date_range(start_date, end_date)
                 if d.weekday() < 5}
    if not requested:
        return False
    existing = storage.read_trade_raw(db_path, "breadth", start=start_date, end=end_date)
    have = {str(d)[:10] for d in existing["trade_date"]} if not existing.empty else set()
    return requested <= have


def _collect_breadth(start_date: str, end_date: str, *, monitor,
                     collected_at: str, db_path=None) -> pd.DataFrame:
    """按明确区间采集涨跌家数（东财 MCP），失败降级新浪快照并留痕。"""
    started = time.time()
    try:
        from tools.backfill_trade_breadth import SOURCE, parse_breadth
        from tools.backfill_trade_eastmoney import mcp_call

        query = (f"东方财富全A(沪深京)(800004.EI)指数在{start_date}至{end_date}每个交易日的"
                 "上涨家数、下跌家数、平盘家数，请完整列出所有交易日")
        got = parse_breadth(mcp_call(query, tool="mx_comprehensive_finance_data"))
        rows = [{
            "trade_date": day, "up_count": rec.get("up_count"),
            "down_count": rec.get("down_count"), "flat_count": rec.get("flat_count"),
            "source": SOURCE, "collected_at": collected_at,
        } for day, rec in sorted(got.items()) if start_date <= day <= end_date]
        df = pd.DataFrame(rows)
        _report(monitor, key="trade.market_breadth", name="涨跌家数",
                status="success", params={"start": start_date, "end": end_date},
                started=started, attempts=1, rows=len(df))
        return df
    except Exception as exc:  # noqa: BLE001 - 外部数据不可用时降级新浪
        fallback_error = ""
        try:
            df = _breadth_from_snapshot(start_date, end_date, db_path=db_path,
                                        collected_at=collected_at)
            if not df.empty:
                _report(monitor, key="trade.market_breadth", name="涨跌家数(新浪降级)",
                        status="success",
                        params={"start": start_date, "end": end_date,
                                "degraded_from": "mx_comprehensive_finance_data"},
                        started=started, attempts=1, rows=len(df),
                        error=describe_failure(exc))
                logger.warning("[交易·涨跌家数] MCP 失败，已降级新浪快照派生 %d 日：%s",
                               len(df), exc)
                return df
            if _breadth_all_present(db_path, start_date, end_date):
                _report(monitor, key="trade.market_breadth", name="涨跌家数(已存在跳过)",
                        status="success",
                        params={"start": start_date, "end": end_date,
                                "skipped_existing": True},
                        started=started, attempts=1, rows=0,
                        error=describe_failure(exc))
                logger.info("[交易·涨跌家数] MCP 失败，但请求区间原始数据已存在，跳过降级：%s", exc)
                return df
        except Exception as fb_exc:  # noqa: BLE001
            fallback_error = f"；新浪降级失败：{describe_failure(fb_exc)}"
        reason = describe_failure(exc) + fallback_error
        _report(monitor, key="trade.market_breadth", name="涨跌家数",
                status="failed", params={"start": start_date, "end": end_date},
                started=started, attempts=1, rows=0, error=reason)
        logger.warning("[交易·涨跌家数] 采集失败：%s", reason)
        return pd.DataFrame()


def _collect_congestion(start_date: str, end_date: str, *, monitor,
                        collected_at: str) -> pd.DataFrame:
    """按明确区间采集成交集中度（乐咕同源接口 + 最新表）。"""
    started = time.time()
    try:
        from tools.backfill_trade_congestion import (
            fetch_api_items, parse_api_items, parse_latest_table,
        )

        items, html = fetch_api_items()
        values: dict[str, dict] = {}
        values.update(parse_api_items(items, start_date, end_date))
        values.update(parse_latest_table(html, start_date, end_date))
        rows = []
        for day in sorted(values):
            row = dict(values[day])
            row["collected_at"] = collected_at
            rows.append(row)
        df = pd.DataFrame(rows)
        _report(monitor, key="trade.congestion", name="成交集中度",
                status="success", params={"start": start_date, "end": end_date},
                started=started, attempts=1, rows=len(df))
        return df
    except Exception as exc:  # noqa: BLE001 - 外部数据不可用时只记失败
        _report(monitor, key="trade.congestion", name="成交集中度",
                status="failed", params={"start": start_date, "end": end_date},
                started=started, attempts=1, rows=0, error=describe_failure(exc))
        logger.warning("[交易·成交集中度] 采集失败：%s", exc)
        return pd.DataFrame()


def _collect_risk_premium(start_date: str, end_date: str, *, monitor,
                          collected_at: str, retries: int = 3,
                          retry_interval: float = 2.0) -> pd.DataFrame:
    """直接采集沪深300风险溢价率（韭圈儿/投研工作台 FED 差值口径）。

    接口一次返回全历史日频序列，本函数只按请求区间过滤并原样落库，
    不在采集层做任何计算；风险溢价率 value 是数据源已计算好的指标。
    """
    import requests

    started = time.time()
    params = {
        "tab": "fed", "code": "000300.SH", "mode": "diff",
        "years": "-1", "years2": "5",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "Referer": "https://www.funddb.cn/indicators?tab=gap",
    }

    def fetch() -> pd.DataFrame:
        resp = requests.get(FUNDDB_EQUITY_BOND_API, params=params,
                            headers=headers, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("series"), list):
            raise RuntimeError(f"股债性价比接口返回异常：{str(payload)[:200]}")
        if not payload["series"]:
            raise RuntimeError("股债性价比接口返回空序列")
        return pd.DataFrame(payload["series"])

    try:
        source_df = _fetch_with_retry(fetch, retries=retries,
                                      retry_interval=retry_interval)
        if source_df.empty:
            raise RuntimeError("股债性价比接口未返回可用数据")
        rows = []
        for _, item in source_df.iterrows():
            day = str(item.get("date", ""))[:10]
            if not day or not (start_date <= day <= end_date):
                continue
            rows.append({
                "trade_date": day,
                "risk_premium": item.get("value"),
                "ratio": item.get("ratio"),
                "close": item.get("close"),
                "ma70": item.get("ma70"),
                "avg": item.get("avg"),
                "upper1std": item.get("upper1std"),
                "lower1std": item.get("lower1std"),
                "upper2std": item.get("upper2std"),
                "lower2std": item.get("lower2std"),
                "source": SOURCE_RISK_PREMIUM,
                "collected_at": collected_at,
            })
        df = pd.DataFrame(rows)
        _report(monitor, key="trade.risk_premium", name="沪深300风险溢价率(FED差值)",
                status="success", params={"start": start_date, "end": end_date,
                                          "tab": "fed", "code": "000300.SH", "mode": "diff"},
                started=started, attempts=1, rows=len(df))
        return df
    except Exception as exc:  # noqa: BLE001 - 外部数据不可用时只记失败
        reason = describe_failure(exc)
        _report(monitor, key="trade.risk_premium", name="沪深300风险溢价率(FED差值)",
                status="failed", params={"start": start_date, "end": end_date},
                started=started, attempts=retries, rows=0, error=reason)
        logger.warning("[交易·风险溢价] 采集失败：%s", reason)
        return pd.DataFrame()


def _default_range() -> tuple[str, str]:
    day = last_completed_trade_day()
    return day, day


def collect_trade(codes: list[str] | None = None, start_date: str | None = None,
                  end_date: str | None = None, *, monitor=None, retries: int = 3,
                  retry_interval: float = 2.0, sources: list[str] | None = None,
                  db_path=None) -> dict[str, pd.DataFrame]:
    """采集交易原始数据，返回 {exchange/margin/snapshot/breadth/congestion/risk_premium: DataFrame}。

    codes    : 兼容接口，实际以 sources 选择原始数据源
    start/end: YYYY-MM-DD；留空时默认最近一个已结束交易日（单日）
    sources  : 要采集的原始源列表，留空采集全部六类
    """
    selected = list(sources or codes or SOURCES_KEYS)
    selected = [s for s in selected if s in SOURCES_KEYS]
    if not selected:
        selected = list(SOURCES_KEYS)

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
    if "breadth" in selected:
        result["breadth"] = _collect_breadth(start_date, end_date, monitor=monitor,
                                             collected_at=collected_at, db_path=db_path)
    if "congestion" in selected:
        result["congestion"] = _collect_congestion(start_date, end_date, monitor=monitor,
                                                   collected_at=collected_at)
    if "risk_premium" in selected:
        result["risk_premium"] = _collect_risk_premium(
            start_date, end_date, monitor=monitor, collected_at=collected_at,
            retries=retries, retry_interval=retry_interval,
        )
    return result
