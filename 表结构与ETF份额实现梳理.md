# 表结构与 ETF 份额实现逻辑梳理

> 基于代码与当前真实库 `output/market_data.db` 梳理，采集时点 2026-11-23。
> SQLite 为唯一实时主存储；CSV / Excel 仅按需导出快照。

## 一、所有表结构

共 **10 张表**（不含 SQLite 内部的 `sqlite_sequence`）。统一约定：业务表以 `biz_key TEXT PRIMARY KEY` 做幂等主键，所有写入用 `ON CONFLICT(biz_key) DO UPDATE`，因此全量采集 / 精确采集 / 失败重跑 / 文件导入都不会产生重复行。各表**实时行数以 `/api/status` 为准**（下表"当前行数"为 2026-11-23 梳理时点快照，随采集变化）。

> 派生指标表 `derived_indicator` 由定时调度增量重算写入，不在 `CLEAR_TABLES` 清理范围内（数据清理模块仅清三类观测表）。其重算口径见下文「三、派生指标增量重算」。

### 1. 业务观测表（3 张）

| 表 | 业务主键 `biz_key` 规则 | 字段（SQLite 列 ← 业务列名） | 索引 | 当前行数 |
|---|---|---|---|---|
| `macro_observation` 宏观月度 | `{指标代码}_{YYYYMM}`，如 `M2_202610` | `biz_key` / `code`←指标代码 / `name`←指标名称 / `date`←日期 / `value`←数值 / `unit`←单位 / `yoy_pct`←同比(%) / `mom_pct`←环比(%) / `source`←数据源 / `collected_at`←采集时间 / `run_id` | `(code, date DESC)` | 3596 |
| `etf_observation` ETF 日频 | `{6位基金代码}_{YYYYMMDD}`，如 `510050_20261020` | `biz_key` / `code`←基金代码 / `name`←基金简称 / `market`←交易市场 / `trade_date`←交易日期 / `shares`←份额(亿份) / `nav`←单位净值(元) / `market_cap`←规模(亿元) / `source`←数据源 / `collected_at`←采集时间 / `run_id` | `(code, trade_date DESC)` | 51123 |
| `index_observation` 指数日频 | `{6位指数代码}_{YYYYMMDD}` | `biz_key` / `code`←指数代码 / `name`←指数名称 / `trade_date`←交易日期 / `open` / `high` / `low` / `close` / `volume` / `pct_change`←涨跌幅(%) / `source` / `collected_at` / `run_id` | `(code, trade_date DESC)` | 6672 |

业务主键生成函数位于 `storage.py`：

- `macro_biz_key(code, date)`：日期截取前 7 位（`YYYY-MM`）去横杠；
- `etf_biz_key(code, trade_date)` / `index_biz_key(...)`：代码补零到 6 位 + 日期去横杠；
- `derived_biz_key(code, date)`：`derived_` + 派生代码 + `YYYYMM`（如 `derived_M2_YOY_202610`）；
- `_clean()` 把 `None` / `NaN` 统一转为 SQL `NULL`，数值列存 `REAL`。

### 1b. 派生指标表（1 张）

| 表 | 业务主键 `biz_key` 规则 | 字段 | 索引 | 当前行数 |
|---|---|---|---|---|
| `derived_indicator` 派生指标 | `derived_{派生代码}_{YYYYMM}`，如 `derived_M2_YOY_202610` | `biz_key` / `code` / `name` / `date`←`YYYY-MM` / `value` / `unit` / `formula` / `grp` / `source`（恒为 `macro_observation`）/ `computed_at` | `biz_key` 主键 | 2314（14 项 × 320 月） |

写入函数：`storage.upsert_derived()`（按 biz_key 幂等覆盖）、`storage.replace_derived()`（手动全量重建时整表替换、清理陈旧口径行）、`storage.read_derived()`（可按月读取）。计算规则集中在 `processors/derived.py`，只读 `macro_observation`，产出 14 项指标。

### 2. 配置表（2 张）

| 表 | 键 | 内容 | 当前行数 |
|---|---|---|---|
| `etf_config` | `key='main'` 单行 | ETF 代码清单 + `listing_dates`（上市/成立日缓存）等，值为 JSON；采集后回写新解析的上市日 | 1 |
| `index_config` | `key='main'` 单行 | 指数代码清单等（本库当前为空，尚为 0 行） | 0 |

> `config.json` 是配置镜像 / 回滚基线。启动 `load_config()` 时若 SQLite 已有配置，则以库内配置覆盖 `config.json` 的 `etf` / `index` 段（`main.py:55`）。

