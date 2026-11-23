"""
宏观数据采集：央行 / 统计局 / 交易所 / FRED / 美国经济日历口径，月度与季度混合频率。

指标组：
  货币与信贷：M0 / M1 / M2、居民储蓄存款、人民币存款、人民币贷款、社会融资规模存量
  外贸：出口额、进口额、进出口总额
  消费与物价：社会消费品零售总额、CPI、PPI
  景气与市场：制造业PMI、非制造业PMI、A股总市值、央行黄金储备
  增长与美国：中国GDP、美国GDP、美国CPI/核心CPI/PPI、非农新增就业、联邦基金目标利率上下限

统一输出：指标代码 / 指标名称 / 日期 / 数值 / 单位 / 同比(%) / 环比(%) / 数据源 / 采集时间

运维监控：每个 akshare 接口调用经 _fetch 记录到 Monitor（成功/失败/失败原因/耗时/行数），
支持 only 参数只采集指定接口，用于失败接口重跑。
"""
from __future__ import annotations

import io
import logging
import re
import time
from datetime import datetime

import pandas as pd
import requests

from monitor import describe_failure, describe_http_failure

logger = logging.getLogger(__name__)

OUTPUT_COLUMNS = [
    "指标代码", "指标名称", "日期", "数值", "单位",
    "同比(%)", "环比(%)", "数据源", "采集时间",
]

PBC = "中国人民银行（akshare 封装）"
NBS = "国家统计局（akshare 封装）"
EXCHANGE = "交易所/第三方（akshare 封装）"
FRED = "FRED（美联储 / BLS / BEA）"

# 社融存量改由央行《社会融资规模存量统计表》官方接口提供，不再走锚点+增量插值
SFR_STOCK_IFACE = "sfr_stock_official"
SFR_STOCK_SOURCE = "中国人民银行（调查统计司·社会融资规模存量统计表）"

# 接口键 -> 中文名称，用于监控报告
IFACE_NAMES = {
    "macro_china_money_supply": "货币供应量 M0/M1/M2",
    "macro_rmb_deposit": "人民币存款（含居民储蓄）",
    "macro_rmb_loan": "人民币贷款余额",
    "macro_china_shrzgm": "社会融资规模增量（旧插值口径，已停用）",
    SFR_STOCK_IFACE: "社会融资规模存量（央行官方统计表）",
    "macro_china_hgjck": "海关进出口",
    "macro_china_consumer_goods_retail": "社会消费品零售总额",
    "macro_china_cpi": "CPI 居民消费价格指数",
    "macro_china_ppi": "PPI 工业生产者出厂价格指数",
    "macro_china_pmi": "PMI 采购经理指数",
    "macro_china_stock_market_cap": "A股总市值（沪深）",
    "macro_china_foreign_exchange_gold": "央行黄金储备",
    "macro_china_gdp": "中国GDP（季度绝对值）",
    "fred_A191RL1Q225SBEA": "美国GDP环比折年率(FRED)",
    "fred_CPIAUCSL": "美国CPI指数(FRED)",
    "fred_CPILFESL": "美国核心CPI指数(FRED)",
    "fred_PPIACO": "美国PPI指数(FRED)",
    "fred_PAYEMS": "美国非农就业人数(FRED)",
    "fred_DFEDTARU": "美国联邦基金目标利率上限(FRED)",
    "fred_DFEDTARL": "美国联邦基金目标利率下限(FRED)",
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
    {"code": "SFR_STOCK", "name": "社会融资规模存量", "iface": SFR_STOCK_IFACE, "unit": "亿元", "granularity": "月度", "source": SFR_STOCK_SOURCE},
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
    {"code": "CN_GDP", "name": "中国GDP(季度绝对值)", "iface": "macro_china_gdp", "unit": "亿元", "granularity": "季度", "source": NBS},
    {"code": "US_GDP", "name": "美国GDP环比折年率", "iface": "fred_A191RL1Q225SBEA", "unit": "%", "granularity": "季度", "source": FRED},
    {"code": "US_CPI", "name": "美国CPI指数", "iface": "fred_CPIAUCSL", "unit": "1982-84=100", "granularity": "月度", "source": FRED},
    {"code": "US_CORE_CPI", "name": "美国核心CPI指数", "iface": "fred_CPILFESL", "unit": "1982-84=100", "granularity": "月度", "source": FRED},
    {"code": "US_PPI", "name": "美国PPI指数", "iface": "fred_PPIACO", "unit": "指数", "granularity": "月度", "source": FRED},
    {"code": "US_NONFARM", "name": "美国非农新增就业", "iface": "fred_PAYEMS", "unit": "千人", "granularity": "月度", "source": FRED},
    {"code": "US_RATE_UPPER", "name": "美国联邦基金目标利率上限", "iface": "fred_DFEDTARU", "unit": "%", "granularity": "月度", "source": FRED},
    {"code": "US_RATE_LOWER", "name": "美国联邦基金目标利率下限", "iface": "fred_DFEDTARL", "unit": "%", "granularity": "月度", "source": FRED},
]

