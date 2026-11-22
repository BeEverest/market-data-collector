"""结果导出：CSV 与 Excel（含字段口径说明），及历史快照保留策略。"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_NOTES = [
    ["类别", "字段 / 指标", "口径说明"],
    ["宏观-货币信贷", "M0 / M1 / M2", "流通中货币 / 狭义货币 / 广义货币，央行月度，单位：亿元"],
    ["宏观-货币信贷", "居民储蓄存款余额", "住户/储蓄存款口径，单位：亿元"],
    ["宏观-货币信贷", "人民币存款余额 / 贷款余额", "金融机构本外币存贷款，单位：亿元"],
    ["宏观-货币信贷", "社会融资规模存量", "央行月末存量；锚点月为央行公布值，其余月按社融增量插值，单位：亿元"],
    ["宏观-外贸", "出口额 / 进口额 / 进出口总额", "海关总署月度，单位：亿美元（由千美元换算）"],
    ["宏观-消费物价", "社会消费品零售总额(当月)", "国家统计局月度当月值，单位：亿元"],
    ["宏观-消费物价", "CPI", "居民消费价格指数（全国当月，上年同月=100），单位：指数点"],
    ["宏观-消费物价", "PPI", "工业生产者出厂价格指数（当月，上年同月=100），单位：指数点"],
    ["宏观-景气市场", "制造业PMI / 非制造业PMI", "采购经理指数，50 为荣枯线，单位：指数点"],
    ["宏观-景气市场", "A股总市值(沪深)", "沪市 + 深市市价总值，单位：亿元"],
    ["宏观-景气市场", "央行黄金储备", "国家外汇管理局月末黄金储备，单位：万盎司"],
    ["ETF", "交易日期", "交易日（日频口径），非季末；交易所数据通常于次一交易日早晨更新上一交易日"],
    ["ETF", "份额(亿份)", "交易所披露的当日基金总份额，单位：亿份（上交所/SH、深交所/SZ）"],
    ["ETF", "单位净值(元)", "天天基金披露的当日单位净值，单位：元/份"],
    ["ETF", "规模(亿元)", "规模 = 份额(亿份) × 单位净值(元)，单位：亿元"],
    ["ETF", "交易市场", "SH=上海证券交易所，SZ=深圳证券交易所"],
    ["数据源", "宏观数据", "央行 / 统计局 / 海关总署 / 外汇局，经 akshare 封装；社融锚点来自央行报告"],
    ["数据源", "ETF 份额 / 净值", "份额：上交所、深交所；净值：天天基金（东方财富）"],
    ["注意", "单位", "每条记录的单位见「单位」列，不同指标单位不同，勿直接横向比较绝对值"],
    ["注意", "同比/环比", "同比、环比单位均为 %；来源缺失时留空"],
    ["注意", "ETF 口径", "日频口径；规模为份额×净值计算值，与基金公司披露净资产可能存在细微差异"],
    ["监控", "运行日期/失败原因", "接口监控 sheet 记录每次调用的运行日期、状态、失败原因、重试次数"],
    ["监控", "重跑机制", "python main.py --retry-failed 重跑最近批次失败接口；--monitor-report 生成历史监控报告"],
]


def export_results(
    macro_df: pd.DataFrame,
    etf_df: pd.DataFrame,
    base_dir: Path,
    config: dict,
    run_ts: str,
    monitor_records: list[dict] | None = None,
) -> list[str]:
    """按配置导出 CSV / Excel，返回生成的文件路径列表。"""
    output_cfg = config.get("output", {})
    out_dir = base_dir / output_cfg.get("dir", "output")
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = output_cfg.get("filename_prefix", "market_data")
    formats = output_cfg.get("formats", ["csv", "excel"])
    files: list[str] = []

    if "csv" in formats:
        macro_csv = out_dir / f"{prefix}_宏观数据_{run_ts}.csv"
        etf_csv = out_dir / f"{prefix}_ETF规模_{run_ts}.csv"
        macro_df.to_csv(macro_csv, index=False, encoding="utf-8-sig")
        etf_df.to_csv(etf_csv, index=False, encoding="utf-8-sig")
        files.extend([str(macro_csv), str(etf_csv)])
        logger.info("CSV 导出完成：%d 行宏观 / %d 行 ETF", len(macro_df), len(etf_df))

    if "excel" in formats:
        xlsx = out_dir / f"{prefix}_{run_ts}.xlsx"
        notes_df = pd.DataFrame(_NOTES[1:], columns=_NOTES[0])
        monitor_df = pd.DataFrame(monitor_records or [])
        if not monitor_df.empty:
            monitor_df = monitor_df[[
                "run_date", "run_type", "category", "interface_key", "interface_name",
                "status", "attempts", "rows", "error", "duration_sec",
            ]].rename(columns={
                "run_date": "运行日期", "run_type": "运行类型", "category": "类别",
                "interface_key": "接口键", "interface_name": "接口名称", "status": "状态",
                "attempts": "尝试次数", "rows": "数据行数", "error": "失败原因",
                "duration_sec": "耗时(秒)",
            })
        with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
            macro_df.to_excel(writer, sheet_name="宏观数据", index=False)
            etf_df.to_excel(writer, sheet_name="ETF规模", index=False)
            notes_df.to_excel(writer, sheet_name="口径说明", index=False)
            if not monitor_df.empty:
                monitor_df.to_excel(writer, sheet_name="接口监控", index=False)
            for sheet in writer.book.worksheets:
                for column in sheet.columns:
                    max_len = max(
                        (len(str(cell.value)) for cell in column if cell.value is not None),
                        default=8,
                    )
                    sheet.column_dimensions[column[0].column_letter].width = min(max_len + 2, 46)
                sheet.freeze_panes = "A2"
        files.append(str(xlsx))
        logger.info("Excel 导出完成：%s", xlsx)

    return files


# 批次时间戳：run_ts 形如 20261122_180123_full，用前两段识别同一批次
_BATCH_RE = re.compile(r"(\d{8}_\d{6})")


def prune_snapshots(base_dir: Path, config: dict) -> list[str]:
    """只保留最近 N 份数据快照，清理更早批次的文件。

    快照 = 每批次导出的宏观 CSV / ETF CSV / 主 Excel / 运维监控报告 Excel。
    接口调用历史 interface_runs.jsonl 不属于快照，永不清理（重跑依赖它）。
    N 由 output.keep_snapshots 配置，默认 3。返回被删除的文件路径列表。
    """
    output_cfg = config.get("output", {})
    out_dir = base_dir / output_cfg.get("dir", "output")
    if not out_dir.exists():
        return []
    prefix = output_cfg.get("filename_prefix", "market_data")
    keep = max(1, int(output_cfg.get("keep_snapshots", 3)))

    # 收集所有快照文件（CSV/Excel），按批次时间戳归组；排除 monitor 下的 jsonl
    batches: dict[str, list[Path]] = {}
    patterns = [
        f"{prefix}_宏观数据_*.csv", f"{prefix}_ETF规模_*.csv", f"{prefix}_*.xlsx",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(out_dir.glob(pattern))
    candidates.extend((out_dir / "monitor").glob("运维监控报告_*.xlsx"))

    for path in candidates:
        match = _BATCH_RE.search(path.name)
        if not match:
            continue
        batches.setdefault(match.group(1), []).append(path)

    if len(batches) <= keep:
        logger.info("快照清理：当前 %d 个批次，未超过保留上限 %d", len(batches), keep)
        return []

    ordered = sorted(batches)  # 时间戳字符串按字典序即时间顺序
    to_remove = ordered[:-keep]
    removed: list[str] = []
    for batch in to_remove:
        for path in batches[batch]:
            try:
                path.unlink()
                removed.append(str(path))
                logger.info("清理旧快照：%s", path)
            except OSError as exc:
                logger.warning("清理快照失败 %s：%s", path, exc)
    logger.info("快照清理完成：删除 %d 个批次的文件，保留最近 %d 份", len(to_remove), keep)
    return removed
