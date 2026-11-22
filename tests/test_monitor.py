"""monitor.py 单元测试：记录规范化、持久化、失败任务与监控报告。"""
from __future__ import annotations

import json

import pandas as pd

from monitor import RUN_FIELDS, Monitor, describe_failure


def _frame(records: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(records, columns=RUN_FIELDS)


class TestRecord:
    def test_record_normalizes_fields(self, base):
        mon = Monitor(base, run_type="full")
        rec = mon.record(
            category="宏观", interface_key="macro.cpi", interface_name="CPI",
            status="failed", params={"code": "CPI"}, attempts=3, rows=2,
            error=None, duration_sec=1.23456,
        )
        assert rec["run_id"].endswith("_full")
        assert rec["run_date"]
        assert json.loads(rec["params"]) == {"code": "CPI"}
        assert rec["error"] == ""
        assert rec["duration_sec"] == 1.235
        assert mon.records == [rec]

    def test_save_to_jsonl_and_load(self, base):
        mon = Monitor(base)
        mon.record(category="宏观", interface_key="k", interface_name="K", status="success")
        path = mon.save()
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        loaded = Monitor.load(path)
        assert len(loaded) == 1 and loaded.iloc[0]["status"] == "success"

    def test_save_to_sqlite(self, base, db_path):
        mon = Monitor(base, db_path=db_path)
        mon.record(category="ETF", interface_key="etf.nav", interface_name="净值", status="success", rows=3)
        saved = mon.save()
        assert str(saved) == str(db_path)
        loaded = Monitor.load(mon.history_path, db_path=db_path)
        assert len(loaded) == 1 and loaded.iloc[0]["rows"] == 3

    def test_load_skips_broken_lines_and_empty_file(self, base):
        empty = base / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        assert Monitor.load(empty).empty
        path = base / "broken.jsonl"
        path.write_text(
            json.dumps({"run_id": "r", "status": "success"}, ensure_ascii=False) + "\n{broken\n",
            encoding="utf-8",
        )
        df = Monitor.load(path)
        assert len(df) == 1 and df.iloc[0]["run_id"] == "r"


class TestFailedTasks:
    def test_extracts_latest_failed_batch(self, base, run_records):
        mon = Monitor(base)
        tasks = mon.failed_tasks(history=_frame(run_records))
        assert len(tasks) == 1
        assert tasks[0]["interface_key"] == "etf.sse_shares"
        assert tasks[0]["params"] == {"date": "2026-11-01"}
        assert tasks[0]["failed_run"] == "20261102_090000_full"
        assert tasks[0]["last_error"] == "ValueError: 无数据"

    def test_returns_empty_when_no_failure(self, base, run_records):
        ok_only = [r for r in run_records if r["status"] == "success"]
        assert Monitor(base).failed_tasks(history=_frame(ok_only)) == []
        assert Monitor(base).failed_tasks(history=pd.DataFrame(columns=RUN_FIELDS)) == []

    def test_broken_params_fallback_to_empty_dict(self, base, run_records):
        rec = dict(run_records[1])
        rec["params"] = "not-json"
        tasks = Monitor(base).failed_tasks(history=_frame([rec]))
        assert tasks[0]["params"] == {}


class TestReport:
    def test_empty_report(self, base):
        report = Monitor(base).build_report(history=pd.DataFrame(columns=RUN_FIELDS))
        assert set(report) == {"运行总览", "失败明细", "接口健康度", "调用明细", "采集量统计"}
        assert "暂无监控记录" in report["调用明细"].iloc[0, 0]
        assert "暂无采集量统计" in report["采集量统计"].iloc[0, 0]

    def test_report_sheets_and_success_rate(self, base, run_records):
        report = Monitor(base).build_report(history=_frame(run_records))
        overview = report["运行总览"].set_index("运行ID")
        assert len(overview) == 3
        assert overview.loc["20261102_100000_retry", "成功率(%)"] == 100.0
        full_run = overview.loc["20261101_090000_full"]
        assert full_run["接口调用数"] == 2
        assert full_run["失败数"] == 1
        assert full_run["成功率(%)"] == 50.0

        fails = report["失败明细"].set_index("运行ID")
        assert "已重跑成功" in fails.loc["20261102_090000_full", "重跑结果"]
        assert fails.loc["20261101_090000_full", "重跑结果"] == "未重跑成功"

        health = report["接口健康度"].set_index("接口键")
        assert health.loc["etf.sse_shares", "成功率(%)"] == 50.0
        assert health.loc["macro.macro_china_cpi", "最近失败原因"] == "TimeoutError: 读超时"

        detail = report["调用明细"]
        assert len(detail) == 4
        assert "运行时间" in detail.columns and "失败原因" in detail.columns

    def test_report_without_failures(self, base, run_records):
        ok_only = [r for r in run_records if r["status"] == "success"]
        report = Monitor(base).build_report(history=_frame(ok_only))
        assert report["失败明细"].iloc[0, 0] == "无失败记录"

    def test_report_with_collection_stats(self, base, run_records):
        stats = pd.DataFrame([
            {"run_id": "20261101_090000_full", "run_type": "full", "run_date": "2026-11-01",
             "category": "宏观", "code": "M2", "name": "货币供应量", "rows": 24,
             "collected_at": "2026-11-01 09:00:30"},
            {"run_id": "20261101_090000_full", "run_type": "full", "run_date": "2026-11-01",
             "category": "ETF", "code": "510050", "name": "50ETF", "rows": 20,
             "collected_at": "2026-11-01 09:00:40"},
        ])
        report = Monitor(base).build_report(history=_frame(run_records), stats_df=stats)
        stat = report["采集量统计"]
        assert len(stat) == 2
        assert set(["运行ID", "类别口径", "细分口径", "采集行数"]).issubset(stat.columns)
        assert stat.iloc[0]["采集行数"] == 24


class TestDescribeFailure:
    def test_exception_and_empty(self):
        assert describe_failure(ValueError("bad")) == "ValueError: bad"
        assert describe_failure(None, empty=True) == "接口返回空数据（无有效记录）"
        assert describe_failure(None) == "未知原因"

    def test_structured_context(self):
        text = describe_failure(
            TimeoutError("读超时"), url="https://api.example.com/x",
            http_status=503, extra="重试3次",
        )
        assert "TimeoutError: 读超时" in text
        assert "HTTP 503" in text
        assert "URL=https://api.example.com/x" in text
        assert "重试3次" in text
