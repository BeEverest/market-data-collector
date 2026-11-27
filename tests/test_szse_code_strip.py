"""深交所份额数据基金代码清洗测试。

回归 bug：深交所部分交易日（如 2026-11-20）导出的 xlsx 中，基金代码带
尾随空格（'159629  '）。旧代码仅 .str.zfill(6)，空格保留，导致 collect_etf
按 target_codes 做 isin 过滤时整批标的被丢弃，当日份额缺失。
"""
from __future__ import annotations

import io
from unittest.mock import Mock

import pandas as pd
import requests

from collectors import etf


def _xlsx_response(rows: list[dict]) -> Mock:
    """把行数据构造成深交所 xlsx 响应（基金代码带尾随空格）。"""
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, engine="openpyxl")
    resp = Mock()
    resp.content = buf.getvalue()
    resp.raise_for_status = Mock()
    return resp


class TestSzseCodeStrip:
    def test_trailing_spaces_stripped_before_zfill(self):
        """11-20 真实形态：代码带两空格，解析后应为干净 6 位代码。"""
        sess = Mock()
        sess.get.return_value = _xlsx_response([
            {"日期": "2026-11-20", "基金代码": "159629  ",
             "基金简称": "中证1000ETF富国", "基金规模(份)": 795252663.0},
            {"日期": "2026-11-20", "基金代码": "159919  ",
             "基金简称": "沪深300ETF嘉实", "基金规模(份)": 21567816676.0},
        ])
        out = etf.fetch_szse_shares(
            "2026-11-20", "2026-11-20", retries=1, session=sess,
            target_codes=["159629", "159919"],
        )
        assert not out.empty
        assert set(out["基金代码"]) == {"159629", "159919"}
        # 份额换算正确：份 / 1e8
        row = out[out["基金代码"] == "159629"].iloc[0]
        assert abs(row["份额(亿份)"] - 7.95252663) < 1e-6
        assert row["交易日期"] == "2026-11-20"

    def test_codes_match_target_codes_after_strip(self):
        """清洗后的代码应能通过 collect_etf 的 isin(target_codes) 过滤。"""
        sess = Mock()
        sess.get.return_value = _xlsx_response([
            {"日期": "2026-11-20", "基金代码": "159845  ",
             "基金简称": "中证1000ETF华夏", "基金规模(份)": 2388425606.0},
        ])
        out = etf.fetch_szse_shares(
            "2026-11-20", "2026-11-20", retries=1, session=sess,
            target_codes=["159845"],
        )
        assert out["基金代码"].isin(["159845"]).all(), "带空格代码无法匹配 target_codes，会导致当日数据整批丢失"

    def test_clean_codes_still_work(self):
        """无空格的干净代码不应被回归改动破坏。"""
        sess = Mock()
        sess.get.return_value = _xlsx_response([
            {"日期": "2026-11-19", "基金代码": "159915",
             "基金简称": "创业板ETF易方达", "基金规模(份)": 49907454936.0},
        ])
        out = etf.fetch_szse_shares(
            "2026-11-19", "2026-11-19", retries=1, session=sess,
            target_codes=["159915"],
        )
        assert set(out["基金代码"]) == {"159915"}

    def test_remote_disconnect_uses_stored_fallback_and_marks_no_new_row(self):
        """接口断连时沿用已落库份额，但必须标记不写回、不计新增。"""
        sess = Mock()
        sess.get.side_effect = requests.ConnectionError(
            "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
        )
        monitor = Mock()
        fallback = pd.DataFrame([{
            "基金代码": "159629", "基金简称": "中证1000ETF富", "交易市场": "SZ",
            "交易日期": "2026-11-25", "份额(亿份)": 8.09252663,
        }])
        out = etf.fetch_szse_shares(
            "2026-11-25", "2026-11-25", retries=1, session=sess,
            target_codes=["159629"], fallback_frame=fallback, monitor=monitor,
        )

        assert len(out) == 1
        assert out.iloc[0]["基金代码"] == "159629"
        assert out.iloc[0]["交易日期"] == "2026-11-25"
        assert out.iloc[0]["数据源"] == "深交所接口失败-沿用已落库（不计新增）"
        record = monitor.record.call_args.kwargs
        assert record["status"] == "failed"
        assert "已沿用已落库份额 1 行" in record["error"]

    def test_success_response_does_not_use_fallback(self):
        """接口成功时不能把兜底数据混入本次采集结果。"""
        sess = Mock()
        sess.get.return_value = _xlsx_response([
            {"日期": "2026-11-20", "基金代码": "159845  ",
             "基金简称": "中证1000ETF华夏", "基金规模(份)": 2388425606.0},
        ])
        fallback = pd.DataFrame([{
            "基金代码": "159629", "基金简称": "中证1000ETF富", "交易市场": "SZ",
            "交易日期": "2026-11-20", "份额(亿份)": 8.0,
        }])
        out = etf.fetch_szse_shares(
            "2026-11-20", "2026-11-20", retries=1, session=sess,
            target_codes=["159845", "159629"], fallback_frame=fallback,
        )
        assert set(out["基金代码"]) == {"159845"}
        assert "数据源" not in out.columns