### 3. 监控与统计表（3 张）

| 表 | 业务主键 | 关键字段 | 索引 | 当前行数 |
|---|---|---|---|---|
| `interface_run` 接口调用明细 | `{run_id}_{interface_key}_{run_time}_{序号}`（`run_biz_key`，运行时间去符号，同键用 3 位序号区分） | `biz_key` / `run_id` / `run_type`（full/retry/interface/import/scheduled）/ `run_date` / `run_time`（精确到秒）/ `category` / `interface_key` / `interface_name` / `params`(JSON) / `status` / `attempts` / `rows` / `error` / `duration_sec` | `(run_date DESC)`、`(interface_key, run_time)`、`(status)` | 11 |
| `collection_stat` 采集量统计 | `{run_id}\|{category}\|{code}`（ETF/指数代码补零） | `biz_key` / `run_id` / `run_type` / `run_date` / `category` / `code` / `name` / `rows` / `collected_at` | `(run_id, category)`、`(run_date DESC)` | 936 |
| `export_log` 导出记录 | 自增 `id`（本表是唯一非 biz_key 主键） | `export_ts` / `scope` / `file_path` / `row_counts`(JSON) | — | 1 |

### 4. 元数据表（1 张）

| 表 | 键 | 内容 | 当前行数 |
|---|---|---|---|
| `meta` | `key='schema_version'` | schema 版本（当前 `"1"`），`init_schema()` 用 `INSERT OR IGNORE` 写入 | 1 |

### 5. 连接与清理

- `connect()`：`timeout=30`、`WAL` 日志模式、`synchronous=NORMAL`、`busy_timeout=30000`，`check_same_thread=False`（Web 多线程共享）。
- `init_schema()`：幂等建表 + 建索引，每个读写函数入口都会调用。
- `clear_all()`：按 `CLEAR_TABLES` 清空 9 张表（保留结构），并归零 `export_log` 自增计数；`config.json` 不在清除范围。注意 `derived_indicator` **不在** `CLEAR_TABLES` 内，清空观测数据不会连带删除派生指标（重新采集后由派生重算刷新）。

---

## 二、ETF 份额详细实现逻辑

ETF 日频数据口径：**每个交易日一行，`规模(亿元) = 份额(亿份) × 单位净值(元)`**。数据有两条入口：**在线采集**（`collectors/etf.py`）与 **Choice 文件导入**（`importer.py`），最终都经 `storage.upsert_etf()` 写入 `etf_observation`。

### 1. 在线采集的数据源与底层接口

| 环节 | 数据源 / 接口 | 函数 | 频率与产出 |
|---|---|---|---|
| 交易日历 | 新浪 `ak.tool_trade_date_hist_sina()` | `get_trade_dates()` | 一次取区间交易日列表，`etf.trade_calendar` 记录 |
| 上市/成立日 | 天天基金档案页 `fundf10.eastmoney.com/jbgk_<code>.html`，正则抓「成立日期」 | `get_listing_dates()` | 每基金一次，带模块级缓存；成立前记录被过滤 |
| 上交所份额 | `query.sse.com.cn/commonQuery.do`（sqlId=ETFGM_SEARCH） | `fetch_sse_shares()` | **逐交易日**一次请求，`TOT_VOL` 万份→亿份（÷1e4） |
| 深交所份额 | `fund.szse.cn/api/report/ShowReport`（xlsx） | `fetch_szse_shares()` | **一次请求取整个日期区间**，`基金规模(份)`÷1e8→亿份 |
| 单位净值 | 天天基金 `api.fund.eastmoney.com/f10/lsjz`（分页） | `fetch_nav()` | **每基金一次**，翻页至 `TotalCount` 满 |
| 基金简称 | akshare `fund_etf_fund_daily_em()` | `get_fund_name_map()` | 进程内缓存，补全交易所简称 |

所有请求前调用 `throttle_wait()`（`collectors/rate_limit.py`）：进程内单例节流器，相邻请求间隔 `min_interval_sec=1.2s` + `0~jitter_sec=0.5s` 随机抖动，规避深交所对密集请求的断连。重试采用指数退避 `retry_interval × 2^(n-1)`。

### 2. `collect_etf()` 主流程（online 核心）

