"""宏观长周期历史补全。

背景：akshare 的 macro_rmb_loan / macro_rmb_deposit 只返回最近 30 个月，
导致「人民币贷款当月新增」「居民存款同比多增」等派生指标只有近两年。
本模块用可回溯到 2008/1993 的官方口径接口重建长周期余额序列：

  - 人民币贷款余额：macro_china_new_financial_credit（新增信贷，2008 起，含当月新增），
    以最近一期真实余额为锚点向前逐月减去当月新增，重建历史余额。
  - 居民储蓄存款余额：macro_china_supply_of_money（储蓄存款，1993 起，余额），
    直接作为长周期余额来源；与 macro_rmb_deposit 重叠区间以官方余额表为准。
  - 社零 1-2 月：统计局合并发布，akshare「当月/同比」为空但「累计-同比增长」有值，
    以累计同比补同比，累计增量补当月值。

所有函数返回「指标代码/名称/日期/数值/同比/环比/数据源/采集时间」统一 schema 的
DataFrame 片段，由 collect_macro 合并去重（同 biz_key 后来者覆盖）。
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from collectors.macro import PBC, NBS, _normalize_month, _to_float

logger = logging.getLogger(__name__)

LONG_LOAN_IFACE = "macro_china_new_financial_credit"
LONG_DEPOSIT_IFACE = "macro_china_supply_of_money"
RETAIL_IFACE = "macro_china_consumer_goods_retail"


def _shift_month(month: str, delta: int) -> str:
    y, m = (int(x) for x in month[:7].split("-"))
    total = y * 12 + (m - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _row(code, name, date, value, unit, yoy, mom, source, stamp):
    return {
        "指标代码": code, "指标名称": name, "日期": date,
        "数值": _to_float(value), "单位": unit,
        "同比(%)": _to_float(yoy), "环比(%)": _to_float(mom),
        "数据源": source, "采集时间": stamp,
    }


def build_long_loan_balance(fetch, stamp: str | None = None) -> pd.DataFrame:
    """用新增信贷长周期序列重建人民币贷款余额（锚定最近真实余额向前回溯）。"""
    stamp = stamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inc = fetch(LONG_LOAN_IFACE)
    if inc is None or inc.empty or "月份" not in inc.columns or "当月" not in inc.columns:
        return pd.DataFrame()
    cur = fetch("macro_rmb_loan")
    if cur is None or cur.empty or "累计人民币贷款-总额" not in cur.columns:
        return pd.DataFrame()

    inc_map: dict[str, float] = {}
    for _, r in inc.iterrows():
        m = _normalize_month(r["月份"])
        v = _to_float(r["当月"])
        if m and v is not None:
            inc_map[m] = v
    anchor_map: dict[str, float] = {}
    for _, r in cur.iterrows():
        m = _normalize_month(r["月份"])
        v = _to_float(r["累计人民币贷款-总额"])
        if m and v is not None:
            anchor_map[m] = v
    if not inc_map or not anchor_map:
        return pd.DataFrame()

    # 锚点取最早的真实余额，向前回溯（越早误差累积越大，但用于同比/环比差分不影响当月新增口径）
    anchor_m = min(anchor_map)
    balance = dict(anchor_map)
    m = anchor_m
    for _ in range(len(inc_map) + 2):
        # 月末余额递推：balance[m-1] = balance[m] - 当月(m)新增
        prev = _shift_month(m, -1)
        if prev not in inc_map:
            break
        if prev not in balance:
            balance[prev] = balance[m] - inc_map[m]
        m = prev

    rows = [
        _row("RMB_LOAN", "人民币贷款余额", k, v, "亿元", None, None,
             "中国人民银行（新增信贷长周期重建）", stamp)
        for k, v in sorted(balance.items())
    ]
    logger.info("  │ [宏观长周期] 人民币贷款余额重建 %d 个月（%s ~ %s）",
                len(rows), rows[0]["日期"] if rows else "-", rows[-1]["日期"] if rows else "-")
    return pd.DataFrame(rows)


def build_long_hh_deposit(fetch, stamp: str | None = None) -> pd.DataFrame:
    """用货币供应量表中的储蓄存款余额（1993 起）补全居民存款长周期。"""
    stamp = stamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    df = fetch(LONG_DEPOSIT_IFACE)
    if df is None or df.empty or "统计时间" not in df.columns or "储蓄存款" not in df.columns:
        return pd.DataFrame()
    rows = []
    for _, r in df.iterrows():
        m = _normalize_month(r["统计时间"])
        v = _to_float(r["储蓄存款"])
        if not m or v is None:
            continue
        rows.append(_row("HH_DEPOSIT", "居民储蓄存款余额", m, v, "亿元",
                         r.get("储蓄存款同比增长"), None,
                         "中国人民银行（货币供应量表·储蓄存款）", stamp))
    logger.info("  │ [宏观长周期] 居民储蓄存款余额补全 %d 个月", len(rows))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("日期").reset_index(drop=True)


def build_retail_jan_feb(fetch, stamp: str | None = None) -> pd.DataFrame:
    """用累计同比补社零 1-2 月（统计局合并发布，无当月值/同比）。"""
    stamp = stamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    df = fetch(RETAIL_IFACE)
    if df is None or df.empty or "月份" not in df.columns or "累计-同比增长" not in df.columns:
        return pd.DataFrame()
    rows = []
    prev_cum = None
    for _, r in df.iloc[::-1].iterrows():  # 从旧到新，便于累计差分
        m = _normalize_month(r["月份"])
        if not m or not m.endswith(("-01", "-02")):
            continue
        cum = _to_float(r.get("累计"))
        yoy = _to_float(r.get("累计-同比增长"))
        if yoy is None:
            continue
        # 2 月累计=1-2 月合计；1 月累计=1 月值。用同比补，累计差分可作为近似当月值。
        value = cum
        if m.endswith("-02") and prev_cum is not None and cum is not None:
            value = cum - prev_cum
        rows.append(_row("RETAIL_SALES", "社会消费品零售总额(当月)", m, value, "亿元",
                         yoy, None, "国家统计局（1-2月累计同比口径）", stamp))
        prev_cum = cum
    logger.info("  │ [宏观长周期] 社零 1-2 月补齐 %d 条", len(rows))
    return pd.DataFrame(rows)
