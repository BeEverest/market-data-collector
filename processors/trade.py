"""交易指标计算层（数据处理模块「交易」子 Tab）。

只读取 3 张交易原始表 + 政策常量，按确认口径派生 10 项指标，写入 trade_observation 宽表。
所有规则为纯函数（compute_trade），便于单测；recompute_trade 负责增量/全量重算与幂等落库。

口径（详见技术方案第四篇）：
  - 全A成交额 = 沪成交金额(亿元) + 深成交金额(元)÷1e8
  - 成交集中度前5% = 个股成交额降序前 5% 个股成交额之和 ÷ 全A个股成交额之和 ×100%
  - 两融余额 = 融资余额 + 融券余额（全市场，亿元）
  - 融资余额占流通市值比 = 融资余额 ÷（沪流通市值 + 深流通市值÷1e8）×100%
  - 融资买入额占比 = 融资买入额 ÷ 全A成交额 ×100%
  - 涨跌家数 = 快照涨跌幅 >0 / <0 / =0 的股票家数
  - 融资保证金比例 = 政策阶梯常量（按交易日匹配生效值）
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

import storage

logger = logging.getLogger(__name__)

GROUPS = {
    "A": "A. 市场成交",
    "B": "B. 两融核心",
}

INDICATORS: dict[str, dict] = {
    "TOTAL_AMOUNT": {"name": "全A成交额", "group": "A", "unit": "亿元",
                     "formula": "沪成交金额(亿元) + 深成交金额(元)÷1e8"},
    "CONCENTRATION5": {"name": "成交集中度(前5%)", "group": "A", "unit": "%",
                       "formula": "个股成交额降序前5%之和 ÷ 全A个股成交额之和 ×100%"},
    "MARGIN_RATIO": {"name": "平均维持担保比例", "group": "B", "unit": "%",
                     "formula": "两融账户平均维持担保比例，直取"},
    "MARGIN_FINANCING_RATIO": {"name": "融资保证金比例", "group": "B", "unit": "%",
                               "formula": "交易所政策下限，按交易日匹配生效常量"},
    "MARGIN_BALANCE": {"name": "两融余额", "group": "B", "unit": "亿元",
                       "formula": "融资余额 + 融券余额（全市场）"},
    "FINANCING_BALANCE": {"name": "融资余额", "group": "B", "unit": "亿元",
                          "formula": "两融账户融资余额，直取"},
    "SECURITIES_BALANCE": {"name": "融券余额", "group": "B", "unit": "亿元",
                           "formula": "两融账户融券余额，直取"},
    "FINANCING_TO_FLOAT_MV": {"name": "融资余额占流通市值比", "group": "B", "unit": "%",
                              "formula": "融资余额 ÷（沪流通市值 + 深流通市值÷1e8）×100%"},
    "FINANCING_BUY_PCT": {"name": "融资买入额占比", "group": "B", "unit": "%",
                          "formula": "融资买入额 ÷ 全A成交额 ×100%"},
    "DEBT_INVESTORS": {"name": "有负债投资者数", "group": "B", "unit": "人",
                       "formula": "两融账户有融资融券负债的投资者数量，直取"},
}

# 融资保证金比例政策阶梯常量（生效日 -> 比例%）。可被 config.trade_policy.margin_ratio 覆盖。
DEFAULT_MARGIN_POLICY = [
    {"effective": "2015-11-01", "value": 100.0},
    {"effective": "2023-08-28", "value": 80.0},
]

# compute_trade 输出的宽表列（与 storage.upsert_trade 对齐）
OUTPUT_COLUMNS = [
    "trade_date", "total_amount", "concentration5", "margin_ratio",
    "margin_financing_ratio", "margin_balance", "financing_balance",
    "securities_balance", "financing_to_float_mv", "financing_buy_pct",
    "debt_investors", "up_count", "down_count", "flat_count", "source_set",
]


def margin_policy_for(policy: list[dict] | None, trade_date: str) -> float | None:
    """按交易日返回生效的融资保证金比例（%）；取不晚于该日的最近生效值。"""
    steps = sorted(policy or DEFAULT_MARGIN_POLICY, key=lambda x: str(x.get("effective", "")))
    value = None
    for step in steps:
        eff = str(step.get("effective", ""))[:10]
        if eff and eff <= str(trade_date)[:10]:
            value = float(step.get("value"))
    return value


def compute_trade(exchange_row: dict | None, margin_row: dict | None,
                  snapshot_df: pd.DataFrame | None, policy: list[dict] | None,
                  trade_date: str) -> dict:
    """纯函数：输入某交易日三类原始数据 + 政策常量，输出该日 10 指标 + 涨跌家数。

    exchange_row / margin_row : 对应原始表的单行 dict（无数据传 None）
    snapshot_df               : 该交易日全A个股快照 DataFrame
    """
    ex = exchange_row or {}
    mg = margin_row or {}
    snap = snapshot_df if snapshot_df is not None else pd.DataFrame()

    def num(v):
        try:
            f = float(v)
            return None if pd.isna(f) else f
        except (TypeError, ValueError):
            return None

    # 指标1 全A成交额（亿元）：沪亿元 + 深元÷1e8
    sse_amt = num(ex.get("sse_amount"))
    szse_amt = num(ex.get("szse_amount"))
    total_amount = None
    if sse_amt is not None and szse_amt is not None:
        total_amount = round(sse_amt + szse_amt / 1e8, 4)

    # 指标2 成交集中度前5%
    concentration5 = None
    if not snap.empty and "amount" in snap.columns:
        amounts = pd.to_numeric(snap["amount"], errors="coerce").dropna()
        amounts = amounts[amounts > 0].sort_values(ascending=False)
        if len(amounts) > 0 and amounts.sum() > 0:
            n5 = max(1, int(len(amounts) * 0.05))
            concentration5 = round(amounts.head(n5).sum() / amounts.sum() * 100, 4)

    # 指标3/6/7/10 两融直取
    margin_ratio = num(mg.get("margin_ratio"))
    financing_balance = num(mg.get("financing_balance"))
    securities_balance = num(mg.get("securities_balance"))
    debt_investors = num(mg.get("debt_investors"))

    # 指标4 政策常量
    margin_financing_ratio = margin_policy_for(policy, trade_date)

    # 指标5 两融余额
    margin_balance = None
    if financing_balance is not None and securities_balance is not None:
        margin_balance = round(financing_balance + securities_balance, 4)

    # 指标8 融资余额占流通市值比
    financing_to_float_mv = None
    sse_fmv, szse_fmv = num(ex.get("sse_float_mv")), num(ex.get("szse_float_mv"))
    if financing_balance is not None and sse_fmv is not None and szse_fmv is not None:
        denom = sse_fmv + szse_fmv / 1e8
        if denom > 0:
            financing_to_float_mv = round(financing_balance / denom * 100, 4)

    # 指标9 融资买入额占比
    financing_buy_pct = None
    financing_buy = num(mg.get("financing_buy"))
    if financing_buy is not None and total_amount is not None and total_amount > 0:
        financing_buy_pct = round(financing_buy / total_amount * 100, 4)

    # 涨跌家数
    up = down = flat = None
    if not snap.empty and "pct_change" in snap.columns:
        pct = pd.to_numeric(snap["pct_change"], errors="coerce").dropna()
        up, down, flat = int((pct > 0).sum()), int((pct < 0).sum()), int((pct == 0).sum())

    sources = []
    if ex:
        sources.append("exchange")
    if mg:
        sources.append("margin")
    if not snap.empty:
        sources.append("snapshot")

    return {
        "trade_date": trade_date,
        "total_amount": total_amount,
        "concentration5": concentration5,
        "margin_ratio": margin_ratio,
        "margin_financing_ratio": margin_financing_ratio,
        "margin_balance": margin_balance,
        "financing_balance": financing_balance,
        "securities_balance": securities_balance,
        "financing_to_float_mv": financing_to_float_mv,
        "financing_buy_pct": financing_buy_pct,
        "debt_investors": debt_investors,
        "up_count": up, "down_count": down, "flat_count": flat,
        "source_set": "+".join(sources),
    }


def _load_policy(config: dict | None) -> list[dict]:
    policy = (config or {}).get("trade_policy", {}).get("margin_ratio")
    return policy if isinstance(policy, list) and policy else DEFAULT_MARGIN_POLICY


def recompute_trade(db_path: str, config: dict | None = None, run_id: str | None = None,
                    full: bool = False) -> int:
    """从原始表重算交易指标并幂等写 trade_observation。

    run_id : 定时增量时，只重算该 run_id 本次写入的交易所/两融/快照交易日；
             为空或 full=True 时扫描全部有原始数据的交易日。
    full   : True 时全量重算并替换整表（清理陈旧行）。
    返回本次写入的交易日行数。
    """
    import sqlite3
    policy = _load_policy(config)
    computed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 候选交易日 = 交易所 ∪ 两融 ∪ 快照
    dates: set[str] = set()
    for t in ("exchange", "margin"):
        dates.update(storage.trade_raw_dates(db_path, t))
    snap_dates = storage.trade_raw_dates(db_path, "snapshot")
    dates.update(snap_dates)

    if run_id and not full:
        # 增量：只取本次 run_id 写入的原始交易日
        with storage.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM ("
                " SELECT trade_date, run_id FROM trade_exchange_raw UNION ALL"
                " SELECT trade_date, run_id FROM trade_margin_raw UNION ALL"
                " SELECT trade_date, run_id FROM trade_snapshot_raw"
                ") WHERE run_id=?", (run_id,)
            ).fetchall()
        run_dates = {str(r["trade_date"])[:10] for r in rows}
        dates = dates & run_dates if run_dates else set()

    if not dates:
        logger.info("[交易计算] 无待重算交易日（run_id=%s）", run_id or "全部")
        return 0

    results = []
    for day in sorted(dates):
        ex_df = storage.read_trade_raw(db_path, "exchange", day, day)
        mg_df = storage.read_trade_raw(db_path, "margin", day, day)
        # 快照表代码主键含日期，按 trade_date 过滤已覆盖
        snap_df = storage.read_trade_raw(db_path, "snapshot", day, day)
        ex_row = ex_df.iloc[0].to_dict() if not ex_df.empty else None
        mg_row = mg_df.iloc[0].to_dict() if not mg_df.empty else None
        rec = compute_trade(ex_row, mg_row, snap_df, policy, day)
        rec["computed_at"] = computed_at
        results.append(rec)

    out = pd.DataFrame(results)
    if full:
        n = storage.replace_trade(db_path, out, run_id or "")
    else:
        n = storage.upsert_trade(db_path, out, run_id or "")
    logger.info("[交易计算] 重算完成：%d 个交易日（run_id=%s）", n, run_id or "全部")
    return n


def latest_snapshot(db_path: str) -> dict:
    """数据处理交易 Tab 展示：10 指标最新值 + 原始数据覆盖概况。"""
    df = storage.read_trade(db_path)
    latest = df.iloc[-1].to_dict() if not df.empty else {}
    # FastAPI 的 json 不接受 NaN/Inf，统一转 None
    import math
    latest = {k: (None if (isinstance(v, float) and not math.isfinite(v)) else v)
              for k, v in latest.items()}
    coverage = {
        "trade_days": len(df),
        "first_date": str(df.iloc[0]["trade_date"]) if not df.empty else "",
        "last_date": str(df.iloc[-1]["trade_date"]) if not df.empty else "",
        "raw_exchange": len(storage.read_trade_raw(db_path, "exchange")),
        "raw_margin": len(storage.read_trade_raw(db_path, "margin")),
        "raw_snapshot": len(storage.read_trade_raw(db_path, "snapshot")),
    }
    return {"latest": latest, "coverage": coverage,
            "indicators": [{"code": c, **m} for c, m in INDICATORS.items()]}
