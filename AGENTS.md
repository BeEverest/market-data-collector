# 系统规则（AI / 脚本 / 运维必须遵守）

## 1. 采集原数据不可清理

**采集原数据是权威资产。任何代理、脚本、测试、修复任务、服务启动或批量运维操作，都不得清理、删除、覆盖、重置或重建采集原数据。**

受保护表：

| 类别 | 表 | 说明 |
|---|---|---|
| 宏观采集数据 | `macro_observation` | 采集层原始观测表，不进入 `clear_all()` |
| ETF 采集数据 | `etf_observation` | 采集层原始观测表，不进入 `clear_all()` |
| 指数采集数据 | `index_observation` | 采集层原始观测表，不进入 `clear_all()` |
| 日交易原始数据 | `trade_exchange_raw` | 交易所概况原始表，任何清理范围都不得包含 |
| 日交易原始数据 | `trade_margin_raw` | 两融账户原始表，任何清理范围都不得包含 |
| 日交易原始数据 | `trade_snapshot_raw` | 全A个股快照原始表，任何清理范围都不得包含 |
| 日交易原始数据 | `trade_breadth_raw` | 涨跌家数原始表，任何清理范围都不得包含 |
| 日交易原始数据 | `trade_congestion_raw` | 成交集中度原始表，任何清理范围都不得包含 |

硬约束：

1. `storage.IMMUTABLE_RAW_TABLES` 中的 5 张交易原始表不得进入 `CLEAN_TARGETS` 或 `CLEAR_TABLES`。
2. `storage.COLLECTED_DATA_TABLES` 中的所有采集数据表不得进入 `CLEAR_TABLES`；`clear_all()` 只清理监控、配置、元信息、导出记录和采集统计。
3. 观测表 `macro_observation` / `etf_observation` / `index_observation` 的按口径删除，只允许通过 Web「数据清理」在用户明确选择范围并二次确认后调用 `clean_data()`；AI 代理不得主动调用。
4. 排查预览、看板、图表、指标异常时，优先检查前端列映射、查询条件、计算口径与索引；**不得通过清空原表、删除历史数据或重建采集数据来“修复”展示问题**。
5. 测试必须使用 `tmp_path` 临时 SQLite，不得连接或改写 `output/market_data.db` 及任何生产库。
6. 补数、重跑、重算必须走幂等 upsert / 明确日期范围；不得先删后写。

## 2. 三层架构边界

- 采集层：只负责原始落库，保持原始字段与来源可审计。
- 处理层：从采集层派生计算指标，写入 `derived_indicator` / `trade_observation`。
- 看板层：只读计算宽表与必要的观测表，不修改数据。

## 3. 变更纪律

- 任何涉及清理、删除、覆盖数据的代码变更，必须先确认未触碰上述受保护表。
- 任何新增脚本、测试、定时任务都要遵守本规则；违反本规则的实现视为缺陷。