```
入参 codes / start_date / end_date / default_trade_days / sse_dates / szse_range / nav_codes / listing_dates
  │
  ├─ end = end_date 或 今天；start = start_date 或「最近 N 个交易日」
  ├─ target_codes = codes 补零去重
  ├─ get_listing_dates() 解析并原地补全 listing_dates
  ├─ effective_start = max(start, 最早上市日)；若 > end 直接返回空
  ├─ trade_dates = get_trade_dates(effective_start, end)
  │
  ├─ 沪市：for 每个交易日 → fetch_sse_shares(date, target_codes=sh_targets)
  │          sse_dates 非空时只取指定日（部分重跑）
  ├─ 深市：fetch_szse_shares(start, end, target_codes=sz_targets) 一次区间
  ├─ shares = 拼接 → 仅保留 target_codes → 按(代码,日期)去重 keep=last
  ├─ 简称补全：交易所简称优先，缺失用 akshare 映射
  ├─ 净值：for 每只基金 → fetch_nav(code, max(start,上市日), end)
  │          nav_codes 非空时只取指定基金（部分重跑）
  ├─ merged = shares LEFT JOIN nav on (基金代码,交易日期)
  ├─ 过滤早于上市日的行；规模 = 份额 × 净值
  └─ 按(代码,日期)降序输出 OUTPUT_COLUMNS
```

关键点：

- **市场按代码前缀拆分**：`5/6` 开头走沪市，`1/8` 开头走深市；`target_codes` 只用于失败时标注「受影响标的」，不过滤接口返回（顶层再按目标代码过滤）。
- **上市日裁剪两次**：抓取下界抬到最早上市日减少无效请求；合并后再逐行过滤成立前记录。
- **空数据即失败**：份额接口返回空表会记 `status=failed`（含响应片段与受影响标的）；净值空表同样记失败。
- **部分重跑**：`main._retry_failed()` 从失败任务里解析 `sse_dates` / `szse_range` / `nav_codes`，只重采失败部分；份额失败时全量补净值以保证规模可重算，仅净值失败时只补净值、份额沿用主数据。

### 3. Choice 文件导入（另一条份额入口）

`importer.py` 解析 `份额与规模_<6位代码>.xlsx`（妙想 Choice 导出）：

- 文件名正则 `份额与规模[_\-\s]?(\d{6})` 提取基金代码；
- 列按包含匹配：`截止日期` / `流通份额` / `流通规模`，跳过含「数据来源/备注」的脚注行；
- 单位换算：`份额(亿份)=流通份额÷1e8`、`规模(亿元)=流通规模÷1e8`、`单位净值=规模÷份额`（文件无净值列，反推，保留 4 位小数）；
- `交易市场` 按代码前缀推断（15/16/18→SZ，50/51/52/56/58→SH）；
- 基金简称补全顺序：显式 name > SQLite 已有名称 > akshare 映射；
- 单次最多 10 个文件；每个文件一条 `interface_run`（`etf.import`，run_type=`import`）；
- 最终同样 `storage.upsert_etf()`，按 `(代码,日期)` 幂等覆盖。

### 4. 写入与合并

- `main._collect()` 编排三类采集；`_finish()` 统一落库：`monitor.save()` → `upsert_macro/etf/index` → `append_collection_stats()`；按配置决定是否自动导出快照。
- `main._merge_patch()`：补丁非空字段覆盖主数据同键记录，主键缺失则追加；ETF 合并后统一重算 `规模 = 份额 × 净值`。
- 精确采集 `collect_interface(category='etf')` 与重跑都基于「读最新主数据为基底 + 采集补丁 + 合并 + 重算规模 + `_finish`」，因此不会覆盖其它标的/日期。

### 5. 当前库内 ETF 数据画像

- 共 **51123 行 / 26 只基金**，日期跨度 `2016-11-23 ~ 2026-11-20`（实时以 `/api/status` 为准）。
- 来源分布：`妙想Choice` 导入 51105 行；`交易所份额+天天基金净值` 在线采集 18 行。
- 即当前主体数据来自文件导入，在线采集为补充；两者共用同一张 `etf_observation` 表与同一业务主键。

### 6. 失败诊断（近期增强）

每条失败记录含四要素：`标的 / 失败原因 / 参数 / 响应`，由 `monitor.format_failure_detail()` 生成，出现在重跑日志、监控「失败明细」的**失败详情**列与单条重跑日志。空数据场景也记录响应片段；旧记录会从接口键/参数推断标的（如「上交所全部ETF(日期)」）。

---

## 三、派生指标增量重算

派生指标由**定时调度**在每日采集完成后触发（步骤 6/6），写入 `derived_indicator`。计算规则集中在 `processors/derived.py`，只读 `macro_observation`。

