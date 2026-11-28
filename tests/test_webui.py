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
            ("RMB_DEPOSIT", "人民币存款余额", "亿元", 100000 + (100 if i % 2 else 0)),
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
        for code, value in [
            ("M2_YOY", 8.0 + i * 0.1), ("CPI_YOY", 0.2),
            ("RMB_LOAN_NEW", 100.0 if i % 2 else -100.0),
            ("HH_DEPOSIT_NEW_YOY", 50.0 if i % 2 else -50.0),
            ("GOLD_NEW_TON", 2.0 if i % 2 else -2.0),
        ]:
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

    def test_trade_market_chart_uses_linked_scroll_and_wan_yi(self, page):
        """交易图1为上下双轴联动滚动，成交额按万亿元两位小数展示。"""
        _switch(page, "dashtrade")
        _chart(page, "trade_market")
        detail = page.evaluate(
            """id => {
              const card = document.querySelector(`[data-chart-card="${id}"]`);
              const dom = card.querySelector('.chart-box');
              const opt = echarts.getInstanceByDom(dom).getOption();
              return {
                xAxisCount: (opt.xAxis || []).length,
                dataZoomCount: (opt.dataZoom || []).length,
                zoomAxes: (opt.dataZoom || []).map(z => z.xAxisIndex),
                linked: (Array.isArray(opt.axisPointer) ? opt.axisPointer : [opt.axisPointer])
                  .some(p => p && p.link && p.link.length),
                amountAxis: (opt.yAxis || [])[0] && (opt.yAxis || [])[0].name,
                amountValues: ((opt.series || [])[0] || {}).data || [],
                markLines: (opt.series || [])
                  .filter(s => /集中度/.test(s.name || ''))
                  .flatMap(s => ((s.markLine || {}).data || []).map(d => ({
                    y: d.yAxis, color: d.lineStyle && d.lineStyle.color,
                  }))),
                chartHeight: dom.getBoundingClientRect().height,
                tmTall: dom.classList.contains('tm-tall'),
              };
            }""",
            arg="trade_market",
        )
        assert detail["xAxisCount"] == 2
        assert detail["dataZoomCount"] == 2
        assert detail["zoomAxes"] == [[0, 1], [0, 1]]
        assert detail["linked"] is True
        assert "万亿元" in detail["amountAxis"]
        assert all(v is None or round(float(v), 2) == float(v) for v in detail["amountValues"])
        # 40% 分界 + 50% 红色警戒线
        marks = {m["y"]: m["color"] for m in detail["markLines"]}
        assert 40 in marks and 50 in marks
        assert marks[50].lower() == "#e5484d"
        assert detail["tmTall"] is True
        assert detail["chartHeight"] >= 500
        assert page._js_errors == []

    def test_etf_tag_config_modal_multi_select(self, page):
        """ETF 配置：无重复 chips 行；配置弹窗多选取代旧的编辑标签。"""
        _switch(page, "etfcfg")
        # 旧的重复 chips 行已删除
        assert page.locator('#v-etfcfg [data-chips="etf"]').count() == 0
        # 每行都有「配置标签」入口
        edit_btns = page.locator('#v-etfcfg [data-act="etf-tag-edit"]')
        assert edit_btns.count() == 2

        edit_btns.first.click()
        page.wait_for_selector('#modalRoot .etf-tag-picker input[type="checkbox"]')
        opts = page.locator('#modalRoot .etf-tag-picker input[type="checkbox"]')
        values = opts.evaluate_all("els => els.map(e => e.value)")
        assert values == ["汇金", "上证50", "沪深300", "中证A500", "中证1000", "科创50", "创业板", "黄金", "宽基"]
        # 多选：保留已勾选的汇金，再勾沪深300
        page.locator('#modalRoot .etf-tag-picker input[value="沪深300"]').check()
        page.locator('#modalRoot [data-act="modal-ok"]').click()
        page.wait_for_timeout(600)

        first_row_tags = page.locator('#v-etfcfg tbody tr').first.locator("td").nth(2).inner_text()
        assert "汇金" in first_row_tags and "沪深300" in first_row_tags
        assert page._js_errors == []

    def test_etf_add_modal_persists_all_fields(self, page):
        """新增 ETF 弹窗一次填齐代码/名称/标签/上市日期并写入表格。"""
        _switch(page, "etfcfg")
        before = page.locator('#v-etfcfg tbody tr').count()
        page.locator('#v-etfcfg [data-act="etf-add"]').click()
        page.wait_for_selector('#modalRoot .etf-add-form')
        page.locator('#modalRoot [data-add-code]').fill("159300")
        page.locator('#modalRoot [data-add-name]').fill("沪深300ETF测试")
        page.locator('#modalRoot [data-add-listing]').fill("2024-05-10")
        page.locator('#modalRoot .etf-tag-picker input[value="沪深300"]').check()
        page.locator('#modalRoot .etf-tag-picker input[value="汇金"]').check()
        page.locator('#modalRoot [data-act="modal-ok"]').click()
        page.wait_for_timeout(700)

        rows = page.locator('#v-etfcfg tbody tr')
        assert rows.count() == before + 1
        added = rows.last
        cells = [added.locator("td").nth(i).inner_text() for i in range(4)]
        assert cells[0] == "159300"
        assert cells[1] == "沪深300ETF测试"
        assert "汇金" in cells[2] and "沪深300" in cells[2]
        assert cells[3] == "2024-05-10"
        assert page._js_errors == []

    def test_macro_config_add_uses_modal_form(self, page):
        """宏观指标配置：新增改为弹窗表单填写。"""
        _switch(page, "maccfg")
        before = page.locator('#v-maccfg tbody tr').count()
        # 旧的 inline 输入已移除，改为按钮
        assert page.locator('#v-maccfg [data-macro-code]').count() == 0
        page.locator('#v-maccfg [data-act="macro-add"]').click()
        page.wait_for_selector('#modalRoot .etf-add-form')
        page.locator('#modalRoot [data-m-code]').fill("TEST_YOY")
        page.locator('#modalRoot [data-m-name]').fill("测试同比")
        page.locator('#modalRoot [data-m-cat]').fill("测试类")
        page.locator('#modalRoot [data-m-src]').fill("手工")
        page.locator('#modalRoot [data-m-freq]').fill("月")
        page.locator('#modalRoot [data-act="modal-ok"]').click()
        page.wait_for_timeout(400)
        rows = page.locator('#v-maccfg tbody tr')
        assert rows.count() == before + 1
        added = rows.last.inner_text()
        assert "TEST_YOY" in added and "测试同比" in added
        assert page._js_errors == []

    def test_index_config_chips_removed_and_modal_add(self, page):
        """指数配置：删除 chips 行，新增改为弹窗表单。"""
        _switch(page, "idxcfg")
        assert page.locator('#v-idxcfg [data-chips="idx"]').count() == 0
        before = page.locator('#v-idxcfg tbody tr').count()
        page.locator('#v-idxcfg [data-act="idx-add"]').click()
        page.wait_for_selector('#modalRoot .etf-add-form')
        # 用预设下拉快速填充
        page.locator('#modalRoot [data-idx-preset]').select_option("000688|科创50")
        preset_code = page.locator('#modalRoot [data-i-code]').input_value()
        assert preset_code == "000688"
        page.locator('#modalRoot [data-act="modal-ok"]').click()
        page.wait_for_timeout(600)
        rows = page.locator('#v-idxcfg tbody tr')
        assert rows.count() == before + 1
        assert any("000688" in t for t in rows.all_inner_texts())
        assert page._js_errors == []

    def test_etf_config_pagination_and_query(self, page):
        """ETF 配置：分页展示 + 代码/名称关键字与标签多条件查询。"""
        page.evaluate("() => { ETF_CFG_STATE.keyword=''; ETF_CFG_STATE.tag=''; ETF_CFG_STATE.page=1; }")
        _switch(page, "etfcfg")
        page.wait_for_selector('#v-etfcfg [data-etfcfg-table] tbody tr')
        # 采集区间展示来自 /api/schedule-windows，不再显示配置里的旧日期
        window_text = page.locator("#v-etfcfg [data-etf-window]").inner_text().strip()
        expected = page.evaluate("() => `${SCHED_WINDOWS.etf.start} ~ ${SCHED_WINDOWS.etf.end}`")
        assert window_text == expected
        total = page.evaluate("(ETF_CFG.items||[]).length")
        # 默认展示：size=10 时全部标的在一页内
        assert page.locator('#v-etfcfg [data-etfcfg-table] tbody tr').count() == min(total, 10)
        assert page.locator('#v-etfcfg [data-etfcfg-pager] [data-pager]').count() == 1
        # 关键字查询：510050
        page.locator('#v-etfcfg [data-etfcfg-kw]').fill("510050")
        page.locator('#v-etfcfg [data-act="etfcfg-filter"]').click()
        page.wait_for_timeout(200)
        rows = page.locator('#v-etfcfg [data-etfcfg-table] tbody tr')
        assert rows.count() == 1
        assert "510050" in rows.first.inner_text()
        # 重置后按标签「黄金」查询（159915 为黄金）
        page.locator('#v-etfcfg [data-act="etfcfg-reset"]').click()
        page.locator('#v-etfcfg [data-etfcfg-tag]').select_option("黄金")
        page.locator('#v-etfcfg [data-act="etfcfg-filter"]').click()
        page.wait_for_timeout(200)
        rows = page.locator('#v-etfcfg [data-etfcfg-table] tbody tr')
        assert rows.count() == 1
        assert "159915" in rows.first.inner_text()
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

    def test_monitor_default_range_includes_today(self, page):
        """运维监控默认日期上限为今天，当日运行记录不能被最近交易日截掉。"""
        _switch(page, "mon-detail")
        bar = page.locator("#v-mon-detail [data-monitor-filter]")
        today = page.evaluate("TODAY")
        assert bar.locator("[data-mon-end]").input_value() == today
        page.evaluate("""d => {
          MONITOR_RAW.detail.push({
            '运行日期': d, '运行时间': d + ' 21:18:36', '运行ID': 'today_probe',
            '运行类型': 'interface', '类别': '交易', '接口键': 'trade.market_breadth',
            '接口名称': '市场宽度', '调用参数': '{}', '状态': 'success',
            '尝试次数': 1, '数据行数': 0, '失败原因': '', '耗时(秒)': 0.1,
          });
          renderMonitorTable('detail');
        }""", today)
        body = page.locator('#v-mon-detail [data-monitor-table="detail"] tbody').inner_text()
        assert "today_probe" in body
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
        headers = page.locator('#v-mon-failures [data-monitor-table="failures"] thead th').all_inner_texts()
        assert "运行时间" in headers
        assert page._js_errors == []

    def test_log_line_uses_record_time(self, page):
        """实时日志时间用日志记录时间，不用浏览器当前时间。"""
        _switch(page, "console")
        page.evaluate("""() => {
          document.getElementById('logBody').innerHTML = '';
          appendLog('带记录时间的日志', 'INFO', '03:17:42');
        }""")
        first = page.locator("#logBody .log-line").last
        assert "03:17:42" in first.locator(".t").inner_text()
        assert page._js_errors == []

    def test_live_log_panel_scope_and_dom_limit(self, page):
        """实时日志面板：标注 10000 行/15M 落盘滚动，前端最多保留 10000 行。"""
        _switch(page, "console")
        assert "最近10000行" in page.locator("#v-console .log-scope").inner_text()
        assert "落盘15M滚动" in page.locator("#v-console .log-scope").inner_text()
        page.evaluate("""() => {
          const b = document.getElementById('logBody');
          b.innerHTML = Array.from({length:10005},(_,i)=>`<div class="log-line">line-${i}</div>`).join('');
          trimLogBody();
        }""")
        assert page.locator("#logBody .log-line").count() == 10000
        assert page._js_errors == []

    def test_console_shows_two_category_summary_cards(self, page):
        """采集控制台展示两张分类汇总卡片：存量汇总 / 当日净新增，各含宏观/指数/ETF/交易。"""
        _switch(page, "console")
        cards = page.locator("#v-console .summary-card")
        assert cards.count() == 2
        titles = cards.locator(".summary-title").all_inner_texts()
        assert any("采集数量汇总" in t for t in titles)
        assert any("当日净新增汇总" in t for t in titles)
        # 每张卡片都应含四个类别小计
        for i in range(cards.count()):
            labels = cards.nth(i).locator(".summary-cell-lbl").all_inner_texts()
            assert labels == ["宏观", "指数", "ETF", "交易"]
        # 第二张卡片取今日真实新增入库行数（new_row_log），不是接口返回行数累计
        net = page.evaluate("S.today_new")
        vals2 = cards.nth(1).locator(".summary-cell-val").all_inner_texts()
        assert vals2[0].replace(",", "") == str(int(net.get("宏观", 0)))
        subs = cards.locator(".summary-sub").all_inner_texts()
        assert all("写入累计" not in s and "仅对账" not in s for s in subs)
        assert any("幂等去重" in s for s in subs)
        assert page._js_errors == []

    def test_summary_cards_refresh_latest_status(self, page):
        """刷新卡片时使用最新 /api/status 的净新增值，不再显示写入累计。"""
        _switch(page, "console")
        status = page.evaluate("JSON.parse(JSON.stringify(S))")
        status["today_new"] = {"宏观": 7, "指数": 2, "ETF": 3, "交易": 1}
        status["today_written"] = {"宏观": 999999}
        status["server_time"] = "2026-11-27 15:00:00"
        body = json.dumps(status, ensure_ascii=False)
        pattern = "**/api/status*"
        page.route(pattern, lambda route: route.fulfill(
            status=200, content_type="application/json", body=body,
        ))
        try:
            page.evaluate("refreshSummaryCards()")
            page.wait_for_timeout(250)
            cards = page.locator("#v-console .summary-card")
            vals = cards.nth(1).locator(".summary-cell-val").all_inner_texts()
            assert [v.replace(",", "") for v in vals] == ["7", "2", "3", "1"]
            sub = cards.nth(1).locator(".summary-sub").inner_text()
            assert "写入累计" not in sub and "仅对账" not in sub
            assert "幂等去重" in sub
            assert page._js_errors == []
        finally:
            page.unroute(pattern)

    def test_api_requests_disable_browser_cache(self):
        """所有 API 请求禁用浏览器缓存，避免状态卡片拿到旧值。"""
        src = (Path(__file__).parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        assert "fetch(url, {...opts, cache:'no-store'})" in src

    def test_monitor_stats_dual_dimension_filter(self, page):
        """采集量统计按维度1(类别)+维度2(细分口径)两级筛选。"""
        _switch(page, "mon-stats")
        # 注入真实结构的 stats 行（类别口径 / 细分口径 / 名称）
        page.evaluate("""() => {
          MONITOR_RAW.stats = [
            {'类别口径':'宏观','细分口径':'M1_YOY','名称':'M1同比','运行日期':'2026-11-25'},
            {'类别口径':'宏观','细分口径':'M2_YOY','名称':'M2同比','运行日期':'2026-11-25'},
            {'类别口径':'ETF','细分口径':'510050','名称':'上证50ETF','运行日期':'2026-11-25'},
            {'类别口径':'ETF','细分口径':'159915','名称':'创业板ETF','运行日期':'2026-11-25'},
          ];
          MONITOR_STATE.stats = {page:1,size:25,keyword:'',run_type:'',category:'',subcode:'',status:'',start:'',end:''};
          rerenderView('mon-stats');
        }""")
        assert page.locator("#v-mon-stats [data-stat-dim1]").count() == 1
        assert page.locator("#v-mon-stats [data-mon-subcode]").count() == 1
        # 维度1=ETF → 维度2 应级联出两个 ETF 细分口径
        page.locator("#v-mon-stats [data-stat-dim1]").select_option("ETF")
        opts = page.locator("#v-mon-stats [data-mon-subcode] option").all_inner_texts()
        assert any("510050" in o for o in opts)
        assert any("159915" in o for o in opts)
        assert not any("M1_YOY" in o for o in opts)
        # 选择维度2=510050 并筛选，表格只剩对应行
        page.locator("#v-mon-stats [data-mon-subcode]").select_option("510050")
        page.locator('#v-mon-stats [data-act="monitor-filter"]').click()
        page.wait_for_timeout(150)
        body = page.locator('#v-mon-stats [data-monitor-table="stats"] tbody').inner_text()
        assert "510050" in body
        assert "159915" not in body
        assert "M1同比" not in body
        assert page._js_errors == []

    def test_sidebar_footer_pinned_at_bottom(self, page):
        """侧栏底部展示生产系统版权信息，并通过 flex 固定在侧栏底部。"""
        _switch(page, "console")
        foot = page.locator("#sidebar .sidebar-foot")
        assert foot.count() == 1
        text = foot.inner_text()
        assert "投研大盘数据平台" in text
        assert "生产系统" in text
        assert "2026 投研数据团队" in text
        layout = page.evaluate("""() => {
          const side = document.querySelector('#sidebar');
          const foot = side.querySelector('.sidebar-foot');
          const cs = getComputedStyle(side);
          return {
            display: cs.display, direction: cs.flexDirection,
            marginTop: parseFloat(getComputedStyle(foot).marginTop),
            gapToBottom: side.getBoundingClientRect().bottom - foot.getBoundingClientRect().bottom,
          };
        }""")
        assert layout["display"] == "flex"
        assert layout["direction"] == "column"
        # flex + margin-top:auto 让 footer 贴底：与侧栏底部距离接近 0，且上方有较大留白
        assert layout["gapToBottom"] < 2
        assert layout["marginTop"] > 0
        assert page._js_errors == []

    def test_console_trade_count_uses_raw_tables_sum(self, page):
        """采集数量汇总卡片的交易数=5张交易原始表行数之和。"""
        _switch(page, "console")
        page.evaluate("""() => {
          S.trade_rows = 2890;
          S.trade_raw_rows = {exchange:100, margin:200, snapshot:500, breadth:300, congestion:400};
          rerenderView('console');
        }""")
        cards = page.locator("#v-console .summary-card")
        first = cards.first
        labels = first.locator(".summary-cell-lbl").all_inner_texts()
        vals = first.locator(".summary-cell-val").all_inner_texts()
        idx = labels.index("交易")
        # 100+200+500+300+400=1500，而非 trade_rows(2890)
        assert vals[idx].replace(",", "") == "1500"
        assert page._js_errors == []

    def test_trade_config_page_add_remove_sources(self, page):
        """交易参数配置页：区间默认最近交易日、6类源目录、弹窗新增与删除。"""
        _switch(page, "tradecfg")
        assert page.locator("#v-tradecfg [data-act=\"trade-add\"]").count() == 1
        assert page.locator("#v-tradecfg [data-act=\"save-trade-config\"]").count() == 1
        # 采集区间由 /api/schedule-windows 动态给出：最近已结束交易日，不再读配置日期
        window_text = page.locator("#v-tradecfg [data-trade-window]").inner_text().strip()
        last = page.evaluate("LAST")
        assert window_text == f"{last} ~ {last}"
        # catalog 参考表应有 6 类源（含 breadth/congestion/risk_premium）
        cat_text = page.locator("#v-tradecfg").inner_text()
        for code in ("exchange", "margin", "snapshot", "breadth", "congestion", "risk_premium"):
            assert code in cat_text
        initial_rows = page.locator("#v-tradecfg [data-trade-src-table] tbody tr").count()
        # 弹窗填写新增：选择目录预设后代码/名称等自动带出
        page.locator('#v-tradecfg [data-act="trade-add"]').click()
        modal = page.locator("#modalRoot .modal-box")
        assert modal.locator("[data-t-code]").count() == 1
        modal.locator("[data-t-preset]").select_option("breadth")
        assert modal.locator("[data-t-code]").input_value() == "breadth"
        assert modal.locator("[data-t-name]").input_value()
        modal.locator('[data-act="modal-ok"]').click()
        page.wait_for_timeout(150)
        assert page.locator("#v-tradecfg [data-trade-src-table] tbody tr").count() == initial_rows + 1
        # 保存并复核后端已落库
        page.locator('#v-tradecfg [data-act="save-trade-config"]').click()
        page.wait_for_timeout(250)
        saved = page.evaluate("() => TRADE_CFG.sources")
        assert "breadth" in saved
        # 删除刚加的源
        page.locator('#v-tradecfg [data-act="trade-src-remove"][data-code="breadth"]').click()
        page.wait_for_timeout(150)
        assert page.locator("#v-tradecfg [data-trade-src-table] tbody tr").count() == initial_rows
        assert page._js_errors == []


class TestDashboardNewViews:
    """估值导航页与 ETF 维度标签切换（空数据也应能渲染，无 JS 异常）。"""

    def test_valuation_nav_and_cards(self, page):
        assert "dashval" in page.evaluate("Object.keys(VIEWS)")
        _switch(page, "dashval")
        view = page.locator("#v-dashval")
        assert view.locator("[data-val-summary] .stat-card").count() == 4
        assert view.locator('[data-chart-card="val_fed_band"]').count() == 1
        assert view.locator('[data-chart-card="val_fed_percentile"]').count() == 1
        assert page._js_errors == []

    def test_valuation_nav_icon_exists(self, page):
        """估值导航图标必须使用 CSS 中真实存在的 ti-gauge，不能是无效的 chart-gauge。"""
        icon_ok = page.evaluate("""() => {
          const item = NAV.dashboard.groups.flatMap(g => g.items).find(x => x.id === 'dashval');
          const cls = item ? 'ti-' + item.icon : '';
          const probe = document.createElement('i');
          probe.className = cls;
          document.body.appendChild(probe);
          const content = getComputedStyle(probe, '::before').content;
          probe.remove();
          return {icon: cls, ok: cls === 'ti-gauge' && content && content !== 'none' && content !== 'normal'};
        }""")
        assert icon_ok["icon"] == "ti-gauge"
        assert icon_ok["ok"] is True

    def test_trade_view_has_no_risk_chart(self, page):
        """风险溢价率图3已迁至估值维度，日交易下只保留图1/图2。"""
        _switch(page, "dashtrade")
        view = page.locator("#v-dashtrade")
        assert view.locator('[data-chart-card="trade_market"]').count() == 1
        assert view.locator('[data-chart-card="trade_margin"]').count() == 1
        assert view.locator('[data-chart-card="trade_risk"]').count() == 0
        assert page._js_errors == []

    def test_etf_dim_tab_click_before_data_load(self, page):
        """数据尚未加载时点击宽基：切换立即生效，随后渲染不出现汇金图。"""
        page.evaluate("() => { switchMod('dash'); switchView('dashetf'); }")
        # 不等待 /api/dashboard 返回，立即点击宽基标签（旧实现此时尚未绑定点击处理器）
        page.locator('#v-dashetf [data-etf-dim="broad"]').click()
        assert page.evaluate("etfDim") == "broad"
        assert page.locator('#v-dashetf .etf-dim-tab.active').get_attribute("data-etf-dim") == "broad"
        # 数据到达后按新维度渲染，汇金图不得泄漏到宽基
        page.wait_for_timeout(1500)
        assert page.locator('#v-dashetf [data-chart-card="etf_huijin"]').count() == 0
        assert page.locator('#v-dashetf [data-chart-card="etf_huijin_detail"]').count() == 0
        assert page.locator('#v-dashetf [data-chart-card="etf_broad"]').count() == 1
        assert page._js_errors == []

    def test_etf_dim_tabs_switch_charts(self, page):
        _switch(page, "dashetf")
        view = page.locator("#v-dashetf")
        assert view.locator(".etf-dim-tab").count() == 3
        # 默认汇金：应显示汇金图，不显示宽基专属图；每日变化累计图已移除
        assert view.locator('[data-chart-card="etf_huijin"]').count() == 1
        assert view.locator('[data-chart-card="etf_huijin_cumulative"]').count() == 0
        assert view.locator('[data-chart-card="etf_kc50_top5"]').count() == 0
        # 切到宽基：主图 + 六大宽基各 2 图，六组图各自并作一排
        view.locator('[data-etf-dim="broad"]').click()
        page.wait_for_timeout(300)
        assert view.locator('[data-chart-card="etf_broad"]').count() == 1
        assert view.locator('[data-chart-card="etf_kc50_top5"]').count() == 1
        assert view.locator('[data-chart-card="etf_kc50_stock"]').count() == 1
        assert view.locator('[data-chart-card="etf_huijin"]').count() == 0
        assert view.locator(".chart-row-pair").count() == 6
        assert view.locator(".chart-row-pair [data-chart-card]").count() == 12
        # 每组并排：合计图在左、Top5 柱状排名图在右
        pair_order = view.locator(".chart-row-pair").first.locator("[data-chart-card]").evaluate_all(
            "els=>els.map(e=>e.dataset.chartCard)")
        assert pair_order[0].endswith("_stock") and pair_order[1].endswith("_top5")
        # 切回黄金：主图 + 累计图（整行）
        view.locator('[data-etf-dim="gold"]').click()
        page.wait_for_timeout(300)
        assert view.locator('[data-chart-card="etf_gold"]').count() == 1
        assert view.locator('[data-chart-card="etf_gold_cumulative"]').count() == 1
        assert view.locator('[data-chart-card="etf_gold_cumulative"].wide-card').count() == 1
        assert view.locator('[data-chart-card="etf_broad"]').count() == 0
        assert page._js_errors == []

    def test_macro_nav_country_split_and_us_charts(self, page):
        """左侧导航只保留「宏观指标」；顶部中国/美国标签切到美国渲染 3 张 FRED 图。

        回归：静态资源已更新但后端进程未重启时，顶部标签会出现却没有 macro_us 数据。
        必须从左侧导航真实点击进入，覆盖 switchView 程序化调用无法发现的部署时序问题。
        """
        nav = page.evaluate("""() => NAV.dashboard.groups.flatMap(g => g.items)
          .map(x => ({id:x.id, name:x.name}))""")
        assert nav == [
            {"id": "dashmacro", "name": "宏观指标"},
            {"id": "dashetf", "name": "ETF 维度"},
            {"id": "dashtrade", "name": "日交易"},
            {"id": "dashval", "name": "估值"},
        ]
        page.locator('.mod[data-mod="dashboard"]').click()
        with page.expect_response(lambda r: "/api/dashboard" in r.url, timeout=30000) as resp_info:
            page.locator('.fn[data-view="dashmacro"]').click()
        assert resp_info.value.status == 200
        assert [c["id"] for c in resp_info.value.json().get("macro_us", {}).get("charts", [])] == [
            "us_gdp", "us_prices", "us_rates"
        ]
        with page.expect_response(lambda r: "/api/dashboard" in r.url, timeout=30000) as us_resp:
            page.locator('#v-dashmacro .macro-dim-tab[data-macro-dim="macro_us"]').click()
        assert us_resp.value.status == 200
        page.wait_for_timeout(500)
        assert page.evaluate("document.querySelector('.view.active')?.id") == "v-dashmacro_us"
        # 切到美国后左侧导航仍统一高亮「宏观指标」
        assert page.evaluate("document.querySelector('.fn.active')?.dataset.view") == "dashmacro"
        view = page.locator("#v-dashmacro_us")
        assert view.locator("[data-chart-card]").count() == 3
        assert view.locator('[data-chart-card="us_gdp"]').count() == 1
        assert view.locator('[data-chart-card="us_prices"]').count() == 1
        assert view.locator('[data-chart-card="us_rates"]').count() == 1
        assert "FRED" in view.inner_text()
        assert page._js_errors == []

    def test_macro_top_tabs_switch_country(self, page):
        """宏观页面顶部中国/美国标签：形式同 ETF 维度标签，点击切换对应视图。"""
        page.locator('.mod[data-mod="dashboard"]').click()
        _switch(page, "dashmacro")
        cn = page.locator("#v-dashmacro")
        us = page.locator("#v-dashmacro_us")
        assert cn.locator(".macro-dim-tab").count() == 2
        assert us.locator(".macro-dim-tab").count() == 2
        assert [b.inner_text() for b in cn.locator(".macro-dim-tab").all()] == ["中国", "美国"]
        assert "active" in (cn.locator('[data-macro-dim="macro"]').get_attribute("class") or "")
        assert "active" in (us.locator('[data-macro-dim="macro_us"]').get_attribute("class") or "")

        with page.expect_response(lambda r: "/api/dashboard" in r.url, timeout=30000) as resp_info:
            cn.locator('[data-macro-dim="macro_us"]').click()
        assert resp_info.value.status == 200
        page.wait_for_timeout(500)
        assert page.evaluate("document.querySelector('.view.active')?.id") == "v-dashmacro_us"
        assert page.evaluate("document.querySelector('.fn.active')?.dataset.view") == "dashmacro"
        assert us.locator("[data-chart-card]").count() == 3
        assert "active" in (us.locator('[data-macro-dim="macro_us"]').get_attribute("class") or "")

        with page.expect_response(lambda r: "/api/dashboard" in r.url, timeout=30000) as resp_info:
            us.locator('[data-macro-dim="macro"]').click()
        assert resp_info.value.status == 200
        page.wait_for_timeout(500)
        assert page.evaluate("document.querySelector('.view.active')?.id") == "v-dashmacro"
        assert cn.locator("[data-chart-card]").count() > 0
        assert "active" in (cn.locator('[data-macro-dim="macro"]').get_attribute("class") or "")
        assert page._js_errors == []

    def test_macro_signed_bars_and_deposit_chart(self, page):
        """图3/图6/图7/图8 增量柱状图正值红、负值绿；图8 为人民币存款当月新增。"""
        page.locator('.mod[data-mod="dashboard"]').click()
        _switch(page, "dashmacro")
        view = page.locator("#v-dashmacro")
        assert "人民币存款当月新增" in view.inner_text()
        assert "人民币贷款当月新增" not in view.locator('[data-chart-card="loan_new_chart"]').inner_text()
        names = {
            "credit": "人民币贷款当月新增", "hh_deposit": "居民存款同比多增",
            "gold": "黄金当月新增", "loan_new_chart": "人民币存款当月新增",
        }
        for chart_id, series_name in names.items():
            chart = _chart(page, chart_id)
            series = next(s for s in chart["series"] if s["name"] == series_name)
            colored = [p for p in series["data"] if isinstance(p, dict)]
            colors = {p.get("itemStyle", {}).get("color") for p in colored}
            assert "#ff6b6b" in colors and "#35c98b" in colors, chart_id
            values = [p.get("value") for p in colored]
            assert any(v > 0 for v in values) and any(v < 0 for v in values), chart_id
        assert page._js_errors == []

    def test_gold_primary_chart_wide(self, page):
        """黄金维度份额变化/申赎净流入图占满一行。"""
        _switch(page, "dashetf")
        view = page.locator("#v-dashetf")
        view.locator('[data-etf-dim="gold"]').click()
        page.wait_for_timeout(400)
        gold = view.locator('[data-chart-card="etf_gold"]')
        assert gold.count() == 1
        assert "wide-card" in (gold.get_attribute("class") or "").split()
        assert view.locator('[data-chart-card="etf_gold_cumulative"]').count() == 1
        assert page._js_errors == []

    def test_broad_rank_detail_collapse_and_pagination(self, page):
        """宽基 Top5 柱状图明细：可收起，分页每页 10 行；六组共用同一实现。"""
        _switch(page, "dashetf")
        view = page.locator("#v-dashetf")
        view.locator('[data-etf-dim="broad"]').click()
        page.wait_for_timeout(400)
        assert view.locator('[data-detail-for$="_top5"]').count() == 6
        panel = view.locator('[data-detail-for="etf_hs300_top5"]')
        assert panel.is_hidden()

        # 24 条模拟数据 -> 3 页，每页 10 行；只拦截单 ETF 每日明细接口
        rows = [{
            "交易日期": f"2026-09-{24 - i:02d}",
            "净流入(亿元)": 10.0 + i,
            "份额(亿份)": 100.0 + i,
            "份额变化(亿份)": 0.5 + i,
            "份额变化率(%)": 1.0 + i / 10,
        } for i in range(24)]

        def _route_detail(route):
            query = route.request.url.split("?", 1)[1] if "?" in route.request.url else ""
            params = dict(x.split("=", 1) for x in query.split("&") if "=" in x)
            page_no = int(params.get("page", "1"))
            size = int(params.get("page_size", "10"))
            offset = (page_no - 1) * size
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "total": len(rows), "pages": (len(rows) + size - 1) // size,
                "page": page_no, "page_size": size, "rows": rows[offset:offset + size],
            }, ensure_ascii=False))

        page.route("**/api/data/etf?*", _route_detail)
        page.evaluate("""() => {
          const chart = {id:'etf_hs300_top5', dim:'broad', detail_start:'2026-09-01',
                         detail_end:'2026-09-30', rank_meta:[{code:'510300', name:'沪深300ETF测试'}]};
          return etfOpenBroadRankDetail(chart, chart.rank_meta[0]);
        }""")
        panel.wait_for(state="visible")
        page.wait_for_function("""() => {
          const p = document.querySelector('[data-detail-for="etf_hs300_top5"]');
          return p && p.querySelectorAll('tbody tr').length === 10 && p.textContent.includes('每页 10 行');
        }""")
        assert panel.locator('[data-act="etf-rank-close"]').inner_text().strip() == "收起"
        assert "第 1/3 页" in panel.inner_text()
        first_date = panel.locator("tbody tr").first.locator("td").first.inner_text()

        panel.locator('[data-act="etf-rank-next"]').click()
        page.wait_for_function("""() => document.querySelector('[data-detail-for="etf_hs300_top5"]')
          .textContent.includes('第 2/3 页')""")
        assert panel.locator("tbody tr").count() == 10
        assert panel.locator("tbody tr").first.locator("td").first.inner_text() != first_date

        panel.locator('[data-act="etf-rank-prev"]').click()
        page.wait_for_function("""() => document.querySelector('[data-detail-for="etf_hs300_top5"]')
          .textContent.includes('第 1/3 页')""")
        # 同一根柱子的元数据再次触发即收起，第三次触发重新展开
        page.evaluate("""() => {
          const chart = {id:'etf_hs300_top5', dim:'broad', detail_start:'2026-09-01',
                         detail_end:'2026-09-30', rank_meta:[{code:'510300', name:'沪深300ETF测试'}]};
          return etfOpenBroadRankDetail(chart, chart.rank_meta[0]);
        }""")
        page.wait_for_timeout(150)
        assert panel.is_hidden()
        page.evaluate("""() => {
          const chart = {id:'etf_hs300_top5', dim:'broad', detail_start:'2026-09-01',
                         detail_end:'2026-09-30', rank_meta:[{code:'510300', name:'沪深300ETF测试'}]};
          return etfOpenBroadRankDetail(chart, chart.rank_meta[0]);
        }""")
        panel.wait_for(state="visible")
        panel.locator('[data-act="etf-rank-close"]').click()
        assert panel.is_hidden()
        assert page._js_errors == []
