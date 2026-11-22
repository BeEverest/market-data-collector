"""结果导出：宏观 / ETF / 接口监控分类导出（均含口径说明），及历史快照保留策略。"""
from __future__ import annotations

import logging
import re
import zipfile
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
    ["宏观-增长", "中国GDP(季度绝对值)", "国家统计局季度累计绝对值，日期映射为季末月，单位：亿元；同比来自统计局"],
    ["宏观-美国", "美国GDP/CPI/核心CPI/PPI/非农", "FRED（BEA/BLS）；指数类按上年同月/上月计算同比环比；非农为月度新增千人"],
    ["宏观-美国", "美国联邦基金目标利率上限/下限", "FRED DFEDTARU / DFEDTARL 实际值；原始日频按月取最新，单位 %"],
    ["ETF", "交易日期", "交易日（日频口径），非季末；交易所数据通常于次一交易日早晨更新上一交易日"],
    ["ETF", "份额(亿份)", "交易所披露的当日基金总份额，单位：亿份（上交所/SH、深交所/SZ）"],
    ["ETF", "单位净值(元)", "天天基金披露的当日单位净值，单位：元/份"],
    ["ETF", "规模(亿元)", "规模 = 份额(亿份) × 单位净值(元)，单位：亿元"],
    ["ETF", "交易市场", "SH=上海证券交易所，SZ=深圳证券交易所"],
    ["指数", "交易日期", "交易日（日频口径）；采集范围为近一年（默认 365 天，可配置）"],
    ["指数", "开盘价/最高价/最低价/收盘价", "指数当日 OHLC，单位：点；数据源为新浪财经日线（akshare 封装）"],
    ["指数", "成交量", "指数当日成交量，单位：手（新浪财经口径）"],
    ["指数", "涨跌幅(%)", "由相邻交易日收盘价环比计算，(今收/昨收-1)×100；每只指数区间首日为空，单位 %"],
    ["数据源", "宏观数据", "央行 / 统计局 / 海关总署 / 外汇局，经 akshare 封装；社融锚点来自央行报告"],
    ["数据源", "ETF 份额 / 净值", "份额：上交所、深交所；净值：天天基金（东方财富）"],
    ["注意", "单位", "每条记录的单位见「单位」列，不同指标单位不同，勿直接横向比较绝对值"],
    ["注意", "同比/环比", "同比、环比单位均为 %；来源缺失时留空"],
    ["注意", "ETF 口径", "日频口径；规模为份额×净值计算值，与基金公司披露净资产可能存在细微差异"],
    ["监控", "运行日期/失败原因", "接口监控 sheet 记录每次调用的运行日期、状态、失败原因、重试次数"],
    ["监控", "重跑机制", "python main.py --retry-failed 重跑最近批次失败接口；--monitor-report 生成历史监控报告"],
]


def _notes_for(categories: set[str]) -> pd.DataFrame:
    # 表头行（类别=「类别」）不参与分类过滤，必须显式保留为 columns
    rows = [r for r in _NOTES[1:] if r[0] in categories]
    return pd.DataFrame(rows, columns=_NOTES[0])


def _stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _export_dir(base_dir: Path, config: dict) -> Path:
    out_dir = base_dir / config.get("output", {}).get("dir", "output") / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _write_excel(path: Path, sheets: list[tuple[str, pd.DataFrame]]) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, df in sheets:
            df.to_excel(writer, sheet_name=name, index=False)
            ws = writer.sheets[name]
            for column in ws.columns:
                max_len = max(
                    (len(str(cell.value)) for cell in column if cell.value is not None),
                    default=8,
                )
                ws.column_dimensions[column[0].column_letter].width = min(max_len + 2, 46)
            ws.freeze_panes = "A2"