### 1. 14 项指标与依赖

| 组 | 指标（`code`） | 口径 | 依赖原始代码 |
|---|---|---|---|
| A 价格与通胀 | `TRADE_YOY` | 官方累计进出口同比 | `TRADE_CUM_TOTAL` |
| | `RETAIL_SALES_YOY` / `CPI_YOY` / `PPI_YOY` | 官方同比直接取数 | `RETAIL_SALES` / `CPI` / `PPI` |
| | `CPI_PPI_GAP` | CPI同比 − PPI同比 | `CPI` / `PPI` |
| B 货币与信用 | `SFR_STOCK_YOY` | 央行官方增速优先，缺失按存量派生 | `SFR_STOCK` |
| | `M1_YOY` / `M2_YOY` | 官方同比直接取数 | `M1` / `M2` |
| | `M1_M2_GAP` | M1同比 − M2同比 | `M1` / `M2` |
| | `A_SHARE_MC_M2` | A股总市值 ÷ M2 ×100% | `A_SHARE_MC` / `M2` |
| C 存贷款与储备 | `HH_DEPOSIT_NEW` | 居民存款余额一阶差分 | `HH_DEPOSIT` |
| | `HH_DEPOSIT_NEW_YOY` | 居民存款新增二阶差分 | `HH_DEPOSIT` |
| | `RMB_LOAN_NEW` | 贷款余额一阶差分 | `RMB_LOAN` |
| | `GOLD_NEW_TON` | 黄金储备差分 × 31.1034768÷100（万盎司→吨，≥100吨跳变剔除） | `GOLD_RESERVE` |

依赖关系登记在 `DERIVED_SOURCE_CODES`，是增量重算判断"受哪些原始观测变化影响"的依据。

### 2. 增量判定链路（`web_app._recompute_derived(run_id=...)`）

```
定时采集完成（_do_collect，trigger=定时调度）
  └─ _recompute_derived(run_id=monitor.run_id)
       ├─ storage.read_macro_changes_by_run(db, run_id)
       │     → 本次写入的原始 (代码集合, 月份集合)
       ├─ incremental_targets(changed_codes, changed_months)
       │     → 目标派生指标 + 目标月份
       ├─ 派生表为空？→ 是：回退全量 compute_derived + replace_derived（建基线）
       │              → 否：只算目标 compute_derived(target_codes,target_months) + upsert_derived
       └─ 按 biz_key 幂等覆盖，不删全表
```

- **变更识别**：宏观表每行带 `run_id`，`read_macro_changes_by_run()` 按本次 run 取 `(code, YYYY-MM)` 去重集合。
- **依赖传播**：原始观测在月份 M 变更，目标派生月份按偏移 `{0, +1, +12, +13}` 扩展并集——`+1` 覆盖作为下月差分分母，`+12` 覆盖作为明年同月同比分母，`+13` 覆盖二阶差分的去年同月新增分母；目标指标取依赖了变更代码的派生项（见 `DERIVED_SOURCE_CODES`）。保守覆盖，确保任何依赖方向都不漏。
- **分母完整**：`compute_derived()` 的新 `target_codes`/`target_months` 只过滤产出（并跳过无关月份的 emit），但差分/同比分母始终基于**全量** `macro_df` 构建（`new_hh` / `new_loan` 走 `all_dates`），不会因只算目标月而算错。
- **幂等写库**：`upsert_derived()` 按 `biz_key=派生代码+YYYYMM` 覆盖；不做 `replace_derived()` 全表替换，避免对全量历史重复计算。
- **空表回退 / 跳过**：派生表为空（首次部署）自动全量建基线；本次 run 无宏观写入（如仅采 ETF/指数）则跳过，返回 0。

### 3. 手动重算（兜底，口径不变）

`POST /api/derived/recompute`（看板「重新计算」按钮）：

| 请求体 | 行为 | 写库 |
|---|---|---|
| `{}`（默认） | 全量重算 14 项 | `replace_derived()` 整表替换，清理陈旧口径行 |
| `{"month":"YYYY-MM"}` | 按月局部 | `upsert_derived()` 幂等覆盖 |
| `{"code":"XXX"}` | 按指标局部 | 同上 |
| `{"month":...,"code":...}` | 月份+指标 | 同上 |

三种模式（定时增量 / 手动全量 / 手动局部）下差分/同比分母均基于全量原始值计算，不会因单月缺分母算错。
