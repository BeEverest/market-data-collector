# 修复说明与回归清单 — 遗留1：深交所 ETF 份额接口失败

> 关联事项：`rpxpqJ`（交接事项）遗留1　|　修复批次：`fix/szse-throttle`　|　日期：2026-11-22
> 涉及的四项改动：① 控制并发/调低频次　② 深交所接口失败修复　③ 失败指出具体标的　④ 失败原因补充响应消息

## 一、根因定位（基于实测证据，非臆断）

对 `fund.szse.cn/api/report/ShowReport` 做了对照实测：

| 实验 | 结果 |
| --- | --- |
| 首次裸请求（仅 UA + Referer，无 Cookie） | **HTTP 200，解析 6948 行，0.7s** |
| 紧接着第二次请求（含先访问页面取 Cookie） | `RemoteDisconnected('Remote end closed...')` |
| 连续无间隔 3 次 / 间隔 4 秒 3 次 | 全部 `RemoteDisconnected` |

**结论**：接口本身可用、现有 headers 足够、无需 Cookie。失败根因是**短时间密集请求触发深交所反爬断连**。
因此交接文档里「补全 headers / 改用备用源」的猜测不成立；备用源也实测不可用（东财 `fund_etf_spot_em` 同样被断连，`fund_etf_fund_info_em` 列数不匹配），故不引入。

## 二、改动内容

### 1. 控制并发 + 调低频次
- **Web 层**（已有）：`web_app.py` 的 `_run_lock` 保证同一时刻只有一个采集/重跑任务，避免并发写库。本次未改动。
- **请求层**（新增）：新增 `collectors/rate_limit.py`，进程内线程安全的单例节流器：
  - 相邻外部请求最小间隔 `min_interval`（config 默认 **1.2s**）
  - 叠加 `jitter` 随机抖动（默认 **0.5s**），规避机械节律被识别
  - 默认 `min_interval=0`，不配置时完全兼容测试与本地 Mock
  - 在 `main._collect` / `_retry_failed` 按 `config.fetch` 启用
- **重试退避**：ETF 三个接口的失败重试由固定 `retry_interval` 改为**指数退避** `retry_interval × 2^(attempt-1)`。

### 2. 深交所接口失败修复
- 在 `fetch_szse_shares` / `fetch_sse_shares` / `fetch_nav` / `get_listing_dates` 每次请求前调用节流器 `throttle_wait()`。
- 本质是把请求节律降到反爬阈值以下；单次请求已验证可用。

### 3. 失败指出具体标的
- `fetch_szse_shares` / `fetch_sse_shares` 新增 `target_codes` 参数；`collect_etf` 按代码前缀拆分沪市（5/6 开头）与深市（1/8 开头）标的传入。
- 失败记录的 `error` 与 `params` 均带 `受影响标的=<代码列表>`；净值失败带单只代码。
- 实测：真实采集中深交所失败日志明确列出 6 只深市标的（159629/159845/159915/159919/159922/159934）。

### 4. 失败原因补充响应消息
- 新增 `etf._response_snippet()`：从异常 response 提取响应体并压平为单行片段。
- `describe_http_failure()` 新增 `extra` 透传参数（向后兼容），统一拼接「响应片段 + 受影响标的」。
- 宏观 `_fetch` 的失败原因也补充了 FRED/akshare 响应片段。

## 三、影响范围
- **行为变化**：采集请求变慢（每请求额外约 1.2~1.7s），全量采集耗时增加；失败信息更丰富；这是为稳定性做的有意取舍。
- **配置变化**：`config.json` 的 `fetch` 段新增 `min_interval_sec` / `jitter_sec`，设为 0 可关闭节流。
- **兼容性**：函数新增参数均有默认值，`describe_http_failure` 新增关键字参数默认空，完全向后兼容。
- **数据**：不改变业务表结构与 upsert 口径；仅监控记录的 `params`/`error` 内容更详细。

## 四、回归清单（修复后必须验证）

| # | 场景 | 验证点 | 状态 |
| --- | --- | --- | --- |
| 1 | 全量单元测试 | `pytest tests/ -q` 全部通过 | ✅ 153 passed |
| 2 | 节流器默认关闭 | 未配置时不 sleep，兼容测试 | ✅ test_rate_limit |
| 3 | 节流器最小间隔 | 相邻请求间隔被强制 ≥ min_interval | ✅ test_rate_limit |
| 4 | 节流器配置更新 | configure 生效、负数归一为 0 | ✅ test_rate_limit |
| 5 | 失败含响应消息 | error 含 `响应片段=...` | ✅ test_failure_detail |
| 6 | 失败指出标的 | error/params 含 `受影响标的=...` | ✅ test_failure_detail |
| 7 | 空数据失败也带标的 | 空表场景 error 含标的 | ✅ test_failure_detail |
| 8 | 沪市标的失败标注 | 上交所失败含标的 + 响应消息 | ✅ test_failure_detail |
| 9 | 真实采集-沪市 | 短区间沪市数据全部成功 | ✅ 已实测 |
| 10 | 真实采集-深市恢复 | 冷却后单次请求恢复 200 + 3088 行 | ✅ 已实测 |
| 11 | 真实采集-带节流全量ETF | 42 次调用、**失败 0**、ETF 66 行（含深市 18 行） | ✅ 已实测 |
| 12 | Web 看板启动 | `/api/health` ok、首页 200 | ✅ 已复验 |
| 13 | 失败重跑 | `--retry-failed` 能定位并补齐失败接口 | ⏳ 逻辑未变，建议下次真实失败时观察 |

## 五、待办 / 风险
- 深交所 IP 若已触发封禁，需等待冷却（分钟级）后恢复；节流只能**预防**，不能立即解除既有封禁。
- 若冷却后仍持续失败，下一步：在监控看板对深市标的常驻提示「深市份额可能滞后」，避免被误判为数据完整。
- 本次未改业务数据，无需清理历史数据。
