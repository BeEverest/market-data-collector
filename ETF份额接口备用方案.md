# ETF 份额接口备用方案

> 背景：项目当前上交所/深交所份额取自交易所官网数据页的**网页取数接口**（无正式 API 文档、无 SLA）。本文给出当主接口失效、被限流或字段变更时的分层备用方案，并附本环境实测结果。
> 实测时间：2026-11-23 02:0x（UTC+8），环境：本项目沙箱（akshare 1.18.22）。

---

## 一、方案总览（按优先级）

| 层级 | 方案 | 与主源关系 | 数据内容 | 本环境实测 | 适用 |
|---|---|---|---|---|---|
| L0 | 自研直连交易所网页接口（现网） | 主源 | 沪深 ETF **历史日频份额** | 可用：SSE 5行/日、SZSE 27738B | 日常采集 |
| L1 | akshare `fund_etf_scale_sse` | **同源不同客户端** | 沪市历史日频份额 | ✅ 20261120 返回 **927 行** | 自研 headers/分页失效时切换 |
| L1 | akshare `fund_etf_scale_szse`（`1000_lf`） | **异构表** | 深市 ETF **当前快照**（份额+净值） | ✅ HTTP200 / 74057B / **1088 行** | 深市最新值兜底、交叉校验（**不能回填历史**） |
| L2 | 东方财富 `push2` `f38 最新份额` | **异构源** | 实时/最新份额 | ❌ 三域名均 RemoteDisconnected（本沙箱网络受限，非接口失效） | 有公网环境做当日兜底 |
| L3 | Tushare `fund_share` | **异构商业源** | 沪深 ETF 历史日频份额（万份） | 需 2000 积分，未实测 | 主源长期不稳定时替代 |
| L3 | Tushare `etf_share_size` | **异构商业源** | 份额+规模+净值+收盘价一站式 | 需 8000 积分，未实测 | 资金面一体化，成本最高 |
| L4 | Choice 文件导入（**已实现**） | 离线兜底 | 用户导出份额/规模 Excel | ✅ 已导入 3438 行 | 接口长时间不可用的最终兜底 |
| L5 | 交易所官网手工下载 → 走 L4 | 人工兜底 | 同上 | — | 极端情况 |

**推荐组合**：`L0 主源 → L1 自动回落 → L4 离线兜底`；有 Tushare 积分再补 L3 做交叉校验。L2 仅作为部署环境放行 push2 域名后的可选增强。

---

## 二、各方案详解

### L1-a 上交所：akshare 封装（同源自愈）

```python
import akshare as ak
df = ak.fund_etf_scale_sse(date="20261120")   # 927 行
```

- 底层仍是 `query.sse.com.cn/commonQuery.do` + `sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L`，与现网**同一接口**。
- 字段：`基金代码 / 基金简称 / ETF类型 / 统计日期 / 基金份额`；akshare 已把 `TOT_VOL(万份) × 10000` 转为**份**。
- 价值：headers、分页、字段映射由 akshare 社区维护。自研请求因 UA/Referer 策略变化失败时，切到这里通常即可恢复。
- 局限：同源，交易所侧限流时一起失败；依赖 akshare 版本。

### L1-b 深交所：akshare 异构表（`1000_lf`，实测可用）

```python
# akshare 封装（首次调用偶发 RemoteDisconnected，建议加 timeout + 重试）
df = ak.fund_etf_scale_szse()

# 等价直连（本次实测成功，推荐写成自研 fallback）
requests.get("https://fund.szse.cn/api/report/ShowReport",
  params={"SHOWTYPE":"xlsx","CATALOGID":"1000_lf","TABKEY":"tab1","random":"0.0761"},
  headers={"Referer":"https://fund.szse.cn/marketdata/fundslist/index.html","User-Agent":UA})
```

- 与现网 `CATALOGID=scsj_fund_jjgm`（按日期查规模日报）**不是同一张表**：这是「基金产品列表」的**当前快照**。
- 字段：`基金代码 / 基金简称 / 基金类别 / 投资类别 / 上市日期 / 当前规模(份) / 净值 / 基金管理人…`，实测 1088 行。
- **关键限制**：无日期参数、无统计日期列 → 只能做**最新值兜底与交叉校验**，不能按 `trade_date` 回填历史。若用于落库，必须标注 `source` 并记录抓取日，避免与日报口径混淆。

### L2 东方财富实时份额（异构，本环境不通）

- 接口：`https://push2.eastmoney.com/api/qt/clist/get`（akshare `fund_etf_spot_em()`），字段 `f38=最新份额`、`f297=数据日期`。
- 本次实测 `push2 / 88.push2 / 82.push2` 三域名均立即 `RemoteDisconnected`，判断为**沙箱网络对该域名放行缺失**，不是接口下线。
- 优点：与交易所完全异构，可验证份额数量级；缺点：只有最新值、无完整历史，且 `f38` 语义（份额口径/更新时点）需核对。
- 建议：仅在部署环境能连通 push2 时启用，且只用于「当日快照交叉校验」，不入库覆盖主源。

### L3 Tushare（异构商业源，需积分）

