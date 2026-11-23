"""数据看板数据装配（纯函数，不依赖 Web 框架）。

宏观 7 张图：
  1 GDP同比 + 进出口同比
  2 社零/CPI/PPI同比 + CPI-PPI剪刀差
  3 社融存量同比 + 人民币贷款当月新增（双轴）
  4 M1/M2同比 + M1-M2剪刀差
  5 M2、A股总市值（万亿）+ A股总市值/M2（%）+ 上证指数（点，月末收盘，三轴）
  6 居民存款同比多增
  7 黄金当月新增/吨

ETF 2 张柱状图：按标签（汇金/黄金）汇总当日较上一交易日的流通份额变化与流通规模变化。

所有函数均返回前端可直接消费的 dict/list；数值为 float，日期为 YYYY-MM 或 YYYY-MM-DD。
"""
from __future__ import annotations

import math

import pandas as pd

from processors.derived import _shift_month

ETF_DASHBOARD_TAGS = ["汇金", "黄金"]

MACRO_CHART_SPECS = [
    {
        "id": "gdp_trade", "title": "图1：GDP与进出口同比",
        "note": "GDP 为季度同比、进出口为月度官方累计同比，均为百分数",
        "y_axes": [{"name": "同比 (%)", "position": "left"}],
    },
    {
        "id": "price", "title": "图2：社零同比、CPI同比、PPI同比、CPI-PPI剪刀差",
        "note": "剪刀差 = CPI同比 − PPI同比，单位百分点",
        "y_axes": [{"name": "% / 百分点", "position": "left"}],
    },
    {
        "id": "credit", "title": "图3：社融存量同比、人民币贷款当月新增",
        "note": "左轴为社融存量官方同比(%)，右轴为贷款余额差分(亿元)",
        "y_axes": [
            {"name": "同比 (%)", "position": "left"},
            {"name": "当月新增 (亿元)", "position": "right"},
        ],
    },
    {
        "id": "money_gap", "title": "图4：M1同比增速、M2同比增速、M1-M2剪刀差",
        "note": "剪刀差 = M1同比 − M2同比，单位百分点",
        "y_axes": [{"name": "% / 百分点", "position": "left"}],
    },
    {
        "id": "m2_market", "title": "图5：M2、A股总市值|A股总市值/M2、上证指数",
        "note": "左轴万亿元；右轴一为市值/M2(%)；右轴二为上证指数月末收盘(点)",
        "y_axes": [
            {"name": "万亿元", "position": "left"},
            {"name": "A股总市值/M2 (%)", "position": "right", "offset": 0},
            {"name": "上证指数 (点)", "position": "right", "offset": 52},
        ],
    },
    {
        "id": "hh_deposit", "title": "图6：居民存款同比多增",
        "note": "本月居民存款新增 − 去年同月新增，单位亿元",
        "y_axes": [{"name": "同比多增 (亿元)", "position": "left"}],
    },
    {
        "id": "gold", "title": "图7：黄金当月新增/吨",
        "note": "央行黄金储备(万盎司)月度差分换算为吨",
        "y_axes": [{"name": "吨", "position": "left"}],
    },
]


def _clean_number(value) -> float | None:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else round(out, 4)


def _pairs(df: pd.DataFrame, date_col: str, value_col: str,
           code: str | None = None, code_col: str | None = None,
           transform=None) -> list[list]:
    """从 DataFrame 抽取 [[日期, 数值], ...]，按日期升序，跳过空值。"""
    if df is None or df.empty or date_col not in df.columns or value_col not in df.columns:
        return []
    sub = df
    if code is not None and code_col and code_col in df.columns:
        sub = sub[sub[code_col] == code]
    if sub.empty:
        return []
    out: dict[str, float] = {}
    for date, value in zip(sub[date_col], sub[value_col]):
        # 宏观为 YYYY-MM（7 位），日频为 YYYY-MM-DD（10 位），统一保留原精度
        key = str(date)[:10]
        num = _clean_number(value)
        if num is None:
            continue
        out[key] = transform(num) if transform else num
    return [[k, out[k]] for k in sorted(out)]


def _derived_pairs(df: pd.DataFrame, code: str) -> list[list]:
    return _pairs(df, "date", "value", code=code, code_col="code")


def _macro_value(df: pd.DataFrame, code: str, transform=None) -> list[list]:
    return _pairs(df, "日期", "数值", code=code, code_col="指标代码", transform=transform)


def _macro_yoy(df: pd.DataFrame, code: str) -> list[list]:
    return _pairs(df, "日期", "同比(%)", code=code, code_col="指标代码")


def _index_monthly(df: pd.DataFrame, code: str = "000001") -> list[list]:
    """指数日频 -> 月末收盘；取每月最后一个交易日的收盘价。"""
    pairs = _pairs(df, "交易日期", "收盘价", code=code, code_col="指数代码")
    if not pairs:
        return []
    monthly = {d[:7]: v for d, v in pairs}
    return [[m, monthly[m]] for m in sorted(monthly)]


def _cutoff_date(series_list: list[list[list]], months: int | None) -> str:
    """根据所有序列的最大月份，回推 months 个月得到起始月；months<=0 表示全部。"""
    if not months or months <= 0:
        return ""
    last = ""
    for series in series_list:
        if series:
            last = max(last, series[-1][0][:7])
    if not last:
        return ""
    return _shift_month(last, -(int(months) - 1))


def _slice(series: list[list], cutoff: str) -> list[list]:
    if not cutoff:
        return series
    return [[d, v] for d, v in series if d[:7] >= cutoff]