CODE_TO_IFACE: dict[str, str] = {item["code"]: item["iface"] for item in CATALOG}


def catalog() -> list[dict]:
    """返回宏观指标目录（含接口名，供监控展示）。"""
    return [dict(item) for item in CATALOG]


# FRED 公共 CSV 接口（无需 API Key），用于补齐 akshare 美国宏观数据更新滞后的问题
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_SERIES = {
    "fred_A191RL1Q225SBEA": "A191RL1Q225SBEA",  # 美国实际GDP环比折年率
    "fred_CPIAUCSL": "CPIAUCSL",                # 美国CPI指数（季调）
    "fred_CPILFESL": "CPILFESL",                # 美国核心CPI指数（季调）
    "fred_PPIACO": "PPIACO",                    # 美国PPI指数（全部商品）
    "fred_PAYEMS": "PAYEMS",                    # 美国非农就业人数（千人）
    "fred_DFEDTARU": "DFEDTARU",                # 美国联邦基金目标利率上限
    "fred_DFEDTARL": "DFEDTARL",                # 美国联邦基金目标利率下限
}


def _fred_frame(series_id: str) -> pd.DataFrame:
    resp = requests.get(FRED_CSV_URL, params={"id": series_id}, timeout=20)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    if df.shape[1] < 2:
        return pd.DataFrame(columns=["日期", "数值"])
    df = df.iloc[:, :2].copy()
    df.columns = ["日期", "数值"]
    df["数值"] = pd.to_numeric(df["数值"], errors="coerce")
    return df


CUSTOM_FETCHERS = {
    name: (lambda sid=series_id: _fred_frame(sid))
    for name, series_id in FRED_SERIES.items()
}


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


