"""数据看板数据装配（纯函数，不依赖 Web 框架）。

宏观 7 张图：
  1 GDP同比 + 进出口同比
  2 社零/CPI/PPI同比 + CPI-PPI剪刀差
  3 社融存量同比 + 人民币贷款当月新增（双轴）
  4 M1/M2同比 + M1-M2剪刀差
  5 M2、A股总市值（万亿）+ A股总市值/M2（%）+ 上证指数（点，月末收盘，三轴）
  6 居民存款同比多增
  7 黄金当月新增/吨

ETF 图：按标签（汇金/黄金/宽基）汇总当日较上一交易日的流通份额变化与申赎净流入，
并含黄金累计图、科创50 宽基排名与合计图（详见 build_etf_charts 文档）。

所有函数均返回前端可直接消费的 dict/list；数值为 float，日期为 YYYY-MM 或 YYYY-MM-DD。
"""
from __future__ import annotations

import math

import pandas as pd

from processors.derived import _shift_month

ETF_DASHBOARD_TAGS = ["汇金", "黄金", "宽基"]

# 宽基维度：六大宽基标签 → (图表ID短码, 对应指数代码)。
# 每类生成一组同构图：Top5 ETF 份额/申赎净流入排名 + 合计流通份额/申赎净流入
# （上格固定叠加该宽基指数自身日线；库中暂无该指数数据时退化为普通双轴图）。
# 注：000852 官方名称为「中证1000」，ETF 标签已与其统一为「中证1000」。
# 宽基图排列顺序：沪深300 → 科创50 → 创业板 → 上证50 → 中证A500 → 中证1000
BROAD_INDEX_SPECS = [
    ("沪深300", "hs300", "000300"),
    ("科创50", "kc50", "000688"),
    ("创业板", "cyb", "399006"),
    ("上证50", "sz50", "000016"),
    ("中证A500", "a500", "000510"),
    ("中证1000", "a1000", "000852"),
]

# 估值页：沪深300风险溢价率（FED 差值）曲线 + 历史均值 ±1σ/±2σ 通道 + 全历史百分位。
VALUATION_CHART_SPECS = [
    {
        "id": "val_fed_band", "title": "沪深300风险溢价率（FED 差值）· 均值 ±1σ/±2σ",
        "note": "直采韭圈儿股债性价比：1/沪深300 PE_TTM − 10年期国债收益率；"
                "均值与标准差为历史全样本滚动值，越高代表股票性价比越高",
        "layout": "valuation_band",
        "y_axes": [{"name": "风险溢价率 (%)", "position": "left"}],
    },
    {
        "id": "val_fed_percentile", "title": "沪深300风险溢价率 · 全历史百分位",
        "note": "百分位 = 当前风险溢价率低于历史所有交易日的比例（0~100%，越高越便宜）；"
                "以全部历史为基准，不随展示窗口变化",
        "layout": "valuation_percentile",
        "y_axes": [{"name": "历史百分位 (%)", "position": "left"}],
    },
]

# 美国宏观图：FRED 官方序列，独立于中国宏观 8 图，避免中国视图混入不同量纲数据。
US_MACRO_CHART_SPECS = [
    {
        "id": "us_gdp", "title": "图1：美国GDP环比折年率",
        "note": "FRED A191RL1Q225SBEA，实际GDP环比折年率（%），季度频率",
        "y_axes": [{"name": "环比折年率 (%)", "position": "left"}],
    },
    {
        "id": "us_prices", "title": "图2：CPI、核心CPI、PPI、非农就业人数",
        "note": "CPI/核心CPI/PPI为指数（左轴），非农就业人数为千人（右轴），均为 FRED 官方序列",
        "y_axes": [
            {"name": "指数", "position": "left"},
            {"name": "非农就业人数 (千人)", "position": "right"},
        ],
    },
    {
        "id": "us_rates", "title": "图3：联邦基金目标利率上下限",
        "note": "FRED DFEDTARU / DFEDTARL，单位 %；上下限构成目标区间",
        "y_axes": [{"name": "利率 (%)", "position": "left"}],
    },
]

