# 上交所 / 深交所 ETF 份额接口官方资料

> 核实时间 2026-11-23。重要结论先说：**交易所没有为这两个「网页取数接口」发布正式的公开 API 文档**。它们数据页面前端调用的 JSON / xlsx 取数端点，只能从浏览器 DevTools 的 Network 面板抓到；交易所官方对外发布的是「数据展示页面」与「面向会员的技术接口规范」两套东西。下面把三类资料分清楚。

## 一、官方数据展示页面（人看的，有官网背书）

### 上交所 ETF 规模

- 官方页面：<https://www.sse.com.cn/market/funddata/volumn/etfvolumn/>
- 所在路径：数据 → 基金数据 → 基金规模 → ETF规模（产品 → ETF → ETF规模 同源）
- 表格字段：`日期` / `基金代码` / `基金扩位简称` / `总份额(万份)`
- 官网口径原文：**「注：统计数据为当日清算后数据」**
- 单只 ETF 详情页（含基金规模历史）：`https://www.sse.com.cn/assortment/fund/list/etfinfo/basic/index.shtml?FUNDID=<代码>`，例如 512100 页面展示「基金规模：日期/基金代码/基金扩位简称/总份额(万份)」逐日明细。
- ETF 门户：<http://etf.sse.com.cn/>（基金市场总览，总规模每交易日 23:00 更新）。

### 深交所 ETF 规模

- 官方页面：<https://www.szse.cn/market/fund/volume/etf/index.html>
- 所在路径：市场数据 → 基金数据 → 基金规模 → ETF规模
- 官网口径原文：**「说明：T日晚间更新的T日规模仅供参考，以T+1日早间更新的T日规模为准。」**
- 相关页面：深交所 ETF 列表 <https://www.szse.cn/market/product/list/etfList/index.html>（字段「当前规模(万份)」，并注明「该字段 T日晚间更新的T日规模仅供参考，以 T+1日早间更新的 T日规模为准」）；统计资料入口 <https://www.szse.cn/market/periodical/>。

> 这两处「T 日晚更新仅供参考、T+1 早间为准」的官方说明，正是本项目要在交易日**次日**采集份额、且失败需重跑的原因。

## 二、底层取数接口（网页内部接口，无正式文档；本项目所用）

以下参数来自本项目 `collectors/etf.py`，并已用真实请求于 2026-11-23 实测 HTTP 200 验证。这类端点属于网站内部接口，**接口契约不承诺稳定**，交易所可随时调整 sqlId / 参数 / 反爬策略。

### 上交所：`query.sse.com.cn/commonQuery.do`

| 项 | 值 |
|---|---|
| 方法 / URL | GET `https://query.sse.com.cn/commonQuery.do` |
| 必需请求头 | `Referer: https://www.sse.com.cn/`、`User-Agent: 浏览器 UA` |
| `sqlId` | `COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L`（ETF 规模查询） |
| 关键参数 | `STAT_DATE=YYYY-MM-DD`（交易日）、`isPagination=true`、`pageHelp.pageSize=10000`、`pageHelp.pageNo/beginPage/endPage/cacheSize=1` |
| 返回 | JSON，`result[]` 每行：`STAT_DATE`、`ETF_TYPE`、`SEC_CODE`(基金代码)、`NUM`、`SEC_NAME`、`TOT_VOL`(总份额，万份) |
| 单位换算 | `份额(亿份) = TOT_VOL ÷ 10000`（官网万份 → 亿份） |
| 实测评测 | 2026-11-20 查询 HTTP 200，`result` 5 行；样例 `{'STAT_DATE':'2026-11-20','SEC_CODE':'510010','SEC_NAME':'治理ETF','TOT_VOL':'12752.44'}` |

> 同类接口（LOF 规模）：`sqlId=COMMON_SSE_SJ_JJSJ_JJGM_LOFGMTJ_L` + `PRODUCT_TYPE=11,14,15` + `SEARCH_DATE`（第三方抓包整理，非官方文档）。

### 深交所：`fund.szse.cn/api/report/ShowReport`

| 项 | 值 |
|---|---|
| 方法 / URL | GET `https://fund.szse.cn/api/report/ShowReport` |
| 必需请求头 | `Referer: https://fund.szse.cn/marketdata/fundslist/index.html`、`User-Agent: 浏览器 UA` |
| `CATALOGID` | `scsj_fund_jjgm`（基金规模目录） |
| `SHOWTYPE` | `xlsx`（本项目用，返回 Excel 二进制）或 `JSON`（返回 JSON） |
| `TABKEY` | `tab1` |
| `jjlb` | `ETF`（LOF 则为 `LOF`） |
| 日期参数 | `txtStart=YYYY-MM-DD`、`txtEnd=YYYY-MM-DD`（一次请求可取整个区间） |
| 其它 | `random=0.123456789`（防缓存） |
| 返回 | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;charset=GBK`；表内列：`日期`/`基金代码`/`基金简称`/`基金规模(份)` |
| 单位换算 | `份额(亿份) = 基金规模(份) ÷ 1e8` |
| 实测评测 | 2026-11-20 单日查询 HTTP 200，xlsx 27738 字节 |

## 三、交易所真正对外发布的技术接口文档（面向会员，非上述网页接口）

如果需要**正式的、有文档承诺**的数据接口，交易所提供的是下面这套面向会员/机构的技术规范，与个人网页取数不是一回事：

- 上交所「技术接口」：<https://www.sse.com.cn/services/tradingtech//data/>，例如 `IS124_上海证券交易所市场数据文件交换接口规格说明书`（行情/市场数据文件交换）、`IS120_行情网关 BINARY/STEP 数据接口规范`。这些是协议级规范（STEP/BINARY/文件交换），面向持牌参与者。
- 深交所「数据接口」：<https://www.szse.cn/marketServices/technicalservice/interface>。
- 综合业务平台市场参与者接口规格（含 ETF 现金申赎、实物申赎业务类型）：<https://www.sse.com.cn/services/tradingtech/development/c/10789329/files/d1aea6bb089f46daa9b22a7c24547236.pdf>。

## 四、第三方封装（供对比参考）

- Tushare `etf_share_size` 接口文档：<https://tushare.pro/document/2?doc_id=408>。描述「获取沪深 ETF 每日份额和规模数据，交易所于次日早 8:30 左右更新上一交易日数据」，字段 `total_share`(万份)/`total_size`(万元)/`nav`/`close`/`exchange`。需积分（8000）调用。这与本项目「份额来自交易所、净值来自天天基金、规模自算」的思路一致，可作口径交叉验证。

## 五、对本项目的使用建议

1. 份额数据以**官方页面口径**为准：单位为**万份**（沪深页面均如此），本项目换算到亿份；规模由「份额 × 单位净值」自算，官网 ETF规模页只给份额、不直接给规模金额。
2. 采集时点避开 T 日晚间的「仅供参考」数据，取 **T+1 早间更新后的 T 日份额**；若取到空/异常，用监控的按条重跑补采。
3. 网页内部接口无稳定性承诺：必须带浏览器 UA + 正确 Referer，并保留节流（1.2s+抖动）与失败诊断（标的/参数/响应片段），一旦接口变更可凭监控记录快速定位。
4. 若需要 SLA 级别的数据接口，走第三节的会员技术接口或第四节的商业数据服务，而非网页端点。
