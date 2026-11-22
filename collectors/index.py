"""
A 股主要指数日频采集（口径：每个交易日）。

标的：上证指数 / 沪深300 / 科创50 / 创业板指
数据源：akshare stock_zh_index_daily（新浪财经，日线 OHLCV）
涨跌幅：由相邻交易日收盘价计算（首日为空）。

输出字段：指数代码 / 指数名称 / 交易日期 / 开盘价 / 最高价 / 最低价 /
          收盘价 / 成交量 / 涨跌幅(%) / 数据源 / 采集时间

采集范围：默认近一年（可由 start_date / end_date 覆盖）。
运维监控：每个指数接口一次调用，记录到 Monitor（状态/失败原因/重试次数/耗时）。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd

from monitor import describe_failure

logger = logging.getLogger(__name__)

SOURCE = "新浪财经（akshare 封装）"

# 指数目录：代码 -> (名称, akshare symbol)
INDEX_CATALOG: dict[str, dict] = {
    "000001": {"name": "上证指数", "symbol": "sh000001"},
    "000300": {"name": "沪深300", "symbol": "sh000300"},
    "000688": {"name": "科创50", "symbol": "sh000688"},
    "399006": {"name": "创业板指", "symbol": "sz399006"},
}

OUTPUT_COLUMNS = [
    "指数代码", "指数名称", "交易日期",
    "开盘价", "最高价", "最低价", "收盘价", "成交量", "涨跌幅(%)",
    "数据源", "采集时间",
]

DEFAULT_LOOKBACK_DAYS = 365


def catalog() -> list[dict]:
    """返回指数目录（含代码/名称/数据源），供 Web 精确采集下拉。"""
    return [
        {"code": code, "name": meta["name"], "iface": f"index.{meta['symbol']}",
         "unit": "点", "granularity": "交易日", "source": SOURCE}
        for code, meta in INDEX_CATALOG.items()
    ]


def _default_range(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> tuple[str, str]:
    today = date.today()
    start = today - timedelta(days=lookback_days)
    return start.isoformat(), today.isoformat()


def _fetch_index(symbol: str, *, retries: int = 1, retry_interval: float = 0.0) -> pd.DataFrame:
    """调用 akshare 获取单指数全历史日线（不做范围过滤）。"""
    import akshare as ak
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            df = ak.stock_zh_index_daily(symbol=symbol)
            if df is not None and not df.empty:
                return df.copy()
            last_exc = None
        except Exception as exc:
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
        category="指数", interface_key=key, interface_name=name,
        status=status, params=params, attempts=attempts, rows=rows,
        error=error, duration_sec=time.time() - started,
    )


def collect_index(
    codes: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    monitor=None,
    retries: int = 1,
    retry_interval: float = 0.0,
) -> pd.DataFrame:
    """采集主要指数近一年日频数据，返回统一 schema 的 DataFrame。

    codes       : 指数代码列表（6 位）；留空采集全部目录标的
    start/end   : YYYY-MM-DD；默认近一年（lookback_days 天前 ~ 今天）
    """
    targets = [str(c).strip().zfill(6) for c in (codes or list(INDEX_CATALOG)) if str(c).strip()]
    targets = [c for c in dict.fromkeys(targets) if c in INDEX_CATALOG]
    if not targets:
        logger.warning("指数代码为空或均不在目录中，未采集")
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    if not start_date or not end_date:
        d_start, d_end = _default_range(lookback_days)
        start_date = start_date or d_start
        end_date = end_date or d_end
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    frames: list[pd.DataFrame] = []
    for code in targets:
        meta = INDEX_CATALOG[code]
        symbol = meta["symbol"]
        key = f"index.{symbol}"
        started = time.time()
        attempts = 0
        try:
            raw = _fetch_index(symbol, retries=retries, retry_interval=retry_interval)
            attempts = max(1, retries)
            if raw.empty:
                raise ValueError("接口返回空数据（无有效记录）")
            df = raw.copy()
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            df = df.dropna(subset=["date"])
            df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
            if df.empty:
                raise ValueError(f"区间 {start_date}~{end_date} 内无交易日数据")
            df = df.sort_values("date").reset_index(drop=True)
            out = pd.DataFrame({
                "指数代码": code,
                "指数名称": meta["name"],
                "交易日期": df["date"],
                "开盘价": pd.to_numeric(df["open"], errors="coerce"),
                "最高价": pd.to_numeric(df["high"], errors="coerce"),
                "最低价": pd.to_numeric(df["low"], errors="coerce"),
                "收盘价": pd.to_numeric(df["close"], errors="coerce"),
                "成交量": pd.to_numeric(df["volume"], errors="coerce"),
            })
            out["涨跌幅(%)"] = (out["收盘价"].pct_change() * 100).round(4)
            out["数据源"] = SOURCE
            out["采集时间"] = collected_at
            out = out[OUTPUT_COLUMNS]
            frames.append(out)
            _report(monitor, key=key, name=meta["name"], status="success",
                    params={"code": code, "symbol": symbol, "start": start_date,
                            "end": end_date, "interface": "stock_zh_index_daily",
                            "source": SOURCE, "lookback_days": lookback_days,
                            "受影响标的": f"{meta['name']}({code})"},
                    started=started, attempts=attempts, rows=len(out))
            logger.info("指数 %s %s 采集完成：%d 行（%s ~ %s）",
                        code, meta["name"], len(out), start_date, end_date)
        except Exception as exc:
            reason = describe_failure(
                exc, extra=f"受影响标的={meta['name']}({code}) | "
                           f"接口=stock_zh_index_daily | symbol={symbol} | 区间={start_date}~{end_date}"
            )
            _report(monitor, key=key, name=meta["name"], status="failed",
                    params={"code": code, "symbol": symbol, "start": start_date,
                            "end": end_date, "interface": "stock_zh_index_daily",
                            "source": SOURCE, "lookback_days": lookback_days,
                            "受影响标的": f"{meta['name']}({code})"},
                    started=started, attempts=max(1, attempts or 1),
                    rows=0, error=reason)
            logger.error("指数 %s %s 采集失败：%s", code, meta["name"], reason)

    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    return pd.concat(frames, ignore_index=True)