def _zip_files(files: list[str], zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            zf.write(file, arcname=Path(file).name)
    return str(zip_path)


def export_macro(macro_df: pd.DataFrame, base_dir: Path, config: dict,
                 stamp: str | None = None) -> list[str]:
    """导出宏观数据：CSV + 口径说明 CSV + Excel（含口径说明 Sheet）。"""
    stamp = stamp or _stamp()
    out_dir = _export_dir(base_dir, config)
    notes = _notes_for({"宏观-货币信贷", "宏观-外贸", "宏观-消费物价",
                        "宏观-景气市场", "宏观-增长", "宏观-美国", "数据源", "注意"})
    csv_path = out_dir / f"宏观数据_{stamp}.csv"
    note_path = out_dir / f"宏观数据口径说明_{stamp}.csv"
    xlsx_path = out_dir / f"宏观数据_{stamp}.xlsx"
    macro_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    notes.to_csv(note_path, index=False, encoding="utf-8-sig")
    _write_excel(xlsx_path, [("宏观数据", macro_df), ("口径说明", notes)])
    zip_path = out_dir / f"宏观数据_{stamp}.zip"
    files = [str(csv_path), str(note_path), str(xlsx_path)]
    files.append(_zip_files(files, zip_path))
    logger.info("宏观数据导出完成：%d 行", len(macro_df))
    return files


def export_etf(etf_df: pd.DataFrame, base_dir: Path, config: dict,
               stamp: str | None = None) -> list[str]:
    """导出 ETF 数据：CSV + 口径说明 CSV + Excel（含口径说明 Sheet）。"""
    stamp = stamp or _stamp()
    out_dir = _export_dir(base_dir, config)
    notes = _notes_for({"ETF", "数据源", "注意"})
    csv_path = out_dir / f"ETF数据_{stamp}.csv"
    note_path = out_dir / f"ETF数据口径说明_{stamp}.csv"
    xlsx_path = out_dir / f"ETF数据_{stamp}.xlsx"
    etf_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    notes.to_csv(note_path, index=False, encoding="utf-8-sig")
    _write_excel(xlsx_path, [("ETF规模", etf_df), ("口径说明", notes)])
    zip_path = out_dir / f"ETF数据_{stamp}.zip"
    files = [str(csv_path), str(note_path), str(xlsx_path)]
    files.append(_zip_files(files, zip_path))
    logger.info("ETF 数据导出完成：%d 行", len(etf_df))
    return files


def export_index(index_df: pd.DataFrame, base_dir: Path, config: dict,
                 stamp: str | None = None) -> list[str]:
    """导出指数数据：CSV + 口径说明 CSV + Excel（含口径说明 Sheet）+ ZIP。"""
    stamp = stamp or _stamp()
    out_dir = _export_dir(base_dir, config)
    notes = _notes_for({"指数", "数据源", "注意"})
    csv_path = out_dir / f"指数数据_{stamp}.csv"
    note_path = out_dir / f"指数数据口径说明_{stamp}.csv"
    xlsx_path = out_dir / f"指数数据_{stamp}.xlsx"
    index_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    notes.to_csv(note_path, index=False, encoding="utf-8-sig")
    _write_excel(xlsx_path, [("指数数据", index_df), ("口径说明", notes)])
    zip_path = out_dir / f"指数数据_{stamp}.zip"
    files = [str(csv_path), str(note_path), str(xlsx_path)]
    files.append(_zip_files(files, zip_path))
    logger.info("指数数据导出完成：%d 行", len(index_df))
    return files


def export_monitor(history_df: pd.DataFrame, base_dir: Path, config: dict,
                   stamp: str | None = None) -> list[str]:
    """导出接口监控：CSV + 口径说明 CSV + Excel（四张报告 + 口径说明）。"""
    from monitor import Monitor

    stamp = stamp or _stamp()
    out_dir = _export_dir(base_dir, config)
    notes = _notes_for({"监控", "注意"})
    report = Monitor(base_dir).build_report(history=history_df)
    csv_path = out_dir / f"接口监控_{stamp}.csv"
    note_path = out_dir / f"接口监控口径说明_{stamp}.csv"
    xlsx_path = out_dir / f"接口监控_{stamp}.xlsx"
    detail = report.get("调用明细", pd.DataFrame())
    detail.to_csv(csv_path, index=False, encoding="utf-8-sig")
    notes.to_csv(note_path, index=False, encoding="utf-8-sig")
    sheets = list(report.items()) + [("口径说明", notes)]
    _write_excel(xlsx_path, sheets)
    zip_path = out_dir / f"接口监控_{stamp}.zip"
    files = [str(csv_path), str(note_path), str(xlsx_path)]
    files.append(_zip_files(files, zip_path))
    logger.info("接口监控导出完成：%d 行调用明细", len(detail))
    return files


def export_full(macro_df: pd.DataFrame, etf_df: pd.DataFrame,
                history_df: pd.DataFrame, base_dir: Path, config: dict) -> list[str]:
    """分类导出后打包为 ZIP，返回所有生成文件路径（ZIP 在最后）。"""
    stamp = _stamp()
    files = export_macro(macro_df, base_dir, config, stamp)
    files += export_etf(etf_df, base_dir, config, stamp)
    files += export_monitor(history_df, base_dir, config, stamp)
    files = [f for f in files if not f.lower().endswith(".zip")]
    out_dir = _export_dir(base_dir, config)
    zip_path = out_dir / f"全量导出_{stamp}.zip"
    files.append(_zip_files(files, zip_path))
    logger.info("全量导出完成：%s", zip_path)
    return files


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
        "宏观数据_*.csv", "宏观数据口径说明_*.csv", "宏观数据_*.xlsx", "宏观数据_*.zip",
        "ETF数据_*.csv", "ETF数据口径说明_*.csv", "ETF数据_*.xlsx", "ETF数据_*.zip",
        "指数数据_*.csv", "指数数据口径说明_*.csv", "指数数据_*.xlsx", "指数数据_*.zip",
        "接口监控_*.csv", "接口监控口径说明_*.csv", "接口监控_*.xlsx", "接口监控_*.zip",
        "全量导出_*.zip",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(out_dir.glob(pattern))
    candidates.extend((out_dir / "exports").glob("*"))
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