MACRO_CHART_SPECS = [
    {
        "id": "gdp_trade", "title": "图1：GDP与进出口同比",
        "note": "GDP 为季度同比（右轴）、进出口为月度官方累计同比（左轴），均为百分数",
        "y_axes": [
            {"name": "进出口同比 (%)", "position": "left"},
            {"name": "GDP同比 (%)", "position": "right"},
        ],
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
    {
        "id": "loan_new_chart", "title": "图8：人民币贷款当月新增",
        "note": "贷款余额当月差分(亿元)，正值为当月新增、负值为当月回落",
        "y_axes": [{"name": "当月新增 (亿元)", "position": "left"}],
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


def _net_inflow_sum(group) -> float | None:
    """汇总真实净流入（份额变化×净值）；无该列时回退到规模变化以兼容旧数据。"""
    if "净流入(亿元)" in group.columns:
        series = group["净流入(亿元)"].dropna()
        return _clean_number(series.sum()) if not series.empty else None
    if "规模变化(亿元)" in group.columns:
        return _clean_number(group["规模变化(亿元)"].sum())
    return None


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


def _index_daily_window(index_df, low: str, high: str, code: str = "000001") -> list[list]:
    """指数日频收盘 [[YYYY-MM-DD, 点位], ...]，按 ETF 图展示窗口截取。

    low/high 来自 _etf_window：精确模式为 YYYY-MM-DD，月份模式 low 为 YYYY-MM、high 为空。
    code 为指数代码（000001/000016/000300/000510/000852/000688/399006 等），无该指数数据时返回空列表。
    """
    pairs = _pairs(index_df, "交易日期", "收盘价", code=str(code).strip().zfill(6),
                   code_col="指数代码")
    if not pairs:
        return []
    out = []
    for d, v in pairs:
        if low and len(low) == 10:
            if d < low:
                continue
        elif low and d[:7] < low:
            continue
        if high and d > high:
            continue
        out.append([d, v])
    return out


def _date_window(df, months: int, start: str | None, end: str | None,
                 date_col: str = "交易日期"):
    """按精确日期区间(start/end)或近 N 个月截取时间序列数据（ETF/交易共用）。

    - start/end 为 YYYY-MM-DD，任一给出则按精确区间过滤，优先于 months。
    - 否则按近 months 个月（基于数据最大交易日回推，0=全部）。
    - date_col 指定日期列名：ETF 为「交易日期」，交易宽表为 trade_date。

    返回 (过滤后df, 范围描述, low, high)：low/high 为精确起止日（精确模式），
    月份模式下 low 为起始月 YYYY-MM、high 为空。供细分图每日明细沿用同口径。
    """
    start = str(start).strip()[:10] if start else ""
    end = str(end).strip()[:10] if end else ""
    default_scope = "全部" if not months else "近 %d 个月" % months
    if df is None or getattr(df, "empty", True):
        return df, default_scope, start, end
    if date_col not in df.columns:
        return df, default_scope, start, end
    if start or end:
        dates = df[date_col].astype(str).str[:10]
        mask = pd.Series(True, index=df.index)
        if start:
            mask &= dates >= start
        if end:
            mask &= dates <= end
        return df[mask], f"{start or '最早'} ~ {end or '最新'}", start, end
    cutoff = ""
    if months:
        valid = sorted(d for d in df[date_col].astype(str).str[:10] if d >= "2000-01-01")
        if valid:
            cutoff = _shift_month(valid[-1][:7], -(int(months) - 1))
    if cutoff:
        df = df[df[date_col].astype(str).str[:7] >= cutoff]
    return df, default_scope, cutoff, ""


def _etf_window(df, months: int, start: str | None, end: str | None):
    """ETF 日期窗口：复用通用 _date_window，日期列为「交易日期」。"""
    return _date_window(df, months, start, end, "交易日期")


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


def _slice_range(series: list[list], start: str | None, end: str | None) -> list[list]:
    """按 YYYY-MM / YYYY-MM-DD 起止月份截取月度序列（含边界）。"""
    low = str(start)[:7] if start else ""
    high = str(end)[:7] if end else ""
    return [[d, v] for d, v in series
            if (not low or str(d)[:7] >= low) and (not high or str(d)[:7] <= high)]


def _chart(spec: dict, series: list[dict]) -> dict:
    return {
        "id": spec["id"],
        "title": spec["title"],
        "note": spec["note"],
        "y_axes": spec["y_axes"],
        "series": [s for s in series if s["data"]],
        **({"layout": spec["layout"]} if spec.get("layout") else {}),
    }


def build_macro_charts(derived_df: pd.DataFrame, macro_df: pd.DataFrame,
                       index_df: pd.DataFrame, months: int | None = 60,
                       start: str | None = None, end: str | None = None) -> list[dict]:
    """装配宏观 8 张图；months 控制展示最近 N 个月（None/0 为全部）。

    start/end 为 YYYY-MM-DD（或 YYYY-MM）绝对日期区间，与 ETF/交易看板口径一致；
    任一给出即优先于 months，避免"近3年/自定义"仍被默认近一年数据限制。
    """
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
    use_range = bool(start or end)
    cutoff = "" if use_range else _cutoff_date(list(raw.values()), months)

    def s(name, key, axis=0, chart_type="line"):
        data = _slice_range(raw[key], start, end) if use_range else _slice(raw[key], cutoff)
        return {
            "name": name, "data": data,
            "y_axis_index": axis, "type": chart_type,
        }

    grouped = {
        "gdp_trade": [s("GDP同比", "gdp_yoy", 1), s("进出口同比", "trade_yoy", 0)],
        "price": [s("社零同比", "retail"), s("CPI同比", "cpi"), s("PPI同比", "ppi"),
                  s("CPI-PPI剪刀差", "cpi_ppi")],
        "credit": [s("社融存量同比", "sfr"), s("人民币贷款当月新增", "loan_new", 1, "bar")],
        "money_gap": [s("M1同比增速", "m1"), s("M2同比增速", "m2"), s("M1-M2剪刀差", "m1_m2")],
        "m2_market": [s("M2", "m2_bal"), s("A股总市值", "mc_bal"),
                      s("A股总市值/M2", "mc_m2", 1), s("上证指数", "sh_index", 2)],
        "hh_deposit": [s("居民存款同比多增", "hh_new_yoy", 0, "bar")],
        "gold": [s("黄金当月新增", "gold_new", 0, "bar")],
        "loan_new_chart": [s("人民币贷款当月新增", "loan_new", 0, "bar")],
    }
    return [_chart(specs[spec_id], grouped[spec_id])
            for spec_id in [x["id"] for x in MACRO_CHART_SPECS]]


def build_us_macro_charts(macro_df: pd.DataFrame, months: int | None = 60,
                          start: str | None = None, end: str | None = None) -> list[dict]:
    """装配美国宏观 3 图；数据来自 FRED 采集链路，口径与中国宏观视图分离。

    months 控制展示最近 N 个月（None/0 为全部）；start/end 为绝对日期区间，
    任一给出即优先于 months，与中国宏观/ETF/交易看板的日期口径保持一致。
    """
    raw = {
        "us_gdp": _macro_value(macro_df, "US_GDP"),
        "us_cpi": _macro_value(macro_df, "US_CPI"),
        "us_core_cpi": _macro_value(macro_df, "US_CORE_CPI"),
        "us_ppi": _macro_value(macro_df, "US_PPI"),
        "us_nonfarm": _macro_value(macro_df, "US_NONFARM"),
        "us_rate_upper": _macro_value(macro_df, "US_RATE_UPPER"),
        "us_rate_lower": _macro_value(macro_df, "US_RATE_LOWER"),
    }
    use_range = bool(start or end)
    cutoff = "" if use_range else _cutoff_date(list(raw.values()), months)

    def s(name, key, axis=0, chart_type="line"):
        data = _slice_range(raw[key], start, end) if use_range else _slice(raw[key], cutoff)
        return {"name": name, "data": data, "y_axis_index": axis, "type": chart_type}

    grouped = {
        "us_gdp": [s("美国GDP环比折年率", "us_gdp", 0, "bar")],
        "us_prices": [s("美国CPI", "us_cpi"), s("美国核心CPI", "us_core_cpi"),
                      s("美国PPI", "us_ppi"), s("美国非农就业人数", "us_nonfarm", 1)],
        "us_rates": [s("联邦基金目标利率上限", "us_rate_upper"),
                     s("联邦基金目标利率下限", "us_rate_lower")],
    }
    specs = {spec["id"]: spec for spec in US_MACRO_CHART_SPECS}
    return [_chart(specs[spec_id], grouped[spec_id])
            for spec_id in [x["id"] for x in US_MACRO_CHART_SPECS]]


def build_etf_charts(etf_df: pd.DataFrame, tag_map: dict | None,
                     months: int = 12, start: str | None = None,
                     end: str | None = None, index_df: pd.DataFrame | None = None,
                     index_code: str = "000001") -> list[dict]:
    """按标签汇总 ETF 较上一交易日的份额变化/申赎净流入；months 控制展示最近 N 个月（0 为全部）。

    与宏观图保持一致的「近一年/近6个月/近3年/近5年/全部」口径。变化值已在
    read_etf_with_changes 中基于全量历史算好，这里只按日期截取展示窗口。
    start/end 为 YYYY-MM-DD 精确日期区间，优先于 months（任一给出即生效）。
    index_df 为指数日频数据，index_code 指定汇金/宽基主图叠加的指数代码（默认上证指数 000001）；
    科创50 合计图固定叠加科创50指数 000688。有该指数数据时前端渲染为上下双图联动。

    维度口径：
    - 汇金：主图（可叠指数）+ 各 ETF 净流入细分；不再提供每日变化累计图。
    - 黄金：主图 + 累计份额变化 / 累计申赎净流入 / 总份额整行图。
    - 宽基：主图（可叠指数）+ 六大宽基指数各一组同构图（Top5 排名 + 合计流通份额 /
      申赎净流入，叠自身指数），每组两图由前端并作一排。
    """
    tag_map = tag_map or {}
    months = max(0, int(months or 0))
    etf_df, scope, low, high = _etf_window(etf_df, months, start, end)
    charts = []

    def attach_overlay(chart: dict, code: str) -> str | None:
        """给图表叠加指数日线（上格），前端渲染为上下双图联动；无数据时保持原样。"""
        idx_data = _index_daily_window(index_df, low, high, code=code)
        if not idx_data:
            return None
        idx_name = "指数"
        if index_df is not None and not index_df.empty and "指数名称" in index_df.columns:
            names = index_df[index_df["指数代码"].astype(str).str.zfill(6)
                             == str(code).strip().zfill(6)]["指数名称"]
            if not names.empty:
                idx_name = str(names.iloc[0])
        chart["layout"] = "etf_overlay"
        chart["index_code"] = str(code).strip().zfill(6)
        chart["index_name"] = f"{idx_name} (点)"
        chart["index_data"] = idx_data
        chart["note"] += f"；上方叠加{idx_name}日线"
        return idx_name
    for tag in ETF_DASHBOARD_TAGS:
        codes = {str(c).zfill(6) for c, tags in tag_map.items()
                 if tag in (tags or [])}
        sub = pd.DataFrame()
        required = {"基金代码", "交易日期", "份额变化(亿份)", "规模变化(亿元)"}
        if etf_df is not None and not etf_df.empty and codes and required.issubset(etf_df.columns):
            sub = etf_df[etf_df["基金代码"].isin(codes)]

        # 按交易日汇总的时间序列（汇金原图 / 黄金图共用）
        if not sub.empty:
            agg = (sub.groupby("交易日期")[["份额变化(亿份)", "规模变化(亿元)"]]
                   .sum().sort_index())
        else:
            agg = pd.DataFrame(columns=["份额变化(亿份)", "规模变化(亿元)"])
        share_data = [[str(d), _clean_number(row["份额变化(亿份)"])]
                      for d, row in agg.iterrows()]
        share_data = [[d, v] for d, v in share_data if v is not None]
        # 资金流统一用申赎净流入（份额变化×当日净值，剔除市值波动）。
        # 按交易日汇总；复用 _net_inflow_sum 以兼容旧数据（无净流入列时回退规模变化）。
        flow_data: list[list] = []
        if "交易日期" in sub.columns:
            for d, grp in sub.groupby("交易日期", sort=True):
                v = _net_inflow_sum(grp)
                if v is not None:
                    flow_data.append([str(d)[:10], v])
        charts.append({
            "id": f"etf_{'huijin' if tag == '汇金' else 'gold' if tag == '黄金' else 'broad'}",
            "dim": "huijin" if tag == "汇金" else "gold" if tag == "黄金" else "broad",
            # 黄金维度的份额变化/申赎净流入图整行展示；汇金/宽基保持原布局
            "wide": tag == "黄金",
            "title": f"{tag}维度 · 流通份额变化 / 申赎净流入",
            "note": f"按标签「{tag}」汇总 {len(codes)} 只 ETF 较上一交易日的份额变化与申赎净流入；{scope}",
            "etf_count": len(codes),
            "codes": sorted(codes),
            "y_axes": [
                {"name": "份额变化 (亿份)", "position": "left"},
                {"name": "申赎净流入 (亿元)", "position": "right"},
            ],
            "series": [
                {"name": f"{tag}·流通份额变化", "data": share_data,
                 "y_axis_index": 0, "type": "bar"},
                {"name": f"{tag}·申赎净流入", "data": flow_data,
                 "y_axis_index": 1, "type": "bar"},
            ],
        })

        if tag == "汇金":
            # 汇金原图叠加所选指数日线：与 ETF 同窗口截取；前端渲染为上下双图联动。
            attach_overlay(charts[-1], index_code)
            # 汇金细分：按每只 ETF 累加所选区间内的真实净流入（份额变化×当日净值）降序。
            # 变化基准来自全量历史（见 read_etf_with_changes），这里只按窗口截取再累加。
            # 兼容旧数据：无「净流入(亿元)」列时回退到规模变化。
            rank: dict[str, dict] = {}
            if not sub.empty:
                for code, grp in sub.groupby("基金代码", sort=False):
                    name = str(grp["基金简称"].iloc[0]) if "基金简称" in grp.columns else str(code)
                    net = _net_inflow_sum(grp)
                    cap = _clean_number(grp["规模变化(亿元)"].sum())
                    share = _clean_number(grp["份额变化(亿份)"].sum())
                    rank[str(code).zfill(6)] = {"name": name, "net": net, "cap": cap, "share": share}
            ordered = sorted(rank.items(), key=lambda kv: (kv[1]["net"] is None, -(kv[1]["net"] or 0)))
            rank_data = [[v["name"], v["net"]] for _, v in ordered if v["net"] is not None]
            # 与 rank_data 顺序一致的代码/简称，供前端点击柱子定位到具体 ETF 拉每日明细
            rank_meta = [{"code": code, "name": v["name"]} for code, v in ordered
                         if v["net"] is not None]
            total_cap_change = _clean_number(sum(v["net"] for v in rank.values() if v["net"] is not None))
            total_share_change = _clean_number(sum(v["share"] for v in rank.values() if v["share"] is not None))
            charts.append({
                "id": "etf_huijin_detail",
                "dim": "huijin",
                "title": "汇金维度 · 各 ETF 净流入细分（申赎资金）",
                "note": f"净流入=份额变化×当日净值，剔除价格涨跌的市值波动（份额折算日不计）；"
                        f"按净流入降序，{scope} · 点击柱子查看每日明细",
                "layout": "etf_rank",
                "group": "huijin_detail",  # 前端紧贴在汇金原图下方，不另起一行
                "etf_count": len(codes),
                "codes": sorted(codes),
                "rank_meta": rank_meta,     # 与 series data 同序：{code,name}
                "detail_start": low,        # 与图表同口径的起始日期（精确模式 YYYY-MM-DD / 月份模式 YYYY-MM）
                "detail_end": high,         # 精确模式结束日期（YYYY-MM-DD），空=不限
                "range_label": scope,
                "total_cap_change": total_cap_change,   # 实为合计净流入（申赎资金）
                "total_share_change": total_share_change,
                "y_axes": [{"name": "净流入 (亿元)", "position": "left"}],
                "series": [
                    {"name": "净流入(申赎资金·亿元)", "data": rank_data,
                     "y_axis_index": 0, "type": "bar"},
                ],
            })

        if tag == "黄金":
            # 黄金维度整行图：累计份额变化 / 累计申赎净流入 / 总份额。
            # 前两条从区间起始日逐日累加「较上一交易日」变化，末值为区间净变化；
            # 总份额为逐交易日存量，来自 ETF 日频「份额(亿份)」合计。
            cum_share, acc = [], 0.0
            for d, v in share_data:
                acc += v
                cum_share.append([d, round(acc, 4)])
            cum_flow, acc = [], 0.0
            for d, v in flow_data:
                acc += v
                cum_flow.append([d, round(acc, 4)])
            total_share: dict[str, float] = {}
            if not sub.empty and {"交易日期", "份额(亿份)"}.issubset(sub.columns):
                for d, grp in sub.groupby("交易日期", sort=True):
                    s = _clean_number(pd.to_numeric(grp["份额(亿份)"], errors="coerce").sum())
                    if s is not None:
                        total_share[str(d)[:10]] = s
            charts.append({
                "id": "etf_gold_cumulative",
                "dim": "gold",
                "wide": True,
                "title": "黄金维度 · 流通累计份额变化 / 累计申赎净流入 / 总份额",
                "note": f"累计变化从所选区间起始日逐日累加（末值为区间净变化），总份额为逐交易日存量；{scope}",
                "etf_count": len(codes),
                "codes": sorted(codes),
                "y_axes": [
                    {"name": "累计份额变化 (亿份)", "position": "left"},
                    {"name": "累计申赎净流入 (亿元)", "position": "right"},
                    {"name": "总份额 (亿份)", "position": "right", "offset": 52},
                ],
                "series": [
                    {"name": "累计流通份额变化", "data": cum_share,
                     "y_axis_index": 0, "type": "line"},
                    {"name": "累计申赎净流入", "data": cum_flow,
                     "y_axis_index": 1, "type": "line"},
                    {"name": "总份额", "data": [[d, v] for d, v in sorted(total_share.items())],
                     "y_axis_index": 2, "type": "line"},
                ],
            })

        if tag == "宽基":
            # 宽基主图叠加所选指数日线（与汇金同口径，默认上证指数），上下双图联动。
            attach_overlay(charts[-1], index_code)
            # 六大宽基指数各生成一组同构图（与科创50口径一致）：
            #   Top5 ETF 份额变化/申赎净流入排名 + 合计流通份额/申赎净流入（叠自身指数）。
            # 同组两图使用同一 pair 键，由前端并作一排；科创50 图表 ID 保持不变。
            for idx_tag, slug, idx_code in BROAD_INDEX_SPECS:
                codes_k = {str(c).zfill(6) for c, tags in tag_map.items()
                           if idx_tag in (tags or [])}
                kc_sub = pd.DataFrame()
                if (etf_df is not None and not etf_df.empty and codes_k
                        and required.issubset(etf_df.columns)):
                    kc_sub = etf_df[etf_df["基金代码"].isin(codes_k)]

                # 图：各宽基 ETF 区间净流入排名（Top5），含份额变化与申赎净流入
                kc_rank: dict[str, dict] = {}
                if not kc_sub.empty:
                    for code, grp in kc_sub.groupby("基金代码", sort=False):
                        name = str(grp["基金简称"].iloc[0]) if "基金简称" in grp.columns else str(code)
                        net = _net_inflow_sum(grp)
                        share = _clean_number(grp["份额变化(亿份)"].sum())
                        kc_rank[str(code).zfill(6)] = {"name": name, "net": net, "share": share}
                ordered = sorted(kc_rank.items(),
                                 key=lambda kv: (kv[1]["net"] is None, -(kv[1]["net"] or 0)))
                top5 = ordered[:5]
                share_rank = [[v["name"], v["share"]] for _, v in top5 if v["share"] is not None]
                net_rank = [[v["name"], v["net"]] for _, v in top5 if v["net"] is not None]
                rank_meta = [{"code": code, "name": v["name"]} for code, v in top5]
                # 先构造柱状排名图，稍后在合计图之后 append，使其并排显示在右侧
                top5_chart = {
                    "id": f"etf_{slug}_top5",
                    "dim": "broad",
                    "pair": slug,  # 与本宽基合计图并作一排
                    "title": f"宽基 · Top5 {idx_tag} ETF 份额变化 / 申赎净流入",
                    "note": f"按「{idx_tag}」标签取数，按区间申赎净流入降序取前 5 只；"
                            f"份额变化=较上一交易日环比累加，净流入=份额变化×当日净值；{scope}",
                    "layout": "etf_rank",
                    "etf_count": len(codes_k),
                    "codes": sorted(codes_k),
                    "rank_meta": rank_meta,
                    "detail_start": low, "detail_end": high,
                    "range_label": scope,
                    "y_axes": [{"name": "份额变化 (亿份)", "position": "left"},
                               {"name": "申赎净流入 (亿元)", "position": "right"}],
                    "series": [
                        {"name": "份额变化(亿份)", "data": share_rank,
                         "y_axis_index": 0, "type": "bar"},
                        {"name": "申赎净流入(亿元)", "data": net_rank,
                         "y_axis_index": 1, "type": "bar"},
                    ],
                }

                # 图：该宽基 ETF 合计流通份额存量 / 申赎净流入（按交易日），双轴；
                # 上格叠加该宽基指数日线（固定 idx_code，不随用户选择的叠加指数变化）。
                stock_share: dict[str, float] = {}
                stock_flow: dict[str, float] = {}
                if not kc_sub.empty and {"交易日期", "份额(亿份)"}.issubset(kc_sub.columns):
                    for d, grp in kc_sub.groupby("交易日期", sort=True):
                        s = _clean_number(pd.to_numeric(grp["份额(亿份)"], errors="coerce").sum())
                        f = _net_inflow_sum(grp)
                        if s is not None:
                            stock_share[str(d)[:10]] = s
                        if f is not None:
                            stock_flow[str(d)[:10]] = f
                stock_chart = {
                    "id": f"etf_{slug}_stock",
                    "dim": "broad",
                    "pair": slug,  # 与 Top5 图并作一排
                    "title": f"宽基 · {idx_tag} ETF 合计流通份额 / 申赎净流入",
                    "note": f"按「{idx_tag}」标签全部 ETF 逐交易日合计：流通份额存量(亿份) 与"
                            f"申赎净流入(亿元)；{scope}",
                    "etf_count": len(codes_k),
                    "codes": sorted(codes_k),
                    "y_axes": [{"name": "流通份额 (亿份)", "position": "left"},
                               {"name": "申赎净流入 (亿元)", "position": "right"}],
                    "series": [
                        {"name": "合计流通份额(亿份)",
                         "data": [[d, v] for d, v in sorted(stock_share.items())],
                         "y_axis_index": 0, "type": "line"},
                        {"name": "合计申赎净流入(亿元)",
                         "data": [[d, v] for d, v in sorted(stock_flow.items())],
                         "y_axis_index": 1, "type": "line"},
                    ],
                }
                attach_overlay(stock_chart, idx_code)
                # 并排顺序：合计图在左、Top5 柱状排名图在右
                charts.append(stock_chart)
                charts.append(top5_chart)
    return charts


def build_etf_daily_detail(etf_df, tag_map, months: int = 12, date: str | None = None,
                           start: str | None = None, end: str | None = None,
                           view: str = "change") -> dict:
    """汇金维度按天明细。

    view='change'（默认，兼容原口径）：份额变化/申赎净流入明细。
      - date 为空：按交易日倒序返回每日汇总与区间合计。
      - date 给定：该交易日各汇金 ETF 的份额变化/申赎净流入，按净流入降序。
    view='total'（总份额口径）：每日汇金 ETF 份额存量合计。
      - date 为空：按交易日倒序返回每日份额合计、较上日变化、ETF 只数。
      - date 给定：该交易日各汇金 ETF 的份额存量/占比/较上日变化，按份额降序。
    口径与 build_etf_charts 一致：变化基准来自全量历史，此处仅按窗口截取。
    start/end 为 YYYY-MM-DD 精确日期区间，优先于 months（任一给出即生效）。
    """
    tag_map = tag_map or {}
    months = max(0, int(months or 0))
    codes = {str(c).zfill(6) for c, tags in tag_map.items() if "汇金" in (tags or [])}
    etf_df, scope, low, high = _etf_window(etf_df, months, start, end)

    if etf_df is None or etf_df.empty or not codes:
        if date:
            return {"date": str(date)[:10], "rows": [], "range": scope}
        if view == "total":
            return {"rows": [], "total": {"shares": 0.0, "days": 0}, "range": scope}
        return {"rows": [], "total": {"share": 0.0, "cap": 0.0,
                                       "net": 0.0, "days": 0}, "range": scope}

    sub = etf_df[etf_df["基金代码"].astype(str).str.zfill(6).isin(codes)].copy()

    # ---- 总份额口径：份额存量取 shares，变化取全量历史已算好的「份额变化(亿份)」列 ----
    # 注意：不能用窗口内 shift 求上一日——窗口首日没有前一日，会把存量误当成变化量。
    # read_etf_with_changes 返回的「份额变化(亿份)」基于全量历史计算，窗口截断不影响其正确性。
    if view == "total":
        if date:
            target = str(date)[:10]
            day = sub[sub["交易日期"].astype(str).str[:10] == target].copy()
            total_shares = _clean_number(day["份额(亿份)"].sum()) or 0.0
            rows = []
            for _, r in day.iterrows():
                shares = _clean_number(r.get("份额(亿份)"))
                if shares is None:
                    continue
                rows.append({
                    "code": str(r["基金代码"]).zfill(6),
                    "name": str(r.get("基金简称") or r["基金代码"]),
                    "shares": shares,
                    "pct": (shares / total_shares * 100) if total_shares else 0.0,
                    "change": _clean_number(r.get("份额变化(亿份)")),
                })
            rows.sort(key=lambda x: -x["shares"])
            return {"date": target, "rows": rows, "range": scope,
                    "total_shares": total_shares}
        # 每日汇总（窗口内）
        rows = []
        for d, grp in sub.groupby("交易日期", sort=False):
            ds = str(d)[:10]
            shares = _clean_number(grp["份额(亿份)"].sum()) or 0.0
            rows.append({
                "date": ds,
                "shares": shares,
                "change": _clean_number(grp["份额变化(亿份)"].sum()),
                "count": int(grp["基金代码"].nunique()),
            })
        rows.sort(key=lambda x: x["date"], reverse=True)
        latest = rows[0]["shares"] if rows else 0.0
        return {
            "rows": rows,
            "total": {"shares": latest, "days": len(rows)},
            "range": scope,
        }

    if date:
        target = str(date)[:10]
        day = sub[sub["交易日期"].astype(str).str[:10] == target]
        rows = []
        for _, r in day.iterrows():
            cap = _clean_number(r.get("规模变化(亿元)"))
            share = _clean_number(r.get("份额变化(亿份)"))
            net = _clean_number(r.get("净流入(亿元)"))
            if cap is None and share is None and net is None:
                continue
            rows.append({
                "code": str(r["基金代码"]).zfill(6),
                "name": str(r.get("基金简称") or r["基金代码"]),
                "share": share or 0.0,
                "cap": cap or 0.0,
                "net": net,
                "share_rate": _clean_number(r.get("份额变化率(%)")),
                "cap_rate": _clean_number(r.get("规模变化率(%)")),
            })
        # 有净流入列时按申赎资金降序；旧数据无该列时回退到规模变化降序
        rows.sort(key=lambda x: -(x["net"] if x["net"] is not None else x["cap"]))
        return {"date": target, "rows": rows, "range": scope}

    rows = []
    for d, grp in sub.groupby("交易日期", sort=False):
        share = _clean_number(grp["份额变化(亿份)"].sum()) or 0.0
        cap = _clean_number(grp["规模变化(亿元)"].sum()) or 0.0
        net = _net_inflow_sum(grp)
        rows.append({
            "date": str(d)[:10],
            "share": share,
            "cap": cap,
            "net": net,
            "count": int(grp["基金代码"].nunique()),
        })
    rows.sort(key=lambda x: x["date"], reverse=True)
    return {
        "rows": rows,
        "total": {
            "share": float(sum(r["share"] for r in rows)),
            "cap": float(sum(r["cap"] for r in rows)),
            "net": float(sum(r["net"] or 0 for r in rows)),
            "days": len(rows),
        },
        "range": scope,
    }


# ---------------- 交易看板（2 图；风险溢价率已迁至「估值」维度） ----------------
TRADE_CHART_SPECS = [
    {
        "id": "trade_market", "title": "图1：市场成交与集中度",
        "note": "上下两图联动滚动：上图 全A成交额(万亿元, 两位小数)/成交集中度前5%(%)；下图 涨跌家数(家)",
        "layout": "trade_market_split",
        "y_axes": [
            {"name": "成交额 (万亿元)", "position": "left"},
            {"name": "集中度 (%)", "position": "right", "offset": 0},
            {"name": "涨跌家数 (家)", "position": "left"},
        ],
    },
    {
        "id": "trade_margin", "title": "图2：两融核心指标",
        "note": "上下两图联动（共享交易日轴）：上图 两融余额(亿元)/有负债投资者数(人)；下图 维持担保比例/融资保证金比例/占流通市值比/融资买入额占比(%)",
        "layout": "trade_margin_split",
        "y_axes": [
            {"name": "亿元 / 人", "position": "left"},
            {"name": "比例 (%)", "position": "right", "offset": 0},
            {"name": "两融余额 (亿元)", "position": "right", "offset": 52},
        ],
    },
]


def build_valuation_charts(rp_df: pd.DataFrame, months: int = 12,
                           start: str | None = None, end: str | None = None) -> dict:
    """估值页：FED 风险溢价率 + 均值±1σ/±2σ 通道 + 全历史百分位 + 当前摘要卡片。

    rp_df 来自 trade_risk_premium_raw（原始直采表），含
    trade_date / risk_premium / avg / upper1std / lower1std / upper2std / lower2std / ratio / close。

    百分位口径：以「全部历史」risk_premium 为基准，统计 ≤ 当前值的交易日占比（0~100%），
    不随展示窗口变化；窗口只决定曲线画哪一段。数值越高代表股票相对债券越便宜。
    """
    specs = {s["id"]: s for s in VALUATION_CHART_SPECS}
    empty = {"charts": [_chart(specs[s["id"]], []) for s in VALUATION_CHART_SPECS],
             "summary": {}, "total_days": 0}

    if rp_df is None or rp_df.empty or "risk_premium" not in rp_df.columns:
        return empty

    df = rp_df.copy()
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    df = df[df["risk_premium"].notna()].sort_values("trade_date").reset_index(drop=True)
    if df.empty:
        return empty

    rp = pd.to_numeric(df["risk_premium"], errors="coerce")
    valid = rp.notna()
    df = df[valid].reset_index(drop=True)
    rp = pd.to_numeric(df["risk_premium"], errors="coerce")

    # 全历史百分位：≤ 当前值的样本数 / 全部样本数 × 100
    total = len(rp)
    pct = rp.rank(pct=True, method="average") * 100

    def col(name: str) -> list[float | None]:
        return [(_clean_number(v) if name in df.columns else None) for v in (
            df[name] if name in df.columns else [None] * total)]

    date_list = [str(d) for d in df["trade_date"]]
    rp_list = [_clean_number(v) for v in rp]
    pct_list = [_clean_number(v) for v in pct]
    avg_list = col("avg")
    up1_list = col("upper1std")
    low1_list = col("lower1std")
    up2_list = col("upper2std")
    low2_list = col("lower2std")

    # 展示窗口：精确 start/end 优先于 months；百分位已在全历史上算好，这里只筛展示点
    view_df, _, _, _ = _date_window(df, months, start, end, "trade_date")
    visible = set(view_df["trade_date"].astype(str).str[:10]) if not view_df.empty else set()

    def series(name, data, axis=0, typ="line"):
        return {"name": name,
                "data": [[d, v] for d, v in zip(date_list, data)
                         if v is not None and d in visible],
                "y_axis_index": axis, "type": typ}

    band_series = [
        series("风险溢价率", rp_list),
        series("历史均值", avg_list),
        series("均值+1σ", up1_list),
        series("均值−1σ", low1_list),
        series("均值+2σ", up2_list),
        series("均值−2σ", low2_list),
    ]
    # 通道上下沿数据供前端渲染 markArea 阴影带
    bands = {
        "std1": [[d, lo, hi] for d, lo, hi in zip(date_list, low1_list, up1_list)
                 if lo is not None and hi is not None and d in visible],
        "std2": [[d, lo, hi] for d, lo, hi in zip(date_list, low2_list, up2_list)
                 if lo is not None and hi is not None and d in visible],
    }
    pct_series = [series("历史百分位", pct_list)]

    band_chart = _chart(specs["val_fed_band"], band_series)
    band_chart["bands"] = bands
    pct_chart = _chart(specs["val_fed_percentile"], pct_series)

    latest = {
        "date": date_list[-1],
        "risk_premium": rp_list[-1],
        "percentile": pct_list[-1],
        "avg": avg_list[-1], "upper1std": up1_list[-1], "lower1std": low1_list[-1],
        "upper2std": up2_list[-1], "lower2std": low2_list[-1],
        "ratio": _clean_number(df["ratio"].iloc[-1]) if "ratio" in df.columns else None,
        "close": _clean_number(df["close"].iloc[-1]) if "close" in df.columns else None,
        "total_days": total,
    }
    return {"charts": [band_chart, pct_chart], "summary": latest, "total_days": total}


def build_trade_charts(trade_df: pd.DataFrame, days: int | None = None,
                       months: int = 12, start: str | None = None,
                       end: str | None = None) -> list[dict]:
    """装配交易 2 图，日期口径与 ETF 维度一致。

    - start/end（YYYY-MM-DD）任一给出即按精确区间过滤，优先于 months 与 days。
    - 否则按近 months 个月（基于最大交易日回推，0=全部）——与 ETF 完全同口径。
    - days 为旧参数「最近 N 个交易日」：仅显式传入（非 None）时按旧口径截取，
      保持旧调用兼容；新前端不传 days，一律用 months/start/end。
    """
    if trade_df is None or trade_df.empty:
        return [_chart(spec, []) for spec in TRADE_CHART_SPECS]

    df = trade_df.sort_values("trade_date").reset_index(drop=True)
    if start or end:
        df, _, _, _ = _date_window(df, 0, start, end, "trade_date")
    elif days is not None:
        if days > 0 and len(df) > days:
            df = df.tail(days).reset_index(drop=True)
    else:
        df, _, _, _ = _date_window(df, months, None, None, "trade_date")

    def pairs(col: str, transform=None) -> list[list]:
        out = []
        for _, r in df.iterrows():
            v = r.get(col)
            if v is None or pd.isna(v):
                continue
            num = float(v)
            out.append([str(r["trade_date"]), round(transform(num), 2) if transform else num])
        return out

    def series(name: str, col: str, axis: int, chart_type: str = "line", transform=None):
        return {"name": name, "data": pairs(col, transform),
                "y_axis_index": axis, "type": chart_type}

    grouped = {
        "trade_market": [
            # total_amount 存量为亿元；看板按万亿元展示并固定两位小数。
            series("全A成交额", "total_amount", 0, "bar", lambda v: v / 1e4),
            series("成交集中度(前5%)", "concentration5", 1),
            series("上涨家数", "up_count", 2),
            series("下跌家数", "down_count", 2),
        ],
        "trade_margin": [
            # y_axis_index: 0=上图左轴(两融余额亿元), 2=上图右轴(投资者数人),
            #              1=下图左轴(比例%), 3=下图右轴次坐标(占流通市值比/融资买入额占比%)
            series("平均维持担保比例", "margin_ratio", 1),
            series("融资保证金比例", "margin_financing_ratio", 1),
            series("融资余额占流通市值比", "financing_to_float_mv", 3),
            series("融资买入额占比", "financing_buy_pct", 3),
            series("两融余额", "margin_balance", 0),
            series("有负债投资者数", "debt_investors", 2),
        ],
    }
    return [_chart(spec, grouped[spec["id"]]) for spec in TRADE_CHART_SPECS]
