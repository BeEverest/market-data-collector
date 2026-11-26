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

# 央行黄金储备曾于 2015-06 一次性披露口径变更（3389→5332 万盎司），
# 属于历史存量重述而非当月增持；差分超过该阈值视为口径跳变，不产出"当月新增"。
GOLD_MAX_MONTHLY_ADD_TON = 100.0

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


# 每项派生指标依赖的原始宏观代码（用于增量重算时判断"受哪些原始观测变化影响"）。
# 差分/同比/二阶差分即使跨月取值，其唯一数据来源仍是下列代码本身，故这里只列代码，
# 月份维度的依赖扩展（上月/去年同月/下月）由 incremental_targets 统一处理。
DERIVED_SOURCE_CODES: dict[str, set[str]] = {
    "TRADE_YOY": {"TRADE_CUM_TOTAL"},
    "RETAIL_SALES_YOY": {"RETAIL_SALES"},
    "CPI_YOY": {"CPI"},
    "PPI_YOY": {"PPI"},
    "CPI_PPI_GAP": {"CPI", "PPI"},
    "SFR_STOCK_YOY": {"SFR_STOCK"},
    "M1_YOY": {"M1"},
    "M2_YOY": {"M2"},
    "M1_M2_GAP": {"M1", "M2"},
    "A_SHARE_MC_M2": {"A_SHARE_MC", "M2"},
    "HH_DEPOSIT_NEW": {"HH_DEPOSIT"},
    "HH_DEPOSIT_NEW_YOY": {"HH_DEPOSIT"},
    "RMB_LOAN_NEW": {"RMB_LOAN"},
    "GOLD_NEW_TON": {"GOLD_RESERVE"},
}

# 原始观测在月份 M 发生变化时，可能受影响的派生月份偏移：
#   0  = 当月直接取数/作为差分的"本月"、同比的"今年"
#   +1 = 作为下月差分的"上月"分母（HH_DEPOSIT_NEW / RMB_LOAN_NEW / GOLD_NEW_TON）
#   +12 = 作为明年同月同比的"去年同月"分母（TRADE_YOY / SFR 派生 / A股M2…）
#   +13 = 二阶差分的"去年同月新增"分母（HH_DEPOSIT_NEW_YOY）
# 取保守并集，确保任何依赖方向的派生行都被覆盖；compute_derived 仍以全量历史为分母，
# 这里的偏移只决定"哪些 (指标, 月份) 需要重算并 upsert"。
INCREMENTAL_MONTH_OFFSETS = (0, 1, 12, 13)


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


def compute_derived(macro_df: pd.DataFrame, computed_at: str | None = None,
                    month: str | None = None, only_code: str | None = None,
                    target_codes: set[str] | None = None,
                    target_months: set[str] | None = None) -> pd.DataFrame:
    """输入宏观观测 DataFrame，输出派生指标 DataFrame（缺失值不产出行）。

    month 为 YYYY-MM 时仅输出该月结果；only_code 为指标代码时仅输出该指标结果。
    target_codes / target_months 为集合版本，供增量重算只产出受影响的 (指标, 月份)。
    注意：这些参数只过滤“输出”（并跳过无关月份的 emit），计算分母仍基于全量
    macro_df——差分/同比类指标依赖上月或去年同月的原始值，只取目标月会算错。
    """
    if macro_df is None or macro_df.empty:
        return pd.DataFrame()
    if "日期" not in macro_df.columns:
        return pd.DataFrame()
    stamp = computed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_dates = sorted({str(d)[:7] for d in macro_df["日期"] if str(d)[:7] >= "2000-01"})
    # 增量模式只遍历目标月份（分母仍来自全量 raw/yoy，不受影响）
    dates = ([d for d in all_dates if d in target_months]
             if target_months is not None else all_dates)

    # 进出口同比优先用官方累计（TRADE_CUM_TOTAL，仅 2023 年起）；
    # 官方累计缺失的早期月份，用月度进出口总额 TRADE_TOTAL 在年内逐月累加重建累计，
    # 使同比可回溯至月度数据起点（2015 年起，首个同比为 2016-01）。
    official_cum = _values(macro_df, "TRADE_CUM_TOTAL")
    trade_monthly = _values(macro_df, "TRADE_TOTAL")
    trade_cum = dict(official_cum)
    rebuilt_cum: dict[str, float] = {}
    for d in sorted(trade_monthly):
        prev_m = _shift_month(d, -1)
        rebuilt_cum[d] = (rebuilt_cum.get(prev_m, 0.0) if prev_m[:4] == d[:4] else 0.0) + trade_monthly[d]
    for d, v in rebuilt_cum.items():
        trade_cum.setdefault(d, v)

    raw = {
        "trade_cum": trade_cum,
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

    # 差分/二阶差分分母必须基于全量月份构建，不能受目标月份裁剪影响，
    # 否则 HH_DEPOSIT_NEW_YOY 等依赖去年同月新增的指标会丢失分母。
    new_hh = {d: raw["hh"][d] - raw["hh"][_shift_month(d, -1)]
              for d in all_dates if d in raw["hh"] and _shift_month(d, -1) in raw["hh"]}
    new_loan = {d: raw["loan"][d] - raw["loan"][_shift_month(d, -1)]
                for d in all_dates if d in raw["loan"] and _shift_month(d, -1) in raw["loan"]}

    rows: list[dict] = []

    def emit(code: str, date: str, value: float) -> None:
        if value is None or pd.isna(value):
            return
        if target_codes is not None and code not in target_codes:
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
            gold_add_ton = (raw["gold"][d] - raw["gold"][prev_m]) * GOLD_TON_PER_WAN_OZ
            # 过滤一次性口径重述（2015-06 等），避免把存量重估当成当月增持
            if abs(gold_add_ton) < GOLD_MAX_MONTHLY_ADD_TON:
                emit("GOLD_NEW_TON", d, gold_add_ton)

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    if month:
        out = out[out["date"] == str(month)[:7]]
    if only_code:
        out = out[out["code"] == str(only_code).strip()]
    return out.reset_index(drop=True)


def incremental_targets(changed_codes, changed_months) -> tuple[set[str], set[str]]:
    """把本次采集变更的原始观测映射为需要重算的派生 (指标, 月份)。

    changed_codes: 本次写入的原始宏观代码集合，如 {"M2", "RMB_LOAN"}。
    changed_months: 本次写入的原始观测月份集合，如 {"2026-10", "2026-11"}。

    返回 (target_codes, target_months)：
      - target_codes：依赖了任一 changed_code 的派生指标；
      - target_months：每个变更月份按 INCREMENTAL_MONTH_OFFSETS 扩展后的月份并集
        （覆盖差分/同比/二阶差分的跨月分母方向），只把这些月份传给 compute_derived。
    这样增量重算只覆盖受影响范围，避免对全量历史逐月重复计算。
    """
    changed_codes = {str(c).strip() for c in (changed_codes or set()) if str(c).strip()}
    changed_months = {str(m)[:7] for m in (changed_months or set()) if str(m)[:7] >= "2000-01"}
    if not changed_codes or not changed_months:
        return set(), set()
    target_codes = {
        derived_code for derived_code, srcs in DERIVED_SOURCE_CODES.items()
        if srcs & changed_codes
    }
    target_months: set[str] = set()
    for month in changed_months:
        for offset in INCREMENTAL_MONTH_OFFSETS:
            target_months.add(_shift_month(month, offset))
    return target_codes, target_months