| 接口 | 积分 | 输出 | 单次上限 | 备注 |
|---|---|---|---|---|
| `fund_share` | ≥2000（5000+ 高频） | `ts_code, trade_date, fd_share(万份)` | 2000 行 | 覆盖沪深 ETF，最贴近现有份额口径，`万份 × 1e4 → 份` |
| `etf_share_size` | ≥8000 | 份额、规模、净值、收盘价、交易所 | 5000 行 | 一站式；交易所次日 08:30 左右更新上一交易日 |

- 官方：`fund_share` <https://tushare.pro/document/2?doc_id=207>；`etf_share_size` <https://tushare.pro/wctapi/documents/408.md>。
- 适合作为**长期稳定替代**；有 SLA 预期，但需 token 与积分成本。
- 接入只需新增一个 collector，映射到现有 `etf_observation(code, trade_date, shares, nav, scale, source)`，注意代码后缀 `.SH/.SZ` 剥离。

### L4 Choice 文件导入（已落地，最终兜底）

- 项目已实现 `--import-files` / Web 多文件上传，文件名 `份额与规模_<ETF代码>.xlsx`，单次≤10 个，按 `(代码,日期)` 幂等 upsert。
- 当交易所接口连续多日失败时，直接走人工导出 → 导入，数据不中断。这是**零开发成本**的兜底。

---

## 三、实测记录

| 目标 | 方式 | 结果 |
|---|---|---|
| 上交所 akshare 封装 | `fund_etf_scale_sse("20261120")` | ✅ 0.4s，927 行，份额单位=份 |
| 深交所 akshare 封装 | `fund_etf_scale_szse()` | ❌ 首次 RemoteDisconnected |
| 深交所 `1000_lf` 直连 | `ShowReport` + `CATALOGID=1000_lf` | ✅ 0.4s，200，74057B，1088 行 |
| 深交所 `scsj_fund_jjgm` 直连（现网主源） | 同上 + 日期参数 | ✅ 200，27738B |
| 东财 push2 三域名 | `clist/get` | ❌ 全部 RemoteDisconnected |
| 天天基金净值 akshare | `fund_etf_fund_info_em` | ❌ 1.18.22 存在 `Length mismatch 14 vs 13` 缺陷，**不要用作净值备用**；继续用自研 `api.fund.eastmoney.com/f10/lsjz` |

---

## 四、推荐切换策略（可直接落地）

```text
采集某个交易日的份额
   │
   ├─ L0 自研直连交易所接口 ────────── 成功 → 落库(source=原值)
   │        └─ 失败（限流/空/异常）
   │
   ├─ L1 沪: ak.fund_etf_scale_sse(date) ─ 成功 → 落库(source=akshare_sse)
   │  深: 1000_lf 当前快照 ─────────── 仅取代码交集做「当前值兜底」，
   │                                  标注 snapshot，禁止写历史日
   │        └─ 失败
   │
   ├─ L2 东财 push2 f38（环境允许时）→ 仅当日快照校验，不覆盖
   │        └─ 失败
   │
   └─ 标记本日采集失败 → 监控失败明细（已含标的/原因/参数/响应）
                        → 人工按条重跑 或 走 L4 Choice 导入
```

工程要点：

1. **配置化顺序**：`config.json` 增加 `etf.share_fallback: ["sse_akshare", "szse_1000lf_snapshot", "em_spot"]`，便于按环境开关。
2. **口径统一**：所有源最终统一为「份」（SSE `TOT_VOL` 万份×1e4；Tushare `fd_share` 万份×1e4；SZSE 原始即份），再由 `storage.upsert_etf` 转亿份/亿元，规模仍按 `份额 × 净值` 反推。
3. **来源可追溯**：`etf_observation.source` 已存在，回落取数必须写入具体来源，避免把快照当日报。
4. **重试与节流不取消**：L1/L2 仍走现有 `rate_limit`（1.2s + 0.5s 抖动）与指数退避，避免把备用源也打限流。
5. **净值备用需另选**：当前 akshare `fund_etf_fund_info_em` 有缺陷；可考虑天天基金 `f10/lsjz`（现网主源）多域名或 Tushare `fund_nav`。

---

## 五、结论与建议

- **立刻可用、零新增依赖**：L1-a（沪市 akshare 封装）与 L1-b（深市 `1000_lf` 直连）都已实测通过，建议在 `collectors/etf.py` 的 `fetch_sse_shares` / `fetch_szse_shares` 各加一层 fallback。
- **深市注意**：`1000_lf` 是快照无日期，只能兜当天、不能补历史；历史日频仍依赖 `scsj_fund_jjgm` 或 Tushare。
- **长期稳定性**：若要摆脱网页接口不确定性，Tushare `fund_share`（2000 积分）是最经济的异构替代；`etf_share_size` 则一步到位但需 8000 积分。
- **保底能力已具备**：Choice 导入通道 + 监控四要素失败明细 + 按条重跑，已覆盖「接口坏了也能补数、且知道为什么坏」的闭环。

（本文仅做方案调研与实测，未改动项目代码。如需，我可以把 L1 回落直接实现到采集器并补测试。）
