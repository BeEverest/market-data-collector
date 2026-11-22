"""失败诊断增强测试：失败原因含响应消息、明确指出受影响标的。"""
from __future__ import annotations

from unittest.mock import Mock

import requests

from collectors import etf


def _http_error(message: str, status: int, body: str) -> requests.RequestException:
    """构造带 response 的请求异常，模拟服务端返回错误体。"""
    response = Mock()
    response.status_code = status
    response.text = body
    response.url = "https://fund.szse.cn/api/report/ShowReport"
    err = requests.ConnectionError(message)
    err.response = response
    return err


class TestResponseSnippet:
    def test_extracts_flattened_text(self):
        err = _http_error("boom", 429, "  访问 \n 过于频繁  limit  ")
        assert etf._response_snippet(err) == "访问 过于频繁 limit"

    def test_no_response_returns_empty(self):
        assert etf._response_snippet(ValueError("no response")) == ""


class TestSzseFailureDetail:
    def test_failure_reports_response_and_targets(self):
        mon = Mock()
        err = _http_error("RemoteDisconnected", 502, "Bad Gateway upstream closed")
        sess = Mock()
        sess.get.side_effect = err
        result = etf.fetch_szse_shares(
            "2026-11-01", "2026-11-21", retries=1, session=sess,
            monitor=mon, target_codes=["159915", "159919"],
        )
        assert result.empty
        rec = mon.record.call_args.kwargs
        assert rec["status"] == "failed"
        assert "受影响标的=159915, 159919" in rec["error"]
        assert "响应片段=Bad Gateway upstream closed" in rec["error"]
        assert rec["params"]["受影响标的"] == "159915, 159919"

    def test_empty_data_reports_targets(self):
        mon = Mock()
        sess = Mock()
        resp = Mock()
        resp.raise_for_status.return_value = None
        # 造一个只有空表的 xlsx 响应
        import io
        import pandas as pd
        buf = io.BytesIO()
        pd.DataFrame(columns=["日期", "基金代码", "基金简称", "基金规模(份)"]).to_excel(
            buf, index=False
        )
        resp.content = buf.getvalue()
        sess.get.return_value = resp
        result = etf.fetch_szse_shares(
            "2026-11-01", "2026-11-21", retries=1, session=sess,
            monitor=mon, target_codes=["159915"],
        )
        assert result.empty
        rec = mon.record.call_args.kwargs
        assert rec["status"] == "failed"
        assert "受影响标的=159915" in rec["error"]


class TestSseFailureDetail:
    def test_empty_data_reports_targets_and_response(self):
        mon = Mock()
        sess = Mock()
        resp = Mock()
        resp.raise_for_status.return_value = None
        resp.text = '{"result": []}'
        resp.json.return_value = {"result": []}
        sess.get.return_value = resp
        result = etf.fetch_sse_shares(
            "2026-11-23", retries=1, session=sess,
            monitor=mon, target_codes=["510050", "510300"],
        )
        assert result.empty
        rec = mon.record.call_args.kwargs
        assert rec["status"] == "failed"
        assert "接口返回空数据" in rec["error"]
        assert "受影响标的=510050, 510300" in rec["error"]
        assert '响应片段={"result": []}' in rec["error"]
        assert rec["params"]["受影响标的"] == "510050, 510300"

    def test_failure_reports_targets(self):
        mon = Mock()
        err = _http_error("timeout", 504, "upstream timeout")
        sess = Mock()
        sess.get.side_effect = err
        result = etf.fetch_sse_shares(
            "2026-11-20", retries=1, session=sess,
            monitor=mon, target_codes=["510050", "510300"],
        )
        assert result.empty
        rec = mon.record.call_args.kwargs
        assert "受影响标的=510050, 510300" in rec["error"]
        assert "响应片段=upstream timeout" in rec["error"]
