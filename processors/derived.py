"""派生指标计算（数据处理模块）。

只读取宏观观测表，按确认口径计算 14 项指标；所有规则为纯函数，便于单测。
关键口径：
  - 进出口同比：官方累计出口+累计进口，对比去年同月累计（TRADE_CUM_TOTAL）
  - 人民币贷款当月新增：RMB_LOAN 余额差分
  - 居民存款新增/同比多增：HH_DEPOSIT 余额差分/二阶差分
  - 黄金当月新增：万盎司差分 × 31.1034768 ÷ 100 = 吨
    （1 万盎司 = 1e4 盎司 × 31.1034768 克 ÷ 1e6 克/吨 = 0.311034768 吨）
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

GROUPS = {
    "A": "A. 价格与通胀",
    "B": "B. 货币与信用",
    "C": "C. 存贷款与储备",
}

# 1 万盎司 = 10^4 盎司 × 31.1034768 克/盎司 ÷ 10^6 克/吨 = 0.311034768 吨
GOLD_TON_PER_WAN_OZ = 31.1034768 / 100

INDICATORS: dict[str, dict] = {
    "TRADE_YOY": {
        "name": "进出口同比", "group": "A", "unit": "%",
        "formula": "(今年累计出口+累计进口) ÷ (去年同月累计出口+累计进口) − 1（官方累计口径）",
    },
    "RETAIL_SALES_YOY": {
        "name": "社零同比", "group": "A", "unit": "%",
        "formula": "RETAIL_SALES 同比(%) 直接取数",
    },
    "CPI_YOY": {
        "name": "CPI同比", "group": "A", "unit": "%",
        "formula": "CPI 同比(%) 直接取数",
    },
    "PPI_YOY": {
        "name": "PPI同比", "group": "A", "unit": "%",
        "formula": "PPI 同比(%) 直接取数",
    },
    "CPI_PPI_GAP": {
        "name": "CPI-PPI剪刀差", "group": "A", "unit": "百分点",
        "formula": "CPI同比 − PPI同比",
    },
    "SFR_STOCK_YOY": {
        "name": "社融存量同比", "group": "B", "unit": "%",
        "formula": "优先取央行官方增速；缺失时按存量值同比派生",
    },
    "M1_YOY": {
        "name": "M1同比增速", "group": "B", "unit": "%",
        "formula": "M1 同比(%) 直接取数",
    },
    "M2_YOY": {
        "name": "M2同比增速", "group": "B", "unit": "%",
        "formula": "M2 同比(%) 直接取数",
    },
    "M1_M2_GAP": {
        "name": "M1-M2剪刀差", "group": "B", "unit": "百分点",
        "formula": "M1同比 − M2同比",
    },
    "A_SHARE_MC_M2": {
        "name": "A股总市值/M2", "group": "B", "unit": "%",
        "formula": "A股总市值 ÷ M2 × 100%",
    },
    "HH_DEPOSIT_NEW": {
        "name": "居民存款新增", "group": "C", "unit": "亿元",
        "formula": "居民存款余额当月 − 上月（余额差分）",
    },
    "HH_DEPOSIT_NEW_YOY": {
        "name": "居民存款同比多增", "group": "C", "unit": "亿元",
        "formula": "本月新增 − 去年同月新增（二阶差分）",
    },
    "RMB_LOAN_NEW": {
        "name": "人民币贷款当月新增", "group": "C", "unit": "亿元",
        "formula": "人民币贷款余额当月 − 上月（余额差分）",
    },
    "GOLD_NEW_TON": {
        "name": "黄金当月新增", "group": "C", "unit": "吨",
        "formula": "(黄金储备当月−上月)万盎司 × 31.1034768 ÷ 100（1万盎司=0.311034768吨）",
    },
}


def _shift_month(month: str, delta: int) -> str:
    year, mon = (int(x) for x in str(month)[:7].split("-"))
    total = year * 12 + (mon - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _sub(df: pd.DataFrame, code: str) -> pd.DataFrame:
    for col in ("指标代码", "code"):
        if col in df.columns:
            return df[df[col] == code]
    return df.iloc[0:0]


def _values(df: pd.DataFrame, code: str) -> dict[str, float]:
    sub = _sub(df, code)
    if sub.empty or "数值" not in sub.columns:
        return {}
    out: dict[str, float] = {}
    for date, val in zip(sub["日期"], sub["数值"]):
        if pd.isna(val):
            continue
        try:
            out[str(date)[:7]] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def _yoy(df: pd.DataFrame, code: str) -> dict[str, float]:
    sub = _sub(df, code)
    if sub.empty or "同比(%)" not in sub.columns:
        return {}
    out: dict[str, float] = {}
    for date, val in zip(sub["日期"], sub["同比(%)"]):
        if pd.isna(val):
            continue
        try:
            out[str(date)[:7]] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def compute_derived(macro_df: pd.DataFrame, computed_at: str | None = None) -> pd.DataFrame:
    """输入宏观观测 DataFrame，输出派生指标 DataFrame（缺失值不产出行）。"""
    if macro_df is None or macro_df.empty:
        return pd.DataFrame()
    if "日期" not in macro_df.columns:
        return pd.DataFrame()
    stamp = computed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dates = sorted({str(d)[:7] for d in macro_df["日期"] if str(d)[:7] >= "2000-01"})

    raw = {
        "trade_cum": _values(macro_df, "TRADE_CUM_TOTAL"),
        "sfr": _values(macro_df, "SFR_STOCK"),
        "m1": _values(macro_df, "M1"),
        "m2": _values(macro_df, "M2"),
        "mc": _values(macro_df, "A_SHARE_MC"),
        "hh": _values(macro_df, "HH_DEPOSIT"),
        "loan": _values(macro_df, "RMB_LOAN"),
        "gold": _values(macro_df, "GOLD_RESERVE"),
    }
    yoy = {
        "retail": _yoy(macro_df, "RETAIL_SALES"),
        "cpi": _yoy(macro_df, "CPI"),
        "ppi": _yoy(macro_df, "PPI"),
        "m1": _yoy(macro_df, "M1"),
        "m2": _yoy(macro_df, "M2"),
        "sfr": _yoy(macro_df, "SFR_STOCK"),
    }

    new_hh = {d: raw["hh"][d] - raw["hh"][_shift_month(d, -1)]
              for d in dates if d in raw["hh"] and _shift_month(d, -1) in raw["hh"]}
    new_loan = {d: raw["loan"][d] - raw["loan"][_shift_month(d, -1)]
                for d in dates if d in raw["loan"] and _shift_month(d, -1) in raw["loan"]}

    rows: list[dict] = []

    def emit(code: str, date: str, value: float) -> None:
        if value is None or pd.isna(value):
            return
        meta = INDICATORS[code]
        rows.append({
            "code": code,
            "name": meta["name"],
            "date": date,
            "value": round(float(value), 4),
            "unit": meta["unit"],
            "formula": meta["formula"],
            "grp": GROUPS[meta["group"]],
            "source": "macro_observation",
            "computed_at": stamp,
        })

    for d in dates:
        prev_y = _shift_month(d, -12)
        prev_m = _shift_month(d, -1)

        # A. 价格与通胀
        if d in raw["trade_cum"] and prev_y in raw["trade_cum"] and raw["trade_cum"][prev_y]:
            emit("TRADE_YOY", d, (raw["trade_cum"][d] / raw["trade_cum"][prev_y] - 1) * 100)
        for code, key in (("RETAIL_SALES_YOY", "retail"), ("CPI_YOY", "cpi"), ("PPI_YOY", "ppi")):
            if d in yoy[key]:
                emit(code, d, yoy[key][d])
        if d in yoy["cpi"] and d in yoy["ppi"]:
            emit("CPI_PPI_GAP", d, yoy["cpi"][d] - yoy["ppi"][d])

        # B. 货币与信用
        if d in yoy["sfr"]:
            emit("SFR_STOCK_YOY", d, yoy["sfr"][d])
        elif d in raw["sfr"] and prev_y in raw["sfr"] and raw["sfr"][prev_y]:
            emit("SFR_STOCK_YOY", d, (raw["sfr"][d] / raw["sfr"][prev_y] - 1) * 100)
        for code, key in (("M1_YOY", "m1"), ("M2_YOY", "m2")):
            if d in yoy[key]:
                emit(code, d, yoy[key][d])
        if d in yoy["m1"] and d in yoy["m2"]:
            emit("M1_M2_GAP", d, yoy["m1"][d] - yoy["m2"][d])
        if d in raw["mc"] and d in raw["m2"] and raw["m2"][d]:
            emit("A_SHARE_MC_M2", d, raw["mc"][d] / raw["m2"][d] * 100)

        # C. 存贷款与储备
        if d in new_hh:
            emit("HH_DEPOSIT_NEW", d, new_hh[d])
        if d in new_hh and prev_y in new_hh:
            emit("HH_DEPOSIT_NEW_YOY", d, new_hh[d] - new_hh[prev_y])
        if d in new_loan:
            emit("RMB_LOAN_NEW", d, new_loan[d])
        if d in raw["gold"] and prev_m in raw["gold"]:
            emit("GOLD_NEW_TON", d, (raw["gold"][d] - raw["gold"][prev_m]) * GOLD_TON_PER_WAN_OZ)

    return pd.DataFrame(rows)
