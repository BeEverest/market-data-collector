"""央行社会融资规模存量：官方口径解析与采集测试（不联网）。"""
import pandas as pd
import pytest

from collectors import macro, macro_sfr_stock as mss


SAMPLE_HTML = """
<table>
  <tr><td>社会融资规模存量统计表</td></tr>
  <tr><td>Aggregate Financing to the Real Economy (Stock)</td></tr>
  <tr><td>单位：万亿元人民币</td></tr>
  <tr><td></td><td>2026.1</td><td>2026.2</td><td>2026.3</td><td>2026.4</td><td>2026.5</td><td>2026.6</td></tr>
  <tr><td>项目 Items</td><td>存量</td><td>增速（%）</td><td>存量</td><td>增速（%）</td><td>存量</td><td>增速（%）</td>
      <td>存量</td><td>增速（%）</td><td>存量</td><td>增速（%）</td><td>存量</td><td>增速（%）</td></tr>
  <tr><td>社会融资规模存量 AFRE(stock)</td>
      <td>449.11</td><td>8.2</td><td>451.40</td><td>8.2</td><td>456.46</td><td>7.9</td>
      <td>456.88</td><td>7.8</td><td>458.81</td><td>7.7</td><td>462.06</td><td>7.4</td></tr>
  <tr><td>人民币贷款 RMB loans</td><td>260</td><td>7.2</td><td>261</td><td>7.1</td><td>262</td><td>7.0</td>
      <td>263</td><td>7.0</td><td>264</td><td>6.9</td><td>265</td><td>6.8</td></tr>
  <tr><td>注：社会融资规模存量是指一定时期末实体经济从金融体系获得的资金余额。</td></tr>
  <tr><td>Note: AFRE(stock) refers to the outstanding of financing.</td></tr>
</table>
"""


class TestParseSfrStock:
    def test_parse_months_and_values(self):
        d = mss.parse_sfr_stock(SAMPLE_HTML)
        assert len(d) == 6
        assert d["2026-01"] == (449.11, 8.2)
        assert d["2026-03"] == (456.46, 7.9)
        assert d["2026-05"] == (458.81, 7.7)
        assert d["2026-06"] == (462.06, 7.4)

    def test_ignores_empty_stock_cells(self):
        html = SAMPLE_HTML.replace("<td>458.81</td><td>7.7</td>", "<td></td><td></td>")
        d = mss.parse_sfr_stock(html)
        assert "2026-05" not in d
        assert "2026-01" in d

    def test_no_table_returns_empty(self):
        assert mss.parse_sfr_stock("<html><body>无表格</body></html>") == {}

    def test_too_few_rows_returns_empty(self):
        assert mss.parse_sfr_stock("<table><tr><td>2026.1</td></tr></table>") == {}


class TestCollectMacroSfrStock:
    @pytest.fixture(autouse=True)
    def _no_akshare(self, monkeypatch):
        monkeypatch.setattr(macro, "_fetch", lambda *a, **k: pd.DataFrame())

    def test_writes_official_rows_only(self):
        stock_map = {"2026-01": (449.11, 8.2), "2026-02": (451.40, 8.2)}
        df = macro.collect_macro(
            start_date="2026-01", end_date="2026-02", sfr_stock_map=stock_map,
        )
        rows = df[df["指标代码"] == "SFR_STOCK"]
        assert len(rows) == 2
        assert set(rows["日期"]) == {"2026-01", "2026-02"}
        jan = rows[rows["日期"] == "2026-01"].iloc[0]
        assert jan["数值"] == pytest.approx(4491100.0)
        assert jan["同比(%)"] == pytest.approx(8.2)
        assert "中国人民银行" in jan["数据源"]
        assert not any("插值" in str(s) for s in rows["数据源"])

    def test_empty_map_produces_no_rows(self):
        df = macro.collect_macro(start_date="2026-01", end_date="2026-02", sfr_stock_map={})
        assert df[df["指标代码"] == "SFR_STOCK"].empty

    def test_fetch_failure_has_no_interpolation_fallback(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("央行站点不可达")

        monkeypatch.setattr(mss, "fetch_sfr_stock", boom)
        df = macro.collect_macro(start_date="2026-01", end_date="2026-02")
        assert df[df["指标代码"] == "SFR_STOCK"].empty

    def test_only_filter_skips_official_fetch(self):
        stock_map = {"2026-01": (449.11, 8.2)}
        df = macro.collect_macro(
            start_date="2026-01", end_date="2026-02",
            sfr_stock_map=stock_map, only={"M2"},
        )
        assert df[df["指标代码"] == "SFR_STOCK"].empty


class TestAbsoluteURL:
    def test_join_relative(self):
        assert mss._absolute("https://www.pbc.gov.cn/a/b/", "../c/d.htm") == "https://www.pbc.gov.cn/a/c/d.htm"