def _chart(spec: dict, series: list[dict]) -> dict:
    return {
        "id": spec["id"],
        "title": spec["title"],
        "note": spec["note"],
        "y_axes": spec["y_axes"],
        "series": [s for s in series if s["data"]],
    }


def build_macro_charts(derived_df: pd.DataFrame, macro_df: pd.DataFrame,
                       index_df: pd.DataFrame, months: int | None = 60) -> list[dict]:
    """装配宏观 7 张图；months 控制展示最近 N 个月（None/0 为全部）。"""
    specs = {s["id"]: s for s in MACRO_CHART_SPECS}
    raw = {
        "gdp_yoy": _macro_yoy(macro_df, "CN_GDP"),
        "trade_yoy": _derived_pairs(derived_df, "TRADE_YOY"),
        "retail": _derived_pairs(derived_df, "RETAIL_SALES_YOY"),
        "cpi": _derived_pairs(derived_df, "CPI_YOY"),
        "ppi": _derived_pairs(derived_df, "PPI_YOY"),
        "cpi_ppi": _derived_pairs(derived_df, "CPI_PPI_GAP"),
        "sfr": _derived_pairs(derived_df, "SFR_STOCK_YOY"),
        "loan_new": _derived_pairs(derived_df, "RMB_LOAN_NEW"),
        "m1": _derived_pairs(derived_df, "M1_YOY"),
        "m2": _derived_pairs(derived_df, "M2_YOY"),
        "m1_m2": _derived_pairs(derived_df, "M1_M2_GAP"),
        "m2_bal": _macro_value(macro_df, "M2", lambda v: v / 10000),
        "mc_bal": _macro_value(macro_df, "A_SHARE_MC", lambda v: v / 10000),
        "mc_m2": _derived_pairs(derived_df, "A_SHARE_MC_M2"),
        "sh_index": _index_monthly(index_df, "000001"),
        "hh_new_yoy": _derived_pairs(derived_df, "HH_DEPOSIT_NEW_YOY"),
        "gold_new": _derived_pairs(derived_df, "GOLD_NEW_TON"),
    }
    cutoff = _cutoff_date(list(raw.values()), months)

    def s(name, key, axis=0, chart_type="line"):
        return {
            "name": name, "data": _slice(raw[key], cutoff),
            "y_axis_index": axis, "type": chart_type,
        }

    grouped = {
        "gdp_trade": [s("GDP同比", "gdp_yoy"), s("进出口同比", "trade_yoy")],
        "price": [s("社零同比", "retail"), s("CPI同比", "cpi"), s("PPI同比", "ppi"),
                  s("CPI-PPI剪刀差", "cpi_ppi")],
        "credit": [s("社融存量同比", "sfr"), s("人民币贷款当月新增", "loan_new", 1, "bar")],
        "money_gap": [s("M1同比增速", "m1"), s("M2同比增速", "m2"), s("M1-M2剪刀差", "m1_m2")],
        "m2_market": [s("M2", "m2_bal"), s("A股总市值", "mc_bal"),
                      s("A股总市值/M2", "mc_m2", 1), s("上证指数", "sh_index", 2)],
        "hh_deposit": [s("居民存款同比多增", "hh_new_yoy", 0, "bar")],
        "gold": [s("黄金当月新增", "gold_new", 0, "bar")],
    }
    return [_chart(specs[spec_id], grouped[spec_id])
            for spec_id in [x["id"] for x in MACRO_CHART_SPECS]]


def build_etf_charts(etf_df: pd.DataFrame, tag_map: dict | None,
                     days: int = 30) -> list[dict]:
    """按标签汇总 ETF 较上一交易日的份额/规模变化；days 控制展示最近 N 个交易日。"""
    tag_map = tag_map or {}
    days = max(1, int(days or 30))
    charts = []
    for tag in ETF_DASHBOARD_TAGS:
        codes = {str(c).zfill(6) for c, tags in tag_map.items()
                 if tag in (tags or [])}
        sub = pd.DataFrame()
        required = {"基金代码", "交易日期", "份额变化(亿份)", "规模变化(亿元)"}
        if etf_df is not None and not etf_df.empty and codes and required.issubset(etf_df.columns):
            sub = etf_df[etf_df["基金代码"].isin(codes)]
        if not sub.empty:
            agg = (sub.groupby("交易日期")[["份额变化(亿份)", "规模变化(亿元)"]]
                   .sum().sort_index().tail(days))
        else:
            agg = pd.DataFrame(columns=["份额变化(亿份)", "规模变化(亿元)"])
        share_data = [[str(d), _clean_number(row["份额变化(亿份)"])]
                      for d, row in agg.iterrows()]
        cap_data = [[str(d), _clean_number(row["规模变化(亿元)"])]
                    for d, row in agg.iterrows()]
        share_data = [[d, v] for d, v in share_data if v is not None]
        cap_data = [[d, v] for d, v in cap_data if v is not None]
        charts.append({
            "id": f"etf_{'huijin' if tag == '汇金' else 'gold'}",
            "title": f"{tag}维度 · 流通份额变化 / 流通规模变化",
            "note": f"按标签「{tag}」汇总 {len(codes)} 只 ETF 较上一交易日的变化；最近 {days} 个交易日",
            "etf_count": len(codes),
            "codes": sorted(codes),
            "y_axes": [
                {"name": "份额变化 (亿份)", "position": "left"},
                {"name": "规模变化 (亿元)", "position": "right"},
            ],
            "series": [
                {"name": f"{tag}·流通份额变化", "data": share_data,
                 "y_axis_index": 0, "type": "bar"},
                {"name": f"{tag}·流通规模变化", "data": cap_data,
                 "y_axis_index": 1, "type": "bar"},
            ],
        })
    return charts
