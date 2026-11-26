"""前端 Web 页面自动化测试（Playwright + 真实浏览器）。

覆盖：页面初始化/全视图渲染、图1多序列 X 轴并集（2026-10 回归）、
看板日期区间联动、数据预览筛选、运维监控筛选。

用例使用临时 SQLite 与临时配置，不污染真实 output/，不触发任何采集/清理写操作。
需要 Playwright 与 Chromium；缺少任一依赖时自动跳过。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import pandas as pd
import pytest
import uvicorn

import storage
import web_app

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = pytest.mark.webui

LATEST_MONTH = "2026-10"
TRADE_YOY_LAST = 25.2571


def _seed_data(root: Path) -> tuple[Path, dict]:
    output = root / "output"
    output.mkdir(parents=True, exist_ok=True)
    db_path = output / "market_data.db"
    cfg = {
        "storage": {"backend": "sqlite", "db_path": str(db_path), "auto_export_on_collect": False},
        "output": {"dir": str(output / "exports"), "formats": ["csv", "excel"],
                   "filename_prefix": "market_data", "keep_snapshots": 3},
        "etf": {
            "codes": ["510050", "159915"],
            "listing_dates": {"510050": "2004-12-30", "159915": "2011-09-20"},
            "start_date": "", "end_date": "", "default_trade_days": 20,
        },
        "index": {"codes": ["000001"], "start_date": "", "end_date": "", "lookback_days": 365},
        "macro": {"start_date": "2025-01"},
        "monitor": {"history_file": str(output / "monitor" / "interface_runs.jsonl")},
        "scheduler": {"enabled": False, "time": "08:30", "run_type": "full", "macro_lookback_months": 2},
        "web": {"host": "127.0.0.1", "port": 8000},
    }
    cfg_path = root / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    storage.init_schema(db_path)
    months = ["2025-12"] + [f"2026-{m:02d}" for m in range(1, 11)]
    macro_rows = []
    for i, month in enumerate(months):
        for code, name, unit, value in [
            ("M2", "广义货币(M2)", "亿元", 3000000 + i * 10000),
            ("CPI", "居民消费价格指数CPI", "上年同月=100", 100.2),
            ("EXPORT", "出口额", "亿美元", 3000 + i * 100),
        ]:
            macro_rows.append({
                "指标代码": code, "指标名称": name, "日期": month, "数值": value,
                "单位": unit, "同比(%)": 5.0 + i * 0.1, "环比(%)": 0.2,
                "数据源": "测试源", "采集时间": "2026-11-26 10:00:00",
            })
    for month, value, yoy in [
        ("2025-12", 1401879.2, 5.0), ("2026-03", 334192.9, 5.0),
        ("2026-06", 695704.0, 4.7), ("2026-09", 1071071.2, 4.6),
    ]:
        macro_rows.append({
            "指标代码": "CN_GDP", "指标名称": "中国GDP(季度绝对值)", "日期": month,
            "数值": value, "单位": "亿元", "同比(%)": yoy, "环比(%)": None,
            "数据源": "国家统计局", "采集时间": "2026-11-26 10:00:00",
        })
    storage.upsert_macro(db_path, pd.DataFrame(macro_rows), "webui_test")

    trade_yoy_values = [3.2682, 16.1405, 21.3822, 18.2998, 18.3804, 19.2561,
                        21.215, 21.8514, 22.4402, 23.8539, TRADE_YOY_LAST]
    derived_rows = []
    for i, month in enumerate(months):
        derived_rows.append({
            "code": "TRADE_YOY", "name": "进出口同比", "date": month,
            "value": trade_yoy_values[i], "unit": "%", "formula": "累计进出口同比",
            "grp": "A", "source": "macro_observation", "computed_at": "2026-11-26 10:00:00",
        })
        for code, value in [("M2_YOY", 8.0 + i * 0.1), ("CPI_YOY", 0.2)]:
            derived_rows.append({
                "code": code, "name": code, "date": month, "value": value, "unit": "%",
                "formula": "test", "grp": "A", "source": "macro_observation",
                "computed_at": "2026-11-26 10:00:00",
            })
    storage.upsert_derived(db_path, pd.DataFrame(derived_rows))

    etf_rows = []
    for date, base in [("2026-10-23", 100.0), ("2026-10-26", 102.0)]:
        for code, name, nav, tag in [
            ("510050", "50ETF", 3.5, "汇金"), ("159915", "创业板ETF", 2.4, "黄金"),
        ]:
            etf_rows.append({
                "基金代码": code, "基金简称": name, "交易市场": "SH", "交易日期": date,
                "份额(亿份)": base, "单位净值(元)": nav, "规模(亿元)": base * nav,
                "数据源": "SSE+天天基金", "采集时间": "2026-11-26 10:00:00",
            })
    storage.upsert_etf(db_path, pd.DataFrame(etf_rows), "webui_test")
    storage.save_etf_config(db_path, {
        "codes": ["510050", "159915"],
        "listing_dates": {"510050": "2004-12-30", "159915": "2011-09-20"},
        "start_date": "", "end_date": "", "default_trade_days": 20,
        "tags": {"510050": ["汇金"], "159915": ["黄金"]},
    })
    storage.save_index_config(db_path, cfg["index"])

    trade_rows = []
    for date, amount in [("2026-10-23", 7000.0), ("2026-10-26", 7200.0)]:
        trade_rows.append({
            "trade_date": date, "total_amount": amount, "concentration5": 45.2,
            "margin_ratio": 265.0, "margin_financing_ratio": 22.5,
            "margin_balance": 18000.0, "financing_balance": 17500.0,
            "securities_balance": 500.0, "financing_to_float_mv": 2.3,
            "financing_buy_pct": 8.5, "debt_investors": 500000,
            "up_count": 2200, "down_count": 1800, "flat_count": 200,
            "source_set": "测试", "computed_at": "2026-11-26 10:00:00",
        })
    storage.upsert_trade(db_path, pd.DataFrame(trade_rows), "webui_test")

    storage.append_interface_runs(db_path, [
        {
            "run_id": "20261126_100000_full", "run_type": "full", "run_date": "2026-11-26",
            "run_time": "2026-11-26 10:00:00", "category": "宏观",
            "interface_key": "macro.m2", "interface_name": "货币供应量",
            "params": '{"code":"M2"}', "status": "success", "attempts": 1,
            "rows": 11, "error": "", "duration_sec": 0.5,
        },
        {
            "run_id": "20261126_100000_full", "run_type": "full", "run_date": "2026-11-26",
            "run_time": "2026-11-26 10:00:10", "category": "宏观",
            "interface_key": "macro.cpi", "interface_name": "CPI",
            "params": '{"code":"CPI"}', "status": "failed", "attempts": 3,
            "rows": 0, "error": "TimeoutError: 读超时", "duration_sec": 3.0,
        },
        {
            "run_id": "20261126_100000_full", "run_type": "full", "run_date": "2026-11-26",
            "run_time": "2026-11-26 10:00:20", "category": "ETF",
            "interface_key": "etf.sse_shares", "interface_name": "上交所份额",
            "params": '{"date":"2026-10-26"}', "status": "failed", "attempts": 2,
            "rows": 0, "error": "ValueError: 无数据", "duration_sec": 2.0,
        },
    ])
    return cfg_path, cfg


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """启动内存外的真实 FastAPI 服务（临时库），供浏览器访问。"""
    root = tmp_path_factory.mktemp("webui")
    cfg_path, _ = _seed_data(root)
    patch = pytest.MonkeyPatch()
    patch.setattr(web_app, "CONFIG_PATH", cfg_path)
    patch.setattr(web_app, "get_fund_name_map", lambda: {})

    server = uvicorn.Server(uvicorn.Config(
        web_app.app, host="127.0.0.1", port=0, log_level="error", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        patch.undo()
        pytest.fail("Web UI 测试服务启动超时")

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        patch.undo()


@pytest.fixture(scope="module")
def browser(live_server):
    executable = (shutil.which("chromium") or shutil.which("chromium-browser")
                  or shutil.which("google-chrome"))
    if not executable:
        pytest.skip("未找到 Chromium，跳过 Web UI 测试")
    pw = sync_playwright().start()
    try:
        driver = pw.chromium.launch(
            headless=True, executable_path=executable, args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    except Exception as exc:  # pragma: no cover - 环境差异
        pw.stop()
        pytest.skip(f"Chromium 启动失败：{exc}")
    try:
        yield driver
    finally:
        driver.close()
        pw.stop()


@pytest.fixture
def page(browser, live_server):
    tab = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors: list[str] = []
    tab.on("pageerror", lambda err: errors.append(str(err)))
    tab.goto(live_server, wait_until="domcontentloaded", timeout=30000)
    tab.wait_for_selector("#views .view.active", timeout=15000)
    tab.wait_for_timeout(300)
    tab._js_errors = errors  # type: ignore[attr-defined]
    try:
        yield tab
    finally:
        tab.close()


def _switch(page, view_id: str):
    page.evaluate("id => switchView(id)", view_id)
    page.wait_for_timeout(350)


def _chart(page, chart_id: str):
    page.wait_for_function(
        """id => {
          const card = document.querySelector(`[data-chart-card="${id}"]`);
          const dom = card && card.querySelector('.chart-box');
          return !!(dom && window.echarts && echarts.getInstanceByDom(dom));
        }""",
        arg=chart_id, timeout=15000,
    )
    return page.evaluate(
        """id => {
          const card = document.querySelector(`[data-chart-card="${id}"]`);
          const dom = card.querySelector('.chart-box');
          const opt = echarts.getInstanceByDom(dom).getOption();
          return {
            categories: (opt.xAxis[0].data || []).map(String),
            series: (opt.series || []).map(s => ({name: s.name, data: s.data})),
          };
        }""",
        arg=chart_id,
    )


class TestWebPage:
    def test_all_views_render_without_js_errors(self, page):
        """23 个视图均可渲染，切换过程中不产生未捕获 JS 异常。"""
        view_ids = page.evaluate("Object.keys(VIEWS)")
        assert len(view_ids) >= 20
        for view_id in view_ids:
            _switch(page, view_id)
        page.wait_for_timeout(500)
        assert page.locator("#views .view").count() >= 20
        assert page._js_errors == []

    def test_chart1_includes_latest_month_from_secondary_series(self, page):
        """回归：GDP 为季度序列，不能让进出口同比的最新月份没有 X 轴坐标。"""
        _switch(page, "dashmacro")
        chart = _chart(page, "gdp_trade")
        assert LATEST_MONTH in chart["categories"]

        # ECharts 的 series.data 已按 categories 对齐为一维数值数组。
        trade = next(s for s in chart["series"] if s["name"] == "进出口同比")
        pairs = dict(zip(chart["categories"], trade["data"]))
        assert pairs[LATEST_MONTH] == pytest.approx(TRADE_YOY_LAST, abs=1e-4)

        gdp = next(s for s in chart["series"] if s["name"] == "GDP同比")
        gdp_pairs = dict(zip(chart["categories"], gdp["data"]))
        assert gdp_pairs[LATEST_MONTH] is None  # 季度点不伪造月度值
        assert page._js_errors == []

    def test_dashboard_date_range_filter_updates_chart1(self, page):
        """看板日期区间真实回传后端并重绘图1。"""
        _switch(page, "dashmacro")
        view = page.locator("#v-dashmacro")
        view.locator("[data-range-start]").fill("2026-08-01")
        view.locator("[data-range-end]").fill("2026-10-31")
        view.locator("[data-range-end]").dispatch_event("change")
        page.wait_for_timeout(700)

        chart = _chart(page, "gdp_trade")
        assert chart["categories"]
        assert min(chart["categories"]) >= "2026-08"
        assert LATEST_MONTH in chart["categories"]
        assert page._js_errors == []

    def test_preview_filter_applies_code_and_date(self, page):
        """数据预览筛选按代码/日期重载表格。"""
        _switch(page, "prev-macro")
        page.wait_for_selector('#v-prev-macro [data-preview-table="macro"] tbody tr')
        bar = page.locator("#v-prev-macro [data-preview-filter]")
        bar.locator("[data-filter-code]").fill("M2")
        bar.locator("[data-filter-start]").fill("2025-01-01")
        bar.locator("[data-filter-end]").fill("2026-12-31")
        bar.locator("[data-filter-size]").select_option("100")
        bar.locator('[data-act="filter"]').click()
        page.wait_for_timeout(400)

        text = page.locator('#v-prev-macro [data-preview-table="macro"]').inner_text()
        assert "M2" in text
        assert "CPI" not in text
        assert "出口额" not in text
        assert page._js_errors == []

    def test_monitor_failure_filter_keeps_only_failed_rows(self, page):
        """运维监控失败明细按状态筛选生效。"""
        _switch(page, "mon-failures")
        page.wait_for_function("MONITOR_RAW.failures.length >= 2")
        bar = page.locator("#v-mon-failures [data-monitor-filter]")
        bar.locator("[data-mon-start]").fill("2026-01-01")
        bar.locator("[data-mon-end]").fill("2026-12-31")
        bar.locator("[data-mon-status]").select_option("failed")
        bar.locator('[data-act="monitor-filter"]').click()
        page.wait_for_function(
            "document.querySelectorAll('#v-mon-failures [data-monitor-table=\"failures\"] tbody tr').length === 2"
        )

        rows = page.locator('#v-mon-failures [data-monitor-table="failures"] tbody tr')
        assert rows.count() == 2
        assert page._js_errors == []
