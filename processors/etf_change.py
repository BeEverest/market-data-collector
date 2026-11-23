"""ETF 日频环比指标：较上一交易日的份额/规模变化与变化率。

按基金代码分组，以库中该代码的上一交易日记录为基准；首日无基准时四项为空。
"""
from __future__ import annotations

import pandas as pd

ETF_CHANGE_COLUMNS = [
    "份额变化(亿份)", "份额变化率(%)", "规模变化(亿元)", "规模变化率(%)", "净流入(亿元)",
]

# 份额折算识别阈值：单位净值单日涨跌幅超过该值视为折算（份额减半、净值翻倍等），
# 该日份额变化不代表真实申赎，净流入置空。
SPLIT_NAV_CHANGE_THRESHOLD = 0.20


def compute_etf_changes(df: pd.DataFrame) -> pd.DataFrame:
    """输入 ETF 观测 DataFrame，追加四项环比列，不改动原始行。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=list(getattr(df, "columns", [])) + ETF_CHANGE_COLUMNS)
    required = {"基金代码", "交易日期", "份额(亿份)", "规模(亿元)"}
    if not required.issubset(df.columns):
        return df.copy()

    out = df.copy()
    out["交易日期"] = out["交易日期"].astype(str)
    out = out.sort_values(["基金代码", "交易日期"]).reset_index(drop=True)
    grouped = out.groupby("基金代码", sort=False)
    prev_shares = grouped["份额(亿份)"].shift(1)
    prev_cap = grouped["规模(亿元)"].shift(1)

    shares = pd.to_numeric(out["份额(亿份)"], errors="coerce")
    cap = pd.to_numeric(out["规模(亿元)"], errors="coerce")
    out["份额变化(亿份)"] = (shares - prev_shares).round(4)
    out["规模变化(亿元)"] = (cap - prev_cap).round(4)
    out["份额变化率(%)"] = ((shares - prev_shares) / prev_shares * 100).round(4).where(prev_shares != 0)
    out["规模变化率(%)"] = ((cap - prev_cap) / prev_cap * 100).round(4).where(prev_cap != 0)

    # 净流入 = 份额变化(亿份) × 当日单位净值(元)，剔除价格涨跌带来的市值波动。
    # 单位：亿份 × 元/份 = 亿元。
    if "单位净值(元)" in out.columns:
        nav = pd.to_numeric(out["单位净值(元)"], errors="coerce")
        prev_nav = nav.groupby(out["基金代码"], sort=False).shift(1)
        net_inflow = ((shares - prev_shares) * nav).round(4)
        # 份额折算日：净值单日涨跌幅超阈值 → 份额变化非真实申赎，净流入置空
        nav_change = (nav - prev_nav) / prev_nav.replace(0, pd.NA)
        split_day = nav_change.abs() > SPLIT_NAV_CHANGE_THRESHOLD
        net_inflow = net_inflow.mask(split_day.fillna(False))
        out["净流入(亿元)"] = net_inflow
    else:
        out["净流入(亿元)"] = float("nan")
    return out
