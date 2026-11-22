"""交易日历工具：解析"最近一个交易日"采集锚点。

背景：定时调度常在凌晨运行，此时"今天"的交易所份额/净值数据尚未披露
（交易所 T 日份额于 T+1 早间才最终更新），若采集区间结束日取"今天"，
上交所按日查询会返回空数据并记为失败。因此默认区间锚定到
**最近一个已结束的交易日**（严格早于今天的最近交易日，T-1）。

数据源与 ETF 交易日历一致：akshare.tool_trade_date_hist_sina（新浪）。
失败时回退到"昨天"（自然日），保证采集流程不中断。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)


def _calendar_dates() -> list[str]:
    """返回升序的 YYYY-MM-DD 交易日列表；失败返回空列表。"""
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        import pandas as pd
        dates = pd.to_datetime(cal["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        return sorted(d for d in dates.tolist() if isinstance(d, str))
    except Exception as exc:  # noqa: BLE001 - 交易日历不可用时降级
        logger.warning("交易日历获取失败，降级为自然日回退：%s", exc)
        return []


def last_completed_trade_day(reference: date | None = None) -> str:
    """返回严格早于 reference（默认今天）的最近一个交易日 YYYY-MM-DD。

    - 优先取交易日历中 < 今天的最近交易日；
    - 交易日历不可用/无结果时，回退到昨天（自然日），跳过周末。
    """
    today = reference or date.today()
    cal = _calendar_dates()
    if cal:
        earlier = [d for d in cal if d < today.isoformat()]
        if earlier:
            return earlier[-1]
    # 降级：最近的工作日（严格早于 today）
    fallback = today - timedelta(days=1)
    while fallback.weekday() >= 5:  # 5=周六, 6=周日
        fallback -= timedelta(days=1)
    return fallback.isoformat()


def resolve_default_end(end_date: str | None, reference: date | None = None) -> str:
    """解析采集结束日：显式 end_date 优先，否则取最近一个已结束交易日。"""
    return (end_date or "").strip() or last_completed_trade_day(reference)
