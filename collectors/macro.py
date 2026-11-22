"""
宏观数据采集：央行 / 统计局 / 交易所口径，月度频率。

指标组：
  货币与信贷：M0 / M1 / M2、居民储蓄存款、人民币存款、人民币贷款、社会融资规模存量
  外贸：出口额、进口额、进出口总额
  消费与物价：社会消费品零售总额、CPI、PPI
  景气与市场：制造业PMI、非制造业PMI、A股总市值、央行黄金储备

统一输出：指标代码 / 指标名称 / 日期 / 数值 / 单位 / 同比(%) / 环比(%) / 数据源 / 采集时间

运维监控：每个 akshare 接口调用经 _fetch 记录到 Monitor（成功/失败/失败原因/耗时/行数），
支持 only 参数只采集指定接口，用于失败接口重跑。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import pandas as pd

from monitor import describe_failure

logger = logging.getLogger(__name__)

OUTPUT_COLUMNS = [
    "指标代码", "指标名称", "日期", "数值", "单位",
    "同比(%)", "环比(%)", "数据源", "采集时间",
]

PBC = "中国人民银行（akshare 封装）"
NBS = "国家统计局（akshare 封装）"
EXCHANGE = "交易所/第三方（akshare 封装）"

# 接口键 -> 中文名称，用于监控报告
IFACE_NAMES = {
    "macro_china_money_supply": "货币供应量 M0/M1/M2",
    "macro_rmb_deposit": "人民币存款（含居民储蓄）",
    "macro_rmb_loan": "人民币贷款余额",
    "macro_china_shrzgm": "社会融资规模增量",
    "macro_china_hgjck": "海关进出口",
    "macro_china_consumer_goods_retail": "社会消费品零售总额",
    "macro_china_cpi": "CPI 居民消费价格指数",
    "macro_china_ppi": "PPI 工业生产者出厂价格指数",
    "macro_china_pmi": "PMI 采购经理指数",
    "macro_china_stock_market_cap": "A股总市值（沪深）",
    "macro_china_foreign_exchange_gold": "央行黄金储备",
}

# 权威指标目录：供 Web 精确采集下拉与代码→接口推导使用。
# 一个接口可产出多个指标（如 money_supply -> M0/M1/M2），按 code 过滤输出行。
CATALOG: list[dict] = [
    {"code": "M2", "name": "广义货币(M2)", "iface": "macro_china_money_supply", "unit": "亿元", "granularity": "月度", "source": PBC},
    {"code": "M1", "name": "狭义货币(M1)", "iface": "macro_china_money_supply", "unit": "亿元", "granularity": "月度", "source": PBC},
    {"code": "M0", "name": "流通中货币(M0)", "iface": "macro_china_money_supply", "unit": "亿元", "granularity": "月度", "source": PBC},
    {"code": "HH_DEPOSIT", "name": "居民储蓄存款余额", "iface": "macro_rmb_deposit", "unit": "亿元", "granularity": "月度", "source": PBC},
    {"code": "RMB_DEPOSIT", "name": "人民币存款余额", "iface": "macro_rmb_deposit", "unit": "亿元", "granularity": "月度", "source": PBC},
    {"code": "RMB_LOAN", "name": "人民币贷款余额", "iface": "macro_rmb_loan", "unit": "亿元", "granularity": "月度", "source": PBC},
    {"code": "SFR_STOCK", "name": "社会融资规模存量", "iface": "macro_china_shrzgm", "unit": "亿元", "granularity": "月度", "source": "央行锚点+社融增量插值"},
    {"code": "EXPORT", "name": "出口额", "iface": "macro_china_hgjck", "unit": "亿美元", "granularity": "月度", "source": "海关总署（akshare 封装）"},
    {"code": "IMPORT", "name": "进口额", "iface": "macro_china_hgjck", "unit": "亿美元", "granularity": "月度", "source": "海关总署（akshare 封装）"},
    {"code": "TRADE_TOTAL", "name": "进出口总额", "iface": "macro_china_hgjck", "unit": "亿美元", "granularity": "月度", "source": "海关总署（akshare 封装）"},
    {"code": "RETAIL_SALES", "name": "社会消费品零售总额(当月)", "iface": "macro_china_consumer_goods_retail", "unit": "亿元", "granularity": "月度", "source": NBS},
    {"code": "CPI", "name": "居民消费价格指数CPI", "iface": "macro_china_cpi", "unit": "上年同月=100", "granularity": "月度", "source": NBS},
    {"code": "PPI", "name": "工业生产者出厂价格指数PPI", "iface": "macro_china_ppi", "unit": "上年同月=100", "granularity": "月度", "source": NBS},
    {"code": "PMI_MFG", "name": "制造业PMI", "iface": "macro_china_pmi", "unit": "指数", "granularity": "月度", "source": NBS},
    {"code": "PMI_NONMFG", "name": "非制造业PMI", "iface": "macro_china_pmi", "unit": "指数", "granularity": "月度", "source": NBS},
    {"code": "A_SHARE_MC", "name": "A股总市值(沪深)", "iface": "macro_china_stock_market_cap", "unit": "亿元", "granularity": "月度", "source": EXCHANGE},
    {"code": "GOLD_RESERVE", "name": "央行黄金储备", "iface": "macro_china_foreign_exchange_gold", "unit": "万盎司", "granularity": "月度", "source": "国家外汇管理局（akshare 封装）"},
]

CODE_TO_IFACE: dict[str, str] = {item["code"]: item["iface"] for item in CATALOG}


def catalog() -> list[dict]:
    """返回宏观指标目录（含接口名，供监控展示）。"""
    return [dict(item) for item in CATALOG]


def _normalize_month(value) -> str:
    """把 2026年10月份 / 2026-10 / 2026.10 / 201610 统一成 2026-10。"""
    s = "" if value is None else str(value).strip()
    m = re.search(r"(\d{4})\D+(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})[-./](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return s


def _to_float(value):
    if value is None:
        return None
    s = str(value).strip().replace("%", "").replace(",", "")
    if s in ("", "-", "--", "nan", "None", "NaN"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _fetch(ak, fn_name: str, *, monitor=None, retries: int = 1,
           retry_interval: float = 0.0, only: set[str] | None = None,
           params: dict | None = None) -> pd.DataFrame:
    """调用一个宏观接口，记录每次调用状态并支持重试。

    only 非空时仅调用集合内的接口（用于失败接口重跑），其余返回空表且不记录。
    params 写入监控记录，标注本次调用的时间范围/指标代码。
    """
    if only is not None and fn_name not in only:
        return pd.DataFrame()

    started = time.time()
    last_exc: Exception | None = None
    attempts = 0
    for attempt in range(1, retries + 1):
        attempts = attempt
        try:
            df = getattr(ak, fn_name)()
            if df is not None and not df.empty:
                if monitor:
                    monitor.record(
                        category="宏观", interface_key=f"macro.{fn_name}",
                        interface_name=IFACE_NAMES.get(fn_name, fn_name),
                        status="success", attempts=attempt, rows=len(df),
                        params=params, duration_sec=time.time() - started,
                    )
                return df
            last_exc = None  # 空表属于无数据，非异常
            if attempt < retries:
                time.sleep(retry_interval)
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_interval)

    reason = describe_failure(last_exc, empty=last_exc is None)
    if monitor:
        monitor.record(
            category="宏观", interface_key=f"macro.{fn_name}",
            interface_name=IFACE_NAMES.get(fn_name, fn_name),
            status="failed", attempts=attempts, rows=0, error=reason,
            params=params, duration_sec=time.time() - started,
        )
    logger.error("宏观接口 %s 调用失败：%s", fn_name, reason)
    return pd.DataFrame()


def collect_macro(
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    monitor=None,
    only: set[str] | None = None,
    codes: set[str] | None = None,
    retries: int = 1,
    retry_interval: float = 0.0,
) -> pd.DataFrame:
    """采集月度宏观指标，返回统一 schema 的 DataFrame。

    only 为接口函数名集合时，仅采集这些接口（失败接口重跑用）。
    codes 为指标代码集合时，仅输出这些指标（精确采集用；仍按接口抓取再一次过滤）。
    end_date 为结束月份 YYYY-MM，含端点；与 start_date 共同限定采集区间。
    """
    import akshare as ak

    fetch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict] = []
    call_params = {
        "start": start_date or "", "end": end_date or "",
        "codes": ",".join(sorted(codes)) if codes else "",
    }

    def fetch(fn_name: str) -> pd.DataFrame:
        return _fetch(ak, fn_name, monitor=monitor, retries=retries,
                      retry_interval=retry_interval, only=only, params=call_params)

    def add(code, name, date, value, unit, yoy=None, mom=None, source=PBC):
        date = _normalize_month(date)
        if start_date and date < start_date:
            return
        if end_date and date > end_date:
            return
        if codes is not None and code not in codes:
            return
        num = _to_float(value)
        if num is None:  # 跳过数据源本身缺失的月份（如统计局1-2月社零合并统计）
            return
        rows.append({
            "指标代码": code,
            "指标名称": name,
            "日期": date,
            "数值": num,
            "单位": unit,
            "同比(%)": _to_float(yoy),
            "环比(%)": _to_float(mom),
            "数据源": source,
            "采集时间": fetch_time,
        })

    # ============ 央行：货币供应量 M0 / M1 / M2 ============
    ms = fetch("macro_china_money_supply")
    money_specs = [
        ("M2", "广义货币(M2)", "货币和准货币(M2)-数量(亿元)", "货币和准货币(M2)-同比增长", "货币和准货币(M2)-环比增长"),
        ("M1", "狭义货币(M1)", "货币(M1)-数量(亿元)", "货币(M1)-同比增长", "货币(M1)-环比增长"),
        ("M0", "流通中货币(M0)", "流通中的现金(M0)-数量(亿元)", "流通中的现金(M0)-同比增长", "流通中的现金(M0)-环比增长"),
    ]
    if not ms.empty:
        for code, name, val, yoy, mom in money_specs:
            if not {val, "月份"}.issubset(ms.columns):
                logger.warning("货币供应量接口缺少列，跳过 %s", code)
                continue
            for _, r in ms.iterrows():
                add(code, name, r["月份"], r[val], "亿元", r.get(yoy), r.get(mom), PBC)

    # ============ 央行：存贷款 ============
    dep = fetch("macro_rmb_deposit")
    if not dep.empty:
        for code, name, val, yoy, mom in [
            ("HH_DEPOSIT", "居民储蓄存款余额", "新增储蓄存款-数量", "新增储蓄存款-同比", "新增储蓄存款-环比"),
            ("RMB_DEPOSIT", "人民币存款余额", "新增存款-数量", "新增存款-同比", "新增存款-环比"),
        ]:
            if val not in dep.columns:
                continue
            for _, r in dep.iterrows():
                add(code, name, r["月份"], r[val], "亿元", r.get(yoy), r.get(mom), PBC)

    loan = fetch("macro_rmb_loan")
    if not loan.empty and "累计人民币贷款-总额" in loan.columns:
        for _, r in loan.iterrows():
            add("RMB_LOAN", "人民币贷款余额", r["月份"], r["累计人民币贷款-总额"], "亿元",
                r.get("累计人民币贷款-同比"), source=PBC)

    # ============ 央行：社会融资规模存量（锚点 + 增量插值） ============
    SFR_ANCHORS = {
        "2024-12": 408.30, "2025-03": 422.96, "2025-05": 426.16,
        "2025-08": 433.66, "2025-12": 442.12, "2026-01": 449.11,
        "2026-07": 463.27, "2026-10": 469.16,
    }
    sfr = fetch("macro_china_shrzgm")
    if not sfr.empty and "社会融资规模增量" in sfr.columns:
        sfr = sfr.copy()
        sfr["_月"] = sfr["月份"].astype(str).str[:4] + "-" + sfr["月份"].astype(str).str[4:6]
        sfr = sfr.sort_values("_月").reset_index(drop=True)
        inc_by_month = dict(zip(sfr["_月"], sfr["社会融资规模增量"].map(_to_float)))

        anchor_months = sorted(SFR_ANCHORS)
        for i in range(len(anchor_months) - 1):
            m0, m1 = anchor_months[i], anchor_months[i + 1]
            v0, v1 = SFR_ANCHORS[m0], SFR_ANCHORS[m1]
            months_between = [m for m in sfr["_月"] if m0 < m < m1]
            total_inc = sum(inc_by_month.get(m) or 0 for m in months_between + [m1])
            inc_map = {m: (inc_by_month.get(m) or 0) for m in months_between}
            running = v0
            for m in months_between:
                if total_inc:
                    running += (v1 - v0) * (inc_map[m] / total_inc)
                add("SFR_STOCK", "社会融资规模存量", m, running * 10000, "亿元",
                    source="央行公布值锚点 + 社融增量插值")
        for m, v in SFR_ANCHORS.items():
            add("SFR_STOCK", "社会融资规模存量", m, v * 10000, "亿元",
                source="中国人民银行（金融统计数据报告）")

    # ============ 海关：进出口 ============
    trade = fetch("macro_china_hgjck")
    if not trade.empty:
        need = {"月份", "当月出口额-金额", "当月进口额-金额"}
        if need.issubset(trade.columns):
            for _, r in trade.iterrows():
                exp = _to_float(r["当月出口额-金额"])
                imp = _to_float(r["当月进口额-金额"])
                if exp is None or imp is None:
                    continue
                add("EXPORT", "出口额", r["月份"], exp / 1e5, "亿美元",
                    r.get("当月出口额-同比增长"), r.get("当月出口额-环比增长"), "海关总署（akshare 封装）")
                add("IMPORT", "进口额", r["月份"], imp / 1e5, "亿美元",
                    r.get("当月进口额-同比增长"), r.get("当月进口额-环比增长"), "海关总署（akshare 封装）")
                add("TRADE_TOTAL", "进出口总额", r["月份"], (exp + imp) / 1e5, "亿美元",
                    source="海关总署（akshare 封装）")
        else:
            logger.warning("海关接口缺少列：%s", list(trade.columns))

    # ============ 统计局：社会消费品零售总额 ============
    retail = fetch("macro_china_consumer_goods_retail")
    if not retail.empty and "当月" in retail.columns:
        for _, r in retail.iterrows():
            add("RETAIL_SALES", "社会消费品零售总额(当月)", r["月份"], r["当月"], "亿元",
                r.get("同比增长"), r.get("环比增长"), NBS)

    # ============ 统计局：CPI / PPI ============
    cpi = fetch("macro_china_cpi")
    if not cpi.empty and "全国-当月" in cpi.columns:
        for _, r in cpi.iterrows():
            add("CPI", "居民消费价格指数CPI", r["月份"], r["全国-当月"], "上年同月=100",
                r.get("全国-同比增长"), r.get("全国-环比增长"), NBS)

    ppi = fetch("macro_china_ppi")
    if not ppi.empty and "当月" in ppi.columns:
        for _, r in ppi.iterrows():
            add("PPI", "工业生产者出厂价格指数PPI", r["月份"], r["当月"], "上年同月=100",
                r.get("当月同比增长"), source=NBS)

    # ============ 统计局：PMI ============
    pmi = fetch("macro_china_pmi")
    if not pmi.empty:
        for code, name, val in [
            ("PMI_MFG", "制造业PMI", "制造业-指数"),
            ("PMI_NONMFG", "非制造业PMI", "非制造业-指数"),
        ]:
            if val not in pmi.columns:
                continue
            for _, r in pmi.iterrows():
                add(code, name, r["月份"], r[val], "指数", source=NBS)

    # ============ A股总市值（沪市 + 深市） ============
    cap = fetch("macro_china_stock_market_cap")
    if not cap.empty and {"市价总值-上海", "市价总值-深圳"}.issubset(cap.columns):
        for _, r in cap.iterrows():
            sh = _to_float(r["市价总值-上海"])
            sz = _to_float(r["市价总值-深圳"])
            if sh is None or sz is None:
                continue
            add("A_SHARE_MC", "A股总市值(沪深)", r["数据日期"], sh + sz, "亿元", source=EXCHANGE)

    # ============ 央行黄金储备 ============
    gold = fetch("macro_china_foreign_exchange_gold")
    if not gold.empty and "黄金储备" in gold.columns:
        for _, r in gold.iterrows():
            add("GOLD_RESERVE", "央行黄金储备", r["统计时间"], r["黄金储备"], "万盎司",
                source="国家外汇管理局（akshare 封装）")

    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if not df.empty:
        df = df.sort_values(["指标代码", "日期"], ascending=[True, False]).reset_index(drop=True)
    logger.info("宏观数据采集完成：%d 行，%d 个指标",
                len(df), df["指标代码"].nunique() if not df.empty else 0)
    return df