def _normalize_quarter(value) -> str:
    """把 2026年第1-3季度 / 2025年第1-4季度 映射为季末月 YYYY-MM。"""
    s = "" if value is None else str(value).strip()
    m = re.search(r"(\d{4})\s*年第\s*1-(\d)\s*季度", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)) * 3:02d}"
    m = re.search(r"(\d{4})\s*年第\s*(\d)\s*季度", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)) * 3:02d}"
    return _normalize_month(s)


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

    # 完整调用参数：函数级时间范围 + 具体接口标识 + 数据源 + FRED 序列 ID
    call_params: dict = dict(params or {})
    call_params.setdefault("interface", fn_name)
    is_fred = fn_name.startswith("fred_")
    call_params.setdefault("source", "FRED CSV" if is_fred else "akshare")
    if is_fred and fn_name in FRED_SERIES:
        call_params.setdefault("series_id", FRED_SERIES[fn_name])
    if is_fred:
        call_params.setdefault("url", FRED_CSV_URL)
    # 受影响标的：宏观接口对应指标名 + FRED 序列 ID，便于失败明细定位
    target_label = IFACE_NAMES.get(fn_name, fn_name)
    series_id = call_params.get("series_id", "")
    call_params["受影响标的"] = f"{target_label}({series_id})" if series_id else target_label

    started = time.time()
    last_exc: Exception | None = None
    attempts = 0
    for attempt in range(1, retries + 1):
        attempts = attempt
        try:
            fetcher = CUSTOM_FETCHERS.get(fn_name)
            df = fetcher() if fetcher else getattr(ak, fn_name)()
            if df is not None and not df.empty:
                if monitor:
                    monitor.record(
                        category="宏观", interface_key=f"macro.{fn_name}",
                        interface_name=IFACE_NAMES.get(fn_name, fn_name),
                        status="success", attempts=attempt, rows=len(df),
                        params=call_params, duration_sec=time.time() - started,
                    )
                return df
            last_exc = None  # 空表属于无数据，非异常
            if attempt < retries:
                time.sleep(retry_interval)
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_interval)

    if last_exc is not None:
        resp = getattr(last_exc, "response", None)
        snippet = ""
        if resp is not None:
            try:
                snippet = " ".join(str(getattr(resp, "text", "") or "").split())[:200]
            except Exception:
                snippet = ""
        reason = describe_http_failure(
            last_exc, url=FRED_CSV_URL if is_fred else "", response_text=snippet,
            extra=f"受影响标的={call_params['受影响标的']}",
        )
    else:
        reason = describe_failure(
            None, empty=True, extra=f"受影响标的={call_params['受影响标的']}"
        )
    if monitor:
        monitor.record(
            category="宏观", interface_key=f"macro.{fn_name}",
            interface_name=IFACE_NAMES.get(fn_name, fn_name),
            status="failed", attempts=attempts, rows=0, error=reason,
            params=call_params, duration_sec=time.time() - started,
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
    sa_mom_map: dict[str, float] | None = None,
    sa_mom_url: str = "",
    sfr_stock_map: dict[str, tuple[float, float]] | None = None,
    sfr_stock_url: str = "",
) -> pd.DataFrame:
    """采集月度宏观指标，返回统一 schema 的 DataFrame。

    only 为接口函数名集合时，仅采集这些接口（失败接口重跑用）。
    codes 为指标代码集合时，仅输出这些指标（精确采集用；仍按接口抓取再一次过滤）。
    start_date / end_date 支持 YYYY-MM 或 YYYY-MM-DD，内部统一按月比较（含端点）。

    sa_mom_map：社零「季调后环比」{YYYY-MM: %}；传入时直接用于覆盖，
        为 None 时自动抓取统计局最新一期（失败则降级保留接口原值）。
        传空 dict {} 可显式禁用覆盖（测试/离线用）。
    sfr_stock_map：社融存量 {YYYY-MM: (存量万亿, 官方增速%)}；传入时直接写入，
        为 None 时自动抓取央行官方存量统计表；抓取失败或传空 dict {} 时本次不产出该指标，
        不再回退到任何插值/估算口径。
    """
    import akshare as ak

    start_date = _normalize_month(start_date) if start_date else None
    end_date = _normalize_month(end_date) if end_date else None
    fetch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict] = []
    call_params = {
        "start": start_date or "", "end": end_date or "",
        "codes": ",".join(sorted(codes)) if codes else "",
    }
    total_ifaces = len(only) if only is not None else len(IFACE_NAMES)
    fetch_count = 0

    def fetch(fn_name: str) -> pd.DataFrame:
        nonlocal fetch_count
        fetch_count += 1
        df = _fetch(ak, fn_name, monitor=monitor, retries=retries,
                    retry_interval=retry_interval, only=only, params=call_params)
        label = IFACE_NAMES.get(fn_name, fn_name)
        if only is not None and fn_name not in only:
            logger.info("  │ 接口 %d/%d %s：跳过（不在本次重跑范围）",
                        fetch_count, total_ifaces, label)
        elif df.empty:
            logger.warning("  │ 接口 %d/%d %s：无数据",
                           fetch_count, total_ifaces, label)
        else:
            logger.info("  │ 接口 %d/%d %s：返回 %d 行原始记录",
                        fetch_count, total_ifaces, label, len(df))
        return df

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

    def add_fred_level(code, name, fn, unit, source):
        """FRED 水平序列：同时按 12 期/1 期计算同比、环比（%）。"""
        d = fetch(fn)
        if d.empty or not {"日期", "数值"}.issubset(d.columns):
            return
        d = d.copy().sort_values("日期").reset_index(drop=True)
        vals = pd.to_numeric(d["数值"], errors="coerce")
        keys = d["日期"].astype(str).str[:7].tolist()
        val_map = {k: v for k, v in zip(keys, vals) if pd.notna(v)}

        def prev_month(key: str) -> str:
            y, m = key.split("-")[:2]
            y, m = int(y), int(m)
            return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"

        for i, r in d.iterrows():
            value = vals.iloc[i]
            if pd.isna(value):
                continue
            key = keys[i]
            yoy_key = f"{int(key[:4]) - 1:04d}-{key[5:7]}"
            mom_key = prev_month(key)
            yoy = (value / val_map[yoy_key] - 1) * 100 if yoy_key in val_map else None
            mom = (value / val_map[mom_key] - 1) * 100 if mom_key in val_map else None
            add(code, name, r["日期"], value, unit, yoy, mom, source)

    def add_fred_monthly_latest(code, name, fn, unit, source):
        """FRED 日频序列按月取最新有效值，避免同月重复行。"""
        d = fetch(fn)
        if d.empty or not {"日期", "数值"}.issubset(d.columns):
            return
        d = d.copy()
        d["_月"] = d["日期"].astype(str).str[:7]
        d = d.dropna(subset=["数值"]).sort_values("日期").drop_duplicates("_月", keep="last")
        for _, r in d.iterrows():
            add(code, name, r["日期"], r["数值"], unit, source=source)

    logger.info("  [宏观采集] 分步明细：区间 %s ~ %s，本次调用 %d 个接口",
                start_date or "起", end_date or "今", total_ifaces)
    logger.info("  ├ 央行：货币供应量 / 存款 / 贷款 / 社融")
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

    # ============ 央行：社会融资规模存量（仅央行官方统计表，无插值） ============
    if only is None or SFR_STOCK_IFACE in only or "macro_china_shrzgm" in only:
        started = time.time()
        sfr_url_used = sfr_stock_url or ""
        sfr_label = IFACE_NAMES[SFR_STOCK_IFACE]
        sfr_params = {
            **call_params,
            "interface": SFR_STOCK_IFACE,
            "source": "中国人民银行调查统计司",
            "url": sfr_url_used,
            "受影响标的": "社会融资规模存量（央行官方统计表）",
        }
        try:
            from collectors.macro_sfr_stock import fetch_sfr_stock

            if sfr_stock_map is None:
                sfr_official, sfr_url_used = fetch_sfr_stock(
                    url=sfr_stock_url or None,
                    start_month=start_date, end_month=end_date,
                )
                sfr_params["url"] = sfr_url_used or ""
                sfr_params["start_month"] = start_date or ""
                sfr_params["end_month"] = end_date or ""
            else:
                sfr_official = sfr_stock_map
                sfr_params["source"] = "调用方传入官方口径"

            before = len(rows)
            if sfr_official:
                for m, (stock_wan, growth) in sorted(sfr_official.items()):
                    add("SFR_STOCK", "社会融资规模存量", m,
                        round(stock_wan * 10000, 4), "亿元", yoy=growth,
                        source=SFR_STOCK_SOURCE)
                logger.info("  │ [社融存量] 央行官方口径写入 %d 个月，来源 %s",
                            len(rows) - before, sfr_url_used or "显式传入口径")
            else:
                logger.warning("  │ [社融存量] 无官方数据，本次不产出该指标（插值逻辑已删除）")

            if monitor:
                monitor.record(
                    category="宏观", interface_key=f"macro.{SFR_STOCK_IFACE}",
                    interface_name=sfr_label, status="success",
                    attempts=1, rows=len(rows) - before, params=sfr_params,
                    duration_sec=time.time() - started,
                )
        except Exception as exc:
            reason = describe_failure(exc, url=sfr_url_used,
                                      extra="受影响标的=社会融资规模存量（央行官方统计表）")
            logger.error("  │ [社融存量] 官方统计表抓取失败，本次不产出该指标：%s", reason)
            if monitor:
                monitor.record(
                    category="宏观", interface_key=f"macro.{SFR_STOCK_IFACE}",
                    interface_name=sfr_label, status="failed", error=reason,
                    params=sfr_params, duration_sec=time.time() - started,
                )

    logger.info("  ├ 海关：进出口（出口 / 进口 / 进出口总额）")
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
                # 数据处理模块用「官方累计口径」计算进出口同比：累计出口+累计进口
                cum_exp = _to_float(r.get("累计出口额-金额"))
                cum_imp = _to_float(r.get("累计进口额-金额"))
                if cum_exp is not None and cum_imp is not None:
                    add("TRADE_CUM_TOTAL", "进出口累计总额（官方）", r["月份"],
                        (cum_exp + cum_imp) / 1e5, "亿美元",
                        source="海关总署（akshare 封装·官方累计额）")
        else:
            logger.warning("海关接口缺少列：%s", list(trade.columns))

    logger.info("  ├ 统计局：社会消费品零售 / CPI / PPI / PMI")
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

    logger.info("  ├ 交易所 / 外汇局：A股总市值 / 黄金储备 / 中国GDP")
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

    # ============ 中国 GDP（季度绝对值 + 同比增长） ============
    cn_gdp = fetch("macro_china_gdp")
    if not cn_gdp.empty and "国内生产总值-绝对值" in cn_gdp.columns:
        for _, r in cn_gdp.iterrows():
            add("CN_GDP", "中国GDP(季度绝对值)", _normalize_quarter(r["季度"]),
                r["国内生产总值-绝对值"], "亿元", r.get("国内生产总值-同比增长"), source=NBS)

    logger.info("  ├ 美国 FRED：GDP / CPI / 核心CPI / PPI / 非农 / 利率")
    # ============ 美国 GDP（FRED 环比折年率） ============
    us_gdp = fetch("fred_A191RL1Q225SBEA")
    if not us_gdp.empty and {"日期", "数值"}.issubset(us_gdp.columns):
        for _, r in us_gdp.iterrows():
            add("US_GDP", "美国GDP环比折年率", r["日期"], r["数值"], "%",
                source=FRED)

    # ============ 美国 CPI / 核心 CPI / PPI（FRED 指数，自动计算同比/环比） ============
    add_fred_level("US_CPI", "美国CPI指数", "fred_CPIAUCSL", "1982-84=100", FRED)
    add_fred_level("US_CORE_CPI", "美国核心CPI指数", "fred_CPILFESL", "1982-84=100", FRED)
    add_fred_level("US_PPI", "美国PPI指数", "fred_PPIACO", "指数", FRED)

    # ============ 美国非农新增就业（由 FRED 总就业人数月度差分得到） ============
    nfp = fetch("fred_PAYEMS")
    if not nfp.empty and {"日期", "数值"}.issubset(nfp.columns):
        nfp = nfp.copy().sort_values("日期").reset_index(drop=True)
        vals = pd.to_numeric(nfp["数值"], errors="coerce")
        change = vals.diff()
        for i, r in nfp.iterrows():
            add("US_NONFARM", "美国非农新增就业", r["日期"], change.iloc[i], "千人",
                source=FRED)

    # ============ 美国实际利率上下限（联邦基金目标利率，FRED 日频按月取最新） ============
    add_fred_monthly_latest(
        "US_RATE_UPPER", "美国联邦基金目标利率上限", "fred_DFEDTARU", "%", FRED)
    add_fred_monthly_latest(
        "US_RATE_LOWER", "美国联邦基金目标利率下限", "fred_DFEDTARL", "%", FRED)

    # ============ 长周期历史补全：存贷款余额 / 社零1-2月 ============
    # 仅当对应接口在采集范围内时补全；重建结果用同 biz_key 覆盖，与近期真实余额拼接。
    logger.info("  ├ 长周期历史补全：贷款余额 / 居民存款 / 社零1-2月")
    try:
        from collectors.macro_long_history import (
            LONG_DEPOSIT_IFACE, LONG_LOAN_IFACE, RETAIL_IFACE,
            build_long_loan_balance, build_long_hh_deposit, build_retail_jan_feb,
        )
        def fetch_helper(fn_name: str) -> pd.DataFrame:
            """补全用的内部接口不受 only 限制（它们是补全逻辑的一部分，非独立重跑标的）。"""
            return _fetch(ak, fn_name, monitor=monitor, retries=retries,
                          retry_interval=retry_interval, only=None, params=call_params)

        extra: list[pd.DataFrame] = []
        if only is None or "macro_rmb_loan" in only or LONG_LOAN_IFACE in only:
            d = build_long_loan_balance(fetch_helper, fetch_time)
            if not d.empty:
                extra.append(d)
        if only is None or "macro_rmb_deposit" in only or LONG_DEPOSIT_IFACE in only:
            d = build_long_hh_deposit(fetch_helper, fetch_time)
            if not d.empty:
                extra.append(d)
        if only is None or RETAIL_IFACE in only:
            d = build_retail_jan_feb(fetch_helper, fetch_time)
            if not d.empty:
                extra.append(d)
        if extra:
            base = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
            merged = pd.concat([base] + extra, ignore_index=True)
            # 长周期重建行覆盖同代码+日期的短周期行（以补全来源为准）
            merged = merged.drop_duplicates(subset=["指标代码", "日期"], keep="last")
            rows = merged[OUTPUT_COLUMNS].to_dict("records")
            logger.info("  └ 长周期补全完成：合并后 %d 行", len(rows))
    except Exception as exc:
        logger.warning("  └ 长周期历史补全失败，保留原始采集结果：%s", exc)

    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)

    # ============ 社零环比口径校正：用统计局官方「季调后环比」覆盖未季调名义环比 ============
    # akshare 的「环比增长」是当月/上月-1（±10%季节性波动），与官方季调口径（±1%）完全不同。
    applied = 0
    if not df.empty and (sa_mom_map is None or sa_mom_map):
        try:
            if sa_mom_map is None:
                from collectors.macro_sa_mom import fetch_sa_mom
                sa_mom_map, sa_mom_url = fetch_sa_mom()
                logger.info("  ├ 社零季调环比：已获取统计局最新一期（%d 个月，%s）",
                            len(sa_mom_map), sa_mom_url or "官网")
            if sa_mom_map:
                from collectors.macro_sa_mom import apply_sa_mom
                df, applied = apply_sa_mom(df, sa_mom_map)
                logger.info("  └ 社零环比口径已校正：覆盖 %d 个月为官方季调值", applied)
        except Exception as exc:  # 抓取失败不能让整个宏观采集失败
            logger.warning("  └ 社零季调环比获取失败，保留接口原环比（未季调口径）：%s", exc)

    if not df.empty:
        df = df.sort_values(["指标代码", "日期"], ascending=[True, False]).reset_index(drop=True)
    logger.info("  └ [宏观采集] 合并完成：%d 行，%d 个指标（本次调用 %d 个接口，社零季调覆盖 %d 月）",
                len(df), df["指标代码"].nunique() if not df.empty else 0, fetch_count, applied)
    return df
