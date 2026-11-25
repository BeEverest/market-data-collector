/* ==========================================================================
 * 投研大盘数据平台 · 交互原型 v5.3
 * 所有交互均为本地模拟，不调用后端；数据来自 assets/data.js 少量样例。
 * ========================================================================== */
const D = window.PROTO_DATA, S = D.status, DB = D.dashboard, LAST = S.last_trade_date;
const CLR = ['#2563eb','#16a36a','#ea8c28','#8056d8','#e0484b','#0d9a9a','#db2777','#64748b'];
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
const byId = id => document.getElementById(id);
const fmt = n => (n == null ? 0 : n).toLocaleString('zh-CN');
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const now = () => new Date().toLocaleTimeString('zh-CN', {hour12:false});
const pad = n => String(n).padStart(2, '0');
const runId = () => `20261126_${now().replace(/:/g,'')}_manual`;

/* ---------------- 反馈组件 ---------------- */
function toast(msg, type='info'){
  const box = byId('toastBox');
  const ic = {info:'info-circle', ok:'circle-check', warn:'alert-triangle', err:'circle-x'}[type] || 'info-circle';
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<i class="ti ti-${ic}"></i><div>${esc(msg)}</div>`;
  box.appendChild(el);
  setTimeout(()=>{ el.style.opacity='0'; el.style.transform='translateX(16px)'; setTimeout(()=>el.remove(), 260); }, 3400);
}
function modal(title, body, onOk, okText='确认'){
  const root = byId('modalRoot');
  root.innerHTML = `<div class="modal-mask"><div class="modal-box">
    <div class="modal-head"><i class="ti ti-question-circle" style="color:var(--blue)"></i>${esc(title)}</div>
    <div class="modal-body">${body}</div>
    <div class="modal-foot"><button class="btn btn-sm" data-act="modal-cancel">取消</button><button class="btn btn-primary btn-sm" data-act="modal-ok">${esc(okText)}</button></div>
  </div></div>`;
  root._onOk = onOk;
}
function closeModal(){ byId('modalRoot').innerHTML = ''; }

/* ---------------- 实时监控日志 ---------------- */
const LOG_STEPS = [
  '[采集] 提交任务，生成 run_id',
  '[宏观] 采集当月 25 项指标（定时调度·仅当月）',
  '[ETF] 采集 26 只标的日频数据',
  '[指数] 采集 4 个指数日线',
  '[交易] 写入 5 张原始表（exchange / margin / snapshot / breadth / congestion）',
  '[派生] 按 run_id 增量重算受影响指标/月份',
  '[完成] 全部接口成功，统计已写入监控',
];
function appendLog(text, level='INFO'){
  const body = byId('logBody'); if(!body) return;
  body.insertAdjacentHTML('beforeend', `<div class="log-line"><span class="t">${now()}</span> <span class="${level}">${esc(text)}</span></div>`);
  const strip = byId('logMarquee'); if(strip){ strip.className = level; strip.textContent = text; }
  const wrap = byId('logBodyWrap'); if(wrap && byId('logAutoScroll')?.checked) wrap.scrollTop = wrap.scrollHeight;
}
function simulateCollect(label, steps){
  const badge = $('#hdStatus');
  badge.textContent = '采集中…'; badge.parentElement.classList.add('busy');
  appendLog(`[${label}] 任务已提交 run_id=${runId()}`, 'INFO');
  const lines = steps || LOG_STEPS;
  let i = 0;
  const timer = setInterval(()=>{
    appendLog(lines[i], i === lines.length-1 ? 'OK' : 'INFO');
    if(++i >= lines.length){
      clearInterval(timer);
      badge.textContent = '系统正常'; badge.parentElement.classList.remove('busy');
      toast(`${label}完成 · ${lines.length} 个步骤`, 'ok');
    }
  }, 380);
}

/* ---------------- 样例数据 ---------------- */
const MACRO_INDS = [
  ['M1_YOY','M1 同比','货币','央行','月'],['M2_YOY','M2 同比','货币','央行','月'],
  ['M1_M2_SCISSOR','M1-M2 剪刀差','货币','计算','月'],['SOCIAL_FINANCING','社融增量','信用','央行','月'],
  ['SOCIAL_FINANCING_STOCK_YOY','社融存量同比','信用','央行','月'],['RMB_LOAN_NEW','人民币贷款当月新增','信用','央行','月'],
  ['RMB_DEPOSIT_NEW','居民存款新增','信用','央行','月'],['RMB_DEPOSIT_YOY_MORE','居民存款同比多增','信用','计算','月'],
  ['CPI_YOY','CPI 同比','物价','统计局','月'],['PPI_YOY','PPI 同比','物价','统计局','月'],
  ['CPI_PPI','CPI-PPI 剪刀差','物价','计算','月'],['RETAIL_SALES_YOY','社零同比','消费','统计局','月'],
  ['RETAIL_SALES_MOM','社零环比','消费','统计局','月'],['EXPORT_YOY','出口同比','外贸','海关','月'],
  ['IMPORT_YOY','进口同比','外贸','海关','月'],['TRADE_BALANCE','进出口差额','外贸','海关','月'],
  ['GDP','中国 GDP','增长','统计局','季'],['GDP_YOY','GDP 同比','增长','统计局','季'],
  ['GOLD_RESERVE','黄金储备','储备','央行','月'],['GOLD_NEW_TON','黄金当月新增/吨','储备','计算','月'],
  ['A_SHARE_MV','A股总市值','市场','交易所','日'],['A_SHARE_MV_M2','A股总市值/M2','市场','计算','月'],
  ['US_CPI','美国 CPI','海外','BLS','月'],['US_CORE_CPI','美国核心 CPI','海外','BLS','月'],
  ['US_NFP','美国非农就业','海外','BLS','月'],['US_UNEMPLOYMENT','美国失业率','海外','BLS','月'],
];
const ETF_ITEMS = [
  ['510050','上证50ETF','宽基','2004-12-30','586.2'],['510300','沪深300ETF','宽基/汇金','2012-05-28','1024.8'],
  ['510500','中证500ETF','宽基','2013-02-06','182.4'],['518880','黄金ETF','黄金','2013-07-29','78.6'],
  ['159915','创业板ETF','宽基','2011-09-20','312.5'],['588000','科创50ETF','宽基','2020-11-16','421.7'],
  ['512100','中证1000ETF','宽基/汇金','2016-10-24','268.3'],['159919','沪深300ETF(深)','汇金','2012-05-28','356.9'],
];
const INDEX_ITEMS = [
  ['000001','上证指数','3,892.14','+0.42','18,225'],['000300','沪深300','4,102.66','+0.18','18,225'],
  ['000688','科创50','1,024.38','-0.35','18,225'],['399006','创业板指','2,318.52','+0.61','18,225'],
];
const TRADE_LATEST = [
  ['TOTAL_AMOUNT','全A成交额','16,760.11','亿元','交易所'],
  ['CONCENTRATION5','成交集中度(前5%)','47.57','%','乐咕/插值'],
  ['MARGIN_BALANCE','两融余额','21,348.6','亿元','交易所'],
  ['MARGIN_FINANCING_RATIO','融资保证金比例','100','%','政策阶梯'],
  ['FINANCING_TO_FLOAT_MV','融资余额占流通市值比','2.18','%','计算'],
  ['FINANCING_BUY_PCT','融资买入额占比','8.42','%','计算'],
  ['AVG_MAINTENANCE_RATIO','平均维持担保比例','278.6','%','交易所'],
  ['DEBT_INVESTORS','有负债投资者数','1,382.5','万人','交易所'],
  ['UP_COUNT','上涨家数','3,128','家','快照'],
  ['DOWN_COUNT','下跌家数','1,842','家','快照'],
];
const MONITOR_DATA = {
  overview: [
    ['20261126_113540_scheduled',`${LAST} 11:35:40`,'定时调度','284','284','0','100%','186s'],
    ['20261125_001004_interface',`${LAST} 00:10:04`,'精确采集','6','6','0','100%','12.1s'],
    ['20261124_000929_interface','2026-11-23 00:09:29','精确采集','4','4','0','100%','12.2s'],
    ['20261123_000849_interface','2026-11-22 00:08:49','精确采集','4','3','1','75.0%','11.2s'],
    ['20261122_030001_scheduled','2026-11-21 03:00:01','定时调度','284','283','1','99.6%','191s'],
  ],
  failures: [
    ['T-10241','天天基金','份额与规模','2026-11-22','HTTP 429 限流','','待重跑'],
    ['T-10240','乐咕乐股','成交集中度','2026-11-22','页面 token 失效','已重跑成功',''],
    ['T-10188','东方财富','指数日线','2026-11-20','连接超时','','待重跑'],
    ['T-10102','央行','社融增量','2026-11-15','接口返回空','已重跑成功',''],
  ],
  health: [
    ['央行','macro_cpb','宏观','128','126','98.4%','2','正常'],
    ['统计局','macro_stats','宏观','96','96','100%','0','正常'],
    ['天天基金','etf_fund','ETF','2,840','2,758','97.1%','82','限流'],
    ['东方财富','index_em','指数','260','258','99.2%','2','正常'],
    ['乐咕乐股','congestion','交易','88','87','98.9%','1','正常'],
  ],
  detail: [
    ['20261126_113540_scheduled','11:35:40','M1 同比','宏观','成功','1','0.8s',''],
    ['20261126_113540_scheduled','11:35:41','沪深300ETF','ETF','成功','1','1.2s',''],
    ['20261126_113540_scheduled','11:35:43','上证指数','指数','成功','1','0.6s',''],
    ['20261123_000849_interface','00:08:49','黄金ETF','ETF','失败','0','11.2s','HTTP 429'],
  ],
  stats: [
    ['宏观指标','26','578','3,596','月','2026-11'],
    ['ETF 基金','26','26','51,175','日','2026-11-24'],
    ['指数日线','4','4','18,225','日','2026-11-24'],
    ['日交易','3','24,056','5,582','日','2026-11-24'],
  ],
};
const DATA_TABS = {
  macro: [['月份','指标代码','指标名称','数值','单位','数据源'],[
    ['2026-10','M1_YOY','M1 同比','6.2','%','央行'],['2026-10','M2_YOY','M2 同比','8.5','%','央行'],
    ['2026-10','CPI_YOY','CPI 同比','0.2','%','统计局'],['2026-09','GDP_YOY','GDP 同比','4.6','%','统计局'],
    ['2026-09','SOCIAL_FINANCING','社融增量','3.42','万亿','央行'],['2026-08','M1_YOY','M1 同比','5.8','%','央行'],
  ]],
  etf: [['交易日期','基金代码','基金名称','份额(亿份)','单位净值','净流入(亿元)'],[
    [LAST,'510300','沪深300ETF','1,024.8','4.02','+12.4'],[LAST,'510050','上证50ETF','586.2','3.28','-3.1'],
    [LAST,'518880','黄金ETF','78.6','9.16','+2.7'],['2026-11-21','510300','沪深300ETF','1,021.6','3.98','+8.2'],
  ]],
  index: [['交易日期','指数代码','指数名称','开盘','最高','最低','收盘','涨跌幅(%)'],[
    [LAST,'000001','上证指数','3,875.20','3,898.60','3,870.11','3,892.14','+0.42'],
    [LAST,'000300','沪深300','4,090.10','4,108.20','4,086.30','4,102.66','+0.18'],
    [LAST,'000688','科创50','1,030.20','1,032.80','1,020.40','1,024.38','-0.35'],
  ]],
  trade: [['交易日期','全A成交额(亿元)','成交集中度(%)','两融余额(亿元)','上涨家数','下跌家数','融资保证金比例(%)'],[
    [LAST,'16,760.11','47.57','21,348.6','3,128','1,842','100'],
    ['2026-11-21','18,204.56','46.82','21,266.4','2,064','2,986','100'],
    ['2026-11-20','17,842.30','48.13','21,190.8','3,486','1,524','100'],
  ]],
  files: [['文件名','类别','行数','生成时间','大小','操作'],[
    ['macro_20261126_113540.csv','宏观','3,596','2026-11-26 11:38','286 KB','下载'],
    ['etf_20261126_113540.xlsx','ETF','51,175','2026-11-26 11:38','1.8 MB','下载'],
    ['index_20261126_113540.csv','指数','18,225','2026-11-26 11:38','642 KB','下载'],
    ['export_all_20261126.zip','全量','—','2026-11-26 11:38','4.2 MB','下载'],
  ]],
};

/* ---------------- 导航结构（覆盖原系统全部功能） ---------------- */
const NAV = {
  overview:{name:'总览',groups:[
    {cap:'概览',items:[{id:'ovCollect',name:'采集数据概览',icon:'layout-dashboard'}]},
  ]},
  collect:{name:'数据采集',groups:[
    {cap:'采集作业',items:[{id:'console',name:'采集控制台',icon:'player-play'}]},
    {cap:'采集配置',items:[
      {id:'maccfg',name:'宏观指标配置',icon:'adjustments'},
      {id:'idxcfg',name:'指数标的配置',icon:'chart-dots'},
      {id:'etfcfg',name:'ETF 标的配置',icon:'coins'},
    ]},
    {cap:'数据预览',items:[
      {id:'prev-macro',name:'宏观数据',icon:'table'},
      {id:'prev-etf',name:'ETF 日频',icon:'table'},
      {id:'prev-index',name:'指数日线',icon:'table'},
      {id:'prev-trade',name:'日交易',icon:'table'},
      {id:'prev-files',name:'输出文件',icon:'file-export'},
    ]},
    {cap:'数据维护',items:[{id:'clean',name:'数据清理',icon:'eraser'}]},
  ]},
  derived:{name:'数据处理',groups:[
    {cap:'指标计算',items:[
      {id:'macrocalc',name:'派生指标',icon:'chart-area'},
      {id:'etfcalc',name:'ETF 计算',icon:'coins'},
      {id:'tradecalc',name:'交易计算',icon:'scale'},
    ]},
  ]},
  dashboard:{name:'数据看板',groups:[
    {cap:'可视化',items:[
      {id:'dashmacro',name:'宏观指标',icon:'chart-area'},
      {id:'dashetf',name:'ETF 维度',icon:'chart-bar'},
      {id:'dashtrade',name:'日交易',icon:'chart-line'},
    ]},
  ]},
  ops:{name:'运维监控',groups:[
    {cap:'接口监控',items:[
      {id:'mon-overview',name:'运行总览',icon:'activity'},
      {id:'mon-failures',name:'失败明细',icon:'alert-triangle',badge:String(S.failed_total)},
      {id:'mon-health',name:'接口健康度',icon:'heartbeat'},
      {id:'mon-detail',name:'调用明细',icon:'list'},
      {id:'mon-stats',name:'采集量统计',icon:'chart-bar'},
    ]},
    {cap:'调度管理',items:[{id:'tasksched',name:'任务调度',icon:'calendar-clock'}]},
  ]},
};
let curMod = 'overview', curView = 'ovCollect';

/* ---------------- 通用组件 ---------------- */
const statCard = (ic,c,val,lbl,trend,tc) => `<div class="col-sm-6 col-lg-3"><div class="stat-card"><div class="ic ${c}"><i class="ti ti-${ic}"></i></div><div class="val">${val}</div><div class="lbl">${lbl}</div>${trend?`<span class="trend ${tc}">${trend}</span>`:''}</div></div>`;
const statRow = (...c) => `<div class="row row-cards">${c.join('')}</div>`;
const head = (t,d) => `<div class="pg-head"><div><h1>${t}</h1><p>${d}</p></div></div>`;
const tbl = (cols, rows, opts={}) => {
  const headCells = [...(opts.expand?['<th></th>']:[]), ...cols.map(c=>`<th>${esc(c)}</th>`), ...(opts.action?['<th>操作</th>']:[])];
  const h = `<thead><tr>${headCells.join('')}</tr></thead>`;
  const b = rows.map((r,i)=>{
    const lead = opts.expand ? `<td><button class="btn btn-sm" data-act="row-expand" data-i="${i}">▸</button></td>` : '';
    const cells = r.map(v=>`<td><div class="cell-clip">${v}</div></td>`).join('');
    const tail = opts.action ? `<td><button class="btn btn-sm" data-act="${esc(opts.action)}" data-i="${i}">${esc(opts.actionLabel||'操作')}</button></td>` : '';
    return `<tr${opts.fail&&String(r[opts.fail]).includes('失败')?' class="row-fail"':''}>${lead}${cells}${tail}</tr>`;
  }).join('');
  return `<div class="table-responsive"><table class="table table-hover">${h}<tbody>${b}</tbody></table></div>`;
};
const pagerBar = (total=2889, page=1, pages=116) => `<div class="pager" data-pager>共 <b>${fmt(total)}</b> 条 · <span class="pgn" data-act="page-first">«</span><span class="pgn" data-act="page-prev">‹</span>${Array.from({length:Math.min(5,pages)},(_,i)=>`<span class="pg ${i+1===page?'active':''}" data-act="page-go" data-p="${i+1}">${i+1}</span>`).join('')}<span class="pgn" data-act="page-next">›</span><span class="pgn" data-act="page-last">»</span> 每页 <select class="form-select" style="width:74px;padding:2px 4px"><option>25 行/页</option><option>50 行/页</option><option>100 行/页</option></select></div>`;
const rangeQuick = (prefix, active=12) => `<span class="seg">${[[6,'近半年'],[12,'近1年'],[36,'近3年'],[60,'近5年'],[120,'近10年']].map(([m,n])=>`<button class="seg-btn ${m===active?'active':''}" data-act="range" data-prefix="${prefix}" data-months="${m}">${n}</button>`).join('')}</span>`;
const filterBar = (withCode=true) => `<div class="ctl-params">${withCode?`<input class="prec-input" list="filterCodeOptions" placeholder="选择或输入代码" style="width:150px">`:''}<input class="prec-input" type="date" value="${LAST}" style="width:130px"><span class="hint">~</span><input class="prec-input" type="date" value="${LAST}" style="width:130px">${rangeQuick('data',12)}<select class="form-select" style="width:88px"><option>25 行/页</option><option>50 行/页</option><option>100 行/页</option></select><button class="btn btn-primary btn-sm" data-act="filter">筛选</button><button class="btn btn-sm" data-act="filter-reset">默认区间</button><span class="hint" data-filter-count>共 ${fmt(2889)} 条</span></div>`;
const exportBtns = () => `<div class="ctl-params"><button class="btn btn-sm" data-act="export" data-k="macro"><i class="ti ti-file-export"></i> 导出宏观</button><button class="btn btn-sm" data-act="export" data-k="etf"><i class="ti ti-file-export"></i> 导出 ETF</button><button class="btn btn-sm" data-act="export" data-k="index"><i class="ti ti-file-export"></i> 导出指数</button><button class="btn btn-primary btn-sm" data-act="export" data-k="full"><i class="ti ti-file-zip"></i> 导出全量</button></div>`;

/* ==========================================================================
 * 视图定义（覆盖原系统全部功能）
 * ========================================================================== */
function previewView(kind){
  const names = {macro:'宏观数据',etf:'ETF 日频',index:'指数日线',trade:'日交易',files:'输出文件'};
  const [cols, rows] = DATA_TABS[kind];
  return head(`采集数据 · ${names[kind]}`,'筛选 / 排序 / 分页 / 导出 · 少量样例数据')
    + `<div class="card"><div class="card-header"><h3><i class="ti ti-table" style="color:var(--blue)"></i> ${names[kind]}</h3><div style="margin-left:auto">${exportBtns()}</div></div>
    <div class="card-body"><div style="margin-bottom:10px">${filterBar()}</div>
    ${tbl(cols, rows, {action: kind==='files'?'download':null, actionLabel:'下载'})}
    ${pagerBar()}</div></div>`;
}
function monitorView(kind){
  const names = {overview:'运行总览',failures:'失败明细',health:'接口健康度',detail:'调用明细',stats:'采集量统计'};
  const failMap = {overview:[],failures:[],health:[],detail:[],stats:[]};
  const opts = kind==='overview' ? {expand:true,expandKey:'run'}
    : kind==='failures' ? {action:'retry-one',actionLabel:'重跑此条'}
    : {};
  return head(`接口监控 · ${names[kind]}`,'成功率 / 失败明细 / 接口健康度 / 调用明细 / 采集量统计')
    + statRow(
      statCard('api','ic-blue',fmt(S.today_calls),'今日调用','全通道','')+
      statCard('circle-check','ic-green',fmt(S.today_calls-S.failed_total),'成功调用',((S.today_calls-S.failed_total)/S.today_calls*100).toFixed(1)+'%','t-green')+
      statCard('circle-x','ic-red',fmt(S.failed_total),'累计失败','历史留痕','t-orange')+
      statCard('calendar','ic-purple',LAST,'最新交易日','',''))
    + `<div class="card"><div class="card-header"><h3><i class="ti ti-activity" style="color:var(--blue)"></i> ${names[kind]}</h3>
      ${kind==='failures'?`<button class="btn btn-warn btn-sm" style="margin-left:auto" data-act="retry-all"><i class="ti ti-rotate-clockwise"></i> 批量重跑</button>`:''}
      <button class="btn btn-sm" ${kind!=='failures'?'style="margin-left:auto"':''} data-act="export" data-k="monitor"><i class="ti ti-file-export"></i> 导出监控</button>
      <button class="btn btn-danger btn-sm" data-act="monitor-clean-toggle"><i class="ti ti-eraser"></i> 清理监控</button></div>
    <div class="card-body">
      <div class="card" data-monitor-clean style="display:none;background:#fbfcfe;margin-bottom:12px"><div class="card-body">
        <div class="ctl-params" style="margin-bottom:8px"><label class="prec-field">时间范围</label><input class="prec-input" type="date" style="width:130px"><span class="hint">~</span><input class="prec-input" type="date" style="width:130px">
          <select class="form-select" style="width:100px"><option>全部状态</option><option>成功</option><option>失败</option></select>
          <select class="form-select" style="width:110px"><option>全部类型</option><option>全量</option><option>定时</option></select>
          <select class="form-select" style="width:90px"><option>全部类别</option><option>宏观</option><option>ETF</option></select></div>
        <div class="ctl-params" style="margin-bottom:8px"><label class="prec-field">关键词 <span class="muted-inline">接口名 / 接口键 / 失败原因</span></label><input class="prec-input" placeholder="留空不限" style="width:220px"></div>
        <div class="ctl-params"><button class="btn btn-sm" data-act="mc-preview">预览影响</button><button class="btn btn-danger btn-sm" data-act="mc-exec">执行清理</button><button class="btn btn-sm" data-act="mc-reset">重置</button></div>
        <div class="clean-result" data-mc-result>—</div></div></div>
      <div class="ctl-params" style="margin-bottom:10px">
        <input class="prec-input" placeholder="接口名/接口键/失败原因 关键词" style="width:200px">
        <select class="form-select" style="width:120px"><option>全部运行类型</option><option>全量 full</option><option>定时 scheduled</option><option>精确采集 interface</option></select>
        <select class="form-select" style="width:100px"><option>全部类别</option><option>宏观</option><option>ETF</option></select>
        <select class="form-select" style="width:100px"><option>全部状态</option><option>成功</option><option>失败</option></select>
        <input class="prec-input" type="date" value="${LAST}" style="width:125px">
        <button class="btn btn-sm" data-act="filter"><i class="ti ti-filter"></i> 筛选</button><button class="btn btn-sm" data-act="filter-reset">重置</button>
        <span class="hint" data-filter-count>共 ${fmt(MONITOR_DATA[kind].length)} 行</span>${kind==='failures'?'<span class="hint">失败明细每行可单独重跑</span>':''}</div>
      ${tbl(MONITOR_HEADERS[kind], MONITOR_DATA[kind], opts)}
      ${pagerBar(MONITOR_DATA[kind].length,1,1)}</div></div>`;
}
const MONITOR_HEADERS = {
  overview:['运行ID','运行时间','运行类型','调用数','成功数','失败数','成功率','耗时'],
  failures:['任务ID','接口名称','类别','发生时间','失败原因','重跑结果','状态'],
  health:['接口名称','接口键','类别','累计调用','成功','成功率','失败数','状态'],
  detail:['运行ID','运行时间','接口名称','类别','状态','数据行数','耗时','失败原因'],
  stats:['类别','标的数','今日新增','累计行数','频率','最新日期'],
};
function dashShell(grp){
  const ctl = grp==='macro'
    ? `<label class="prec-field">宏观范围 <select class="form-select" style="width:100px" data-act="dash-macro-range"><option>近一年</option><option>近6个月</option><option>近3年</option><option>近5年</option><option>全部</option></select></label><span class="hint">数据来源：派生指标 / 宏观观测 / 上交所日线</span>`
    : `${grp==='etf'?`<label class="prec-field">叠加指数 <select class="form-select" style="width:120px"><option>上证指数 000001</option><option>沪深300 000300</option><option>科创50 000688</option><option>创业板指 399006</option></select></label>`:''}
       <label class="prec-field">开始 <input class="prec-input" type="date" data-range-start style="width:125px"></label>
       <label class="prec-field">结束 <input class="prec-input" type="date" data-range-end value="${LAST}" style="width:125px"></label>
       ${rangeQuick('dash-'+grp,12)}<span class="hint" data-range-hint>点选快捷范围回显真实区间；手改日期进入自定义</span>`;
  return head(`数据看板 · ${grp==='macro'?'宏观指标':grp==='etf'?'ETF 维度':'日交易'}`,
      grp==='macro'?'8 张图 · 统一日期联动':grp==='etf'?'4 张图 · 汇金 / 黄金 · 日期联动':'2 张图 · 图1图2各一行 · 两融双 grid 联动')
    + `<div class="card"><div class="card-header"><h3><i class="ti ti-chart-line" style="color:var(--blue)"></i> ${grp==='macro'?'宏观指标':grp==='etf'?'ETF 维度':'日交易'}</h3>
      <div style="flex:1"></div><button class="btn btn-sm" data-act="dash-refresh"><i class="ti ti-refresh"></i> 刷新图表</button><span class="hint" data-dash-meta>宏观 8 图 · ETF 4 图 · 交易 2 图</span></div>
    <div class="card-body"><div class="ctl-params" style="margin-bottom:12px">${ctl}</div>
      <div class="grid-2 ${grp==='trade'?'one-col':''}" data-dash-charts></div></div></div>`;
}

const VIEWS = {
  /* ---------------- 总览 ---------------- */
  ovCollect:()=>head('采集数据概览','系统数据资产一屏总览 · 最近交易日 '+LAST)+statRow(
    statCard('database','ic-blue',fmt(S.macro_rows),'宏观观测',fmt(S.macro_indicators)+' 指标','t-blue')+
    statCard('coins','ic-green',fmt(S.etf_rows),'ETF 日频',fmt(S.etf_codes)+' 标的','t-blue')+
    statCard('chart-line','ic-purple',fmt(S.index_rows),'指数日线',fmt(S.index_codes)+' 指数','t-blue')+
    statCard('scale','ic-orange',fmt(S.trade_rows),'交易观测','5 张原始表','t-green'))
    +`<div class="grid-2"><div class="card"><div class="card-header"><h3><i class="ti ti-chart-area" style="color:var(--blue)"></i> 数据资产分布</h3></div><div class="card-body"><div id="ovAssetChart" class="chart-box sm"></div></div></div>
      <div class="card"><div class="card-header"><h3><i class="ti ti-clock" style="color:var(--ok)"></i> 最近运行</h3></div><div class="card-body">${tbl(['运行ID','时间','类型','状态','耗时'],[
        ['...113540_scheduled','11:35:40','调度','成功','186s'],['...001004_interface','00:10:04','接口','成功','12.1s'],
        ['...000929_interface','00:09:29','接口','成功','12.2s'],['...000849_interface','00:08:49','接口','失败','11.2s']])}</div></div></div>
      <div class="card"><div class="card-header"><h3><i class="ti ti-info-circle" style="color:var(--muted)"></i> 模块导航</h3></div><div class="card-body"><p class="hint" style="font-size:12px;line-height:1.9">顶部 5 模块：<b>总览 / 数据采集 / 数据处理 / 数据看板 / 运维监控</b>。左侧列出当前模块全部功能项，点击切换。所有按钮、Tab、筛选、分页、图表联动均可操作。</p></div></div>`,

  /* ---------------- 数据采集：控制台 ---------------- */
  console:()=>head('采集控制台','整体 / 部分采集 · 3 种采样方式 + 实时监控')+statRow(
    statCard('database','ic-blue',fmt(S.macro_rows),'宏观数据',fmt(S.macro_indicators)+' 指标','t-blue')+
    statCard('coins','ic-green',fmt(S.etf_rows),'ETF 数据',fmt(S.etf_codes)+' 标的','t-blue')+
    statCard('chart-line','ic-purple',fmt(S.index_rows),'指数数据',fmt(S.index_codes)+' 指数','t-blue')+
    statCard('alert-triangle','ic-red',fmt(S.failed_total),'累计失败','需关注','t-orange'))
    +`<div class="card"><div class="card-header"><h3><i class="ti ti-player-play" style="color:var(--blue)"></i> 采样方式</h3><span class="sub">整体 / 部分 · 参数行内可调 · 点击操作按钮观察实时日志</span></div>
      <div class="card-body"><div style="display:grid;grid-template-columns:150px 70px 1fr 70px 90px;gap:8px;font-size:10px;color:#9aabc2;padding:0 0 6px;border-bottom:1px solid #edf1f7"><span>采样方式</span><span>范围</span><span>参数（行内可调）</span><span>启用</span><span>操作</span></div>
      <div class="ctl-row" style="grid-template-columns:150px 70px 1fr 70px 90px"><div class="ctl-name"><div class="ci" style="background:#3b5bdb"><i class="ti ti-refresh"></i></div><div><b>全量采集</b><small>宏观 + ETF + 指数</small></div></div><span class="scope-pill scope-whole">整体</span>
        <div class="ctl-params"><input class="prec-input" type="date" value="${LAST}" style="width:120px"><span class="hint">~</span><input class="prec-input" type="date" value="${LAST}" style="width:120px"><button class="btn btn-sm" data-act="date-default">默认</button></div>
        <div style="text-align:center"><span class="s-always">手动</span></div><div><button class="btn btn-primary btn-sm" data-act="collect-full">立即采集</button></div></div>
      <div class="ctl-row" style="grid-template-columns:150px 70px 1fr 70px 90px"><div class="ctl-name"><div class="ci" style="background:#16855b"><i class="ti ti-target"></i></div><div><b>精确采集</b><small>类别 → 具体口径 + 时间区间</small></div></div><span class="scope-pill scope-part">部分</span>
        <div class="ctl-params"><select class="form-select" style="width:100px" data-act="prec-cat"><option value="macro">宏观指标</option><option value="etf">ETF 基金</option><option value="index">指数</option><option value="trade">交易（原始）</option></select><span class="hint">→</span><select class="form-select" style="width:100px" data-prec-code><option>M1 同比</option><option>社融增量</option></select><input class="prec-input" type="date" value="2025-01-01" style="width:118px" data-prec-start><span class="hint">~</span><input class="prec-input" type="date" value="${LAST}" style="width:118px" data-prec-end></div>
        <div style="text-align:center"><span class="s-always">手动</span></div><div><button class="btn btn-primary btn-sm" data-act="collect-prec">采集所选</button></div></div>
      <div class="ctl-row" style="grid-template-columns:150px 70px 1fr 70px 90px"><div class="ctl-name"><div class="ci" style="background:#c2410c"><i class="ti ti-file-import"></i></div><div><b>文件导入</b><small>份额与规模_代码.xlsx</small></div></div><span class="scope-pill scope-ops">ETF数据</span>
        <div class="ctl-params"><input class="prec-input" type="file" style="width:220px;font-size:10px" data-import-file><button class="btn btn-sm" data-act="import">导入数据</button></div>
        <div style="text-align:center"><span class="s-always">手动</span></div><div><span class="hint" style="font-size:10px">≤10 文件</span></div></div>
      <div class="log-panel" style="margin-top:12px;display:block"><div class="log-head"><span>实时监控 · 实时日志</span><span class="log-scope">全部线程 · 最近5000行</span><div class="log-tools"><label><input type="checkbox" id="logAutoScroll" checked> 自动滚动</label><button class="log-tool" data-act="log-bottom" title="回到底部">↓</button><button class="log-tool" data-act="log-toggle">展开</button><span class="log-dot"></span></div></div>
        <div class="log-strip" data-act="log-toggle"><span class="log-strip-label">最新</span><span id="logMarquee" class="OK">[11:35:40] 定时调度完成 · 284 接口全部成功</span></div>
        <div class="log-body-wrap" id="logBodyWrap"><div id="logBody">${[
          ['11:35:40','OK','[调度] 触发每日全量采集 run_id=20261126_033517'],
          ['11:35:41','INFO','[宏观] 开始采集：区间 2026-11 ~ 2026-11（定时调度·仅当月）'],
          ['11:35:58','OK','[宏观] 完成 25 项指标，写入 macro_observation'],
          ['11:36:12','INFO','[ETF] 采集 26 只标的日频数据'],
          ['11:36:47','OK','[指数] 采集 4 个指数日线完成'],
          ['11:38:06','OK','[派生] 增量重算完成，影响 3 个指标/月份'],
          ['11:38:07','WARN','[接口] 天天基金限流，17 个上市日期走缓存'],
        ].map(r=>`<div class="log-line"><span class="t">${r[0]}</span> <span class="${r[1]}">${esc(r[2])}</span></div>`).join('')}</div></div></div></div></div>`,

  /* ---------------- 数据采集：配置 ---------------- */
  maccfg:()=>head('宏观采集配置','指标可增删 · 数据源与区间配置')+`<div class="card"><div class="card-header"><h3><i class="ti ti-adjustments" style="color:var(--ok)"></i> 宏观指标配置</h3><span class="sub">17 原指标 + GDP + 海外 · 可增删 / 启停</span></div><div class="card-body">
    <div class="ctl-params" style="margin-bottom:11px;padding:9px 10px;background:#f7f9fd;border:1px solid var(--line);border-radius:7px">
      <label class="prec-field"><b>新增指标</b></label>
      <input class="prec-input" placeholder="指标代码" style="width:125px" data-macro-code>
      <input class="prec-input" placeholder="名称" style="width:135px" data-macro-name>
      <input class="prec-input" placeholder="类别" style="width:78px" data-macro-cat>
      <input class="prec-input" placeholder="数据源" style="width:88px" data-macro-src>
      <input class="prec-input" placeholder="频率" style="width:60px" data-macro-freq>
      <button class="btn btn-primary btn-sm" data-act="macro-add"><i class="ti ti-plus"></i> 新增</button></div>
    <div class="ctl-params" style="margin-bottom:12px"><label class="prec-field">默认区间 <input class="prec-input" type="date" value="2025-01-01" style="width:125px"></label><span class="hint">~</span><input class="prec-input" type="date" value="${LAST}" style="width:125px"><button class="btn btn-primary btn-sm" data-act="save-config">保存配置</button><span class="hint" data-config-hint>勾选启用项后保存</span></div>
    <div class="table-responsive" data-macro-table>${tbl(['启用','指标代码','名称','类别','数据源','频率'],MACRO_INDS.map(r=>[
      `<label class="switch-row"><input type="checkbox" checked data-macro-enable></label>`,...r]),{action:'macro-remove',actionLabel:'删除'})}</div></div></div>`,

  idxcfg:()=>head('指数采集配置','4 个指数 · 日线采集')+`<div class="card"><div class="card-header"><h3><i class="ti ti-chart-dots" style="color:var(--blue)"></i> 指数标的配置</h3><span class="sub">灵活增删 · 自定义区间</span></div><div class="card-body">
    <div class="ctl-params"><select class="form-select" style="width:170px" data-idx-new><option>000001 上证指数</option><option>000300 沪深300</option><option>000688 科创50</option><option>399006 创业板指</option></select><button class="btn btn-sm" data-act="idx-add"><i class="ti ti-plus"></i> 添加</button></div>
    <div class="chips" data-chips="idx">${INDEX_ITEMS.map(x=>`<span class="chip"><i class="ti ti-chart-dots"></i> ${x[0]} ${x[1]}<span class="x" data-act="chip-remove">×</span></span>`).join('')}</div>
    <div class="ctl-params"><label class="prec-field">采集区间 <input class="prec-input" type="date" style="width:125px"></label><span class="hint">~</span><input class="prec-input" type="date" value="${LAST}" style="width:125px"><label class="prec-field">回看 <input class="prec-input" type="number" value="1" style="width:60px"> 天</label><button class="btn btn-primary btn-sm" data-act="save-config">保存配置</button></div>
    <div class="table-responsive" style="margin-top:12px">${tbl(['代码','名称'],INDEX_ITEMS.map(x=>[x[0],x[1]]),{action:'idx-row-remove',actionLabel:'删除'})}</div></div></div>`,

  etfcfg:()=>head('ETF 采集配置','26 只标的 · 分组标签与上市日期缓存')+`<div class="card"><div class="card-header"><h3><i class="ti ti-coins" style="color:var(--ok)"></i> ETF 标的配置</h3><span class="sub">宽基 / 汇金 / 黄金 · 自动解析上市日</span></div><div class="card-body">
    <div class="ctl-params"><input class="prec-input" placeholder="输入 6 位基金代码" maxlength="6" style="width:150px" data-etf-new><button class="btn btn-sm" data-act="etf-add"><i class="ti ti-plus"></i> 添加</button></div>
    <div class="chips" data-chips="etf">${ETF_ITEMS.slice(0,6).map(x=>`<span class="chip"><i class="ti ti-coins"></i> ${x[0]} ${x[1]} <span class="mini_tag">${x[2].split('/')[0]}</span><span class="x" data-act="chip-remove">×</span></span>`).join('')}</div>
    <div class="ctl-params"><label class="prec-field">默认区间 <input class="prec-input" type="date" style="width:125px"></label><span class="hint">~</span><input class="prec-input" type="date" value="${LAST}" style="width:125px"><label class="prec-field">最近 <input class="prec-input" type="number" value="60" style="width:60px"> 个交易日</label><button class="btn btn-primary btn-sm" data-act="save-config">保存配置</button></div>
    <div class="table-responsive" style="margin-top:12px">${tbl(['代码','名称','标签','上市日期'],ETF_ITEMS.map(x=>[x[0],x[1],x[2],x[3]]),{action:'etf-row-remove',actionLabel:'删除'})}</div></div></div>`,

  /* ---------------- 数据采集：预览 ---------------- */
  'prev-macro':()=>previewView('macro'), 'prev-etf':()=>previewView('etf'),
  'prev-index':()=>previewView('index'), 'prev-trade':()=>previewView('trade'),
  'prev-files':()=>previewView('files'),

  /* ---------------- 数据采集：清理 ---------------- */
  clean:()=>head('数据清理','危险操作 · 按口径 / 时间范围精确删除')+`<div class="card"><div class="card-header"><h3><i class="ti ti-eraser" style="color:var(--err)"></i> 清理预览</h3><span class="sub">不可撤销 · 不影响配置 / 监控 / 导出</span></div><div class="card-body">
    <div class="ctl-params" style="margin-bottom:10px"><label class="prec-field">数据类别</label><span class="seg" data-clean-cat><button class="seg-btn active" data-act="clean-cat" data-cat="macro">宏观指标</button><button class="seg-btn" data-act="clean-cat" data-cat="etf">ETF 基金</button><button class="seg-btn" data-act="clean-cat" data-cat="index">指数</button></span></div>
    <div class="ctl-params" style="margin-bottom:10px"><label class="prec-field">细分口径 <span class="muted-inline">(全部)</span></label><select class="form-select" style="width:200px" data-clean-code><option>全部（该类别整体）</option><option>M1 同比</option><option>M2 同比</option></select></div>
    <div class="ctl-params" style="margin-bottom:12px"><label class="prec-field">时间范围 <span class="muted-inline">留空 = 全部</span></label><input class="prec-input" type="date" style="width:130px"><span class="hint">~</span><input class="prec-input" type="date" style="width:130px"></div>
    <div class="ctl-params"><button class="btn btn-sm" data-act="clean-preview"><i class="ti ti-eye"></i> 预览影响</button><button class="btn btn-danger btn-sm" data-act="clean-exec"><i class="ti ti-trash"></i> 执行清理</button><button class="btn btn-sm" data-act="clean-reset">重置</button></div>
    <div class="clean-result" data-clean-result>预览结果：<span class="hint">将删除 0 行（请先预览影响）。派生指标表不在清理范围内。</span></div></div></div>`,

  /* ---------------- 数据处理 ---------------- */
  macrocalc:()=>head('宏观计算 · 派生指标','14 项指标 · 手动重算兜底 · 定时调度见「运维监控 · 任务调度」')+`<div class="trigger-grid" style="grid-template-columns:1fr">
    <div class="trigger-card"><div class="tc-head"><h3>🔁 手动重算 <small>兜底</small></h3><span class="badge bg-blue-lt">已就绪</span></div><div class="tc-body">
      <div class="tc-row"><button class="btn btn-primary btn-sm" data-act="recalc-all">重新计算全部</button><button class="btn btn-sm" data-act="export" data-k="derived">导出 CSV</button></div>
      <div class="tc-row"><label class="prec-field">按月 <input class="prec-input" type="month" value="2026-10" style="width:110px"></label><button class="btn btn-sm" data-act="recalc-month">重算该月</button></div>
      <div class="tc-row"><label class="prec-field">按指标 <select class="form-select" style="width:150px"><option>M1 同比</option><option>社融增量</option><option>CPI-PPI 剪刀差</option></select></label><button class="btn btn-sm" data-act="recalc-code">重算该指标</button></div>
      <div class="tc-note">局部重算按业务键幂等覆盖；全量重算才整表替换清理陈旧口径行</div></div></div></div>
    <div class="card"><div class="card-header"><h3><i class="ti ti-list-check" style="color:var(--blue)"></i> 指标口径</h3><label class="prec-field" style="margin-left:auto">查看月份 <input class="prec-input" type="month" value="2026-10" style="width:110px"></label></div><div class="card-body" data-derived-body></div></div>
    <div class="card"><div class="card-header"><h3><i class="ti ti-info-circle" style="color:var(--muted)"></i> 触发逻辑说明</h3></div><div class="card-body"><ol class="steps">
      <li><b>触发条件</b>：服务运行时按「运维监控 · 任务调度」中的配置每日到点触发一次，同一天只触发一次；未启用/未到点/今日已触发/任务占用时不执行。</li>
      <li><b>步骤 1~5</b>：触发 → 提交采集 → 宏观/ETF/指数采集 → 写 SQLite 业务表（幂等 upsert）→ 采集完成统计，全程写入监控日志。</li>
      <li><b>步骤 6/6 派生增量重算</b>：按本次 <code>run_id</code> 推导受影响的派生 <code>(指标, 月份)</code>，只重算目标行并按 <code>biz_key</code> 幂等 upsert。</li>
      <li><b>手动重算（兜底）</b>：「全部」全量重算整表替换；「按月」「按指标」按业务键幂等覆盖只影响目标子集。</li>
      <li><b>口径要点</b>：进出口同比用官方累计；贷款新增为余额差分；黄金新增为储备差分×31.1034768÷100；社融存量取官方增速。</li>
      <li><b>看板联动</b>：看板各派生指标图直接读取 <code>derived_indicator</code>，重算后自动生效；宏观与 ETF 日期口径一致。</li>
    </ol></div></div>`,

  etfcalc:()=>head('ETF 计算','流通份额变化 / 申赎净流入 · 汇金维度')+`<div class="card"><div class="card-header"><h3><i class="ti ti-calculator" style="color:var(--blue)"></i> ETF 计算</h3><span class="sub">数据来自 etf_observation 全量历史</span></div><div class="card-body">
    <div class="formula-note"><div><b>流通份额变化</b> = 当日份额 − 上一交易日份额（亿份）</div><div><b>申赎净流入</b> = 份额变化 × 当日单位净值（亿元），剔除价格涨跌的市值波动；净值单日涨跌超 ±20% 视为份额折算，净流入置空</div></div>
    <div class="ctl-params" style="margin-bottom:12px"><label class="prec-field">查看范围 <select class="form-select" style="width:100px"><option>近半年</option><option selected>近一年</option><option>近3年</option><option>近5年</option><option>全部</option></select></label><label class="prec-field">起始 <input class="prec-input" type="date" style="width:125px"></label><label class="prec-field">结束 <input class="prec-input" type="date" value="${LAST}" style="width:125px"></label><button class="btn btn-sm" data-act="etfcalc-refresh"><i class="ti ti-refresh"></i> 刷新</button><button class="btn btn-sm" data-act="export" data-k="etfcalc"><i class="ti ti-file-export"></i> 导出 CSV</button><span class="hint">共 23 条</span></div>
    <div class="mini_tag" style="display:inline-block;margin-bottom:8px">汇金合计净流入 +38.6 亿元</div>
    ${tbl(['日期','代码','名称','份额变化(亿份)','净流入(亿元)','单位净值'],[
      [LAST,'510300','沪深300ETF','+3.2','+12.4','4.02'],[LAST,'510050','上证50ETF','-0.8','-3.1','3.28'],
      [LAST,'518880','黄金ETF','+1.4','+2.7','9.16'],['2026-11-21','510300','沪深300ETF','+2.1','+8.2','3.98']])}
    <div class="card-header" style="margin:16px -14px 10px;padding:10px 14px"><h3>汇金维度 · 每个交易日总份额</h3><span class="sub">23 只汇金 ETF 份额合计 · 点击展开明细</span></div>
    ${tbl(['日期','汇金总份额(亿份)','较上日','明细'],[
      [LAST,'8,642.5','+18.3','展开 ▸'],['2026-11-21','8,624.2','+6.8','展开 ▸'],['2026-11-20','8,617.4','-2.1','展开 ▸']],{action:'etf-share-detail',actionLabel:'展开 ▸'})}</div></div>`,

  tradecalc:()=>head('交易计算','从 5 张原始表派生 10 项指标')+`<div class="trigger-grid">
    <div class="trigger-card"><div class="tc-head"><h3>🔁 手动重算 <small>兜底</small></h3><span class="badge bg-blue-lt">已就绪</span></div><div class="tc-body">
      <div class="tc-row"><button class="btn btn-primary btn-sm" data-act="trade-recalc-all">重新计算全部</button><label class="prec-field">按交易日 <input class="prec-input" type="date" value="${LAST}" style="width:130px" data-trade-recalc-date></label><button class="btn btn-sm" data-act="trade-recalc-date">重算该日</button></div>
      <div class="tc-note">全量重算整表替换；按日重算只覆盖该交易日。差分/占比分母始终基于原始值计算。</div></div></div>
    <div class="trigger-card"><div class="tc-head"><h3>📊 原始数据覆盖</h3></div><div class="tc-body">
      <div class="tc-row"><span class="hint">交易所概况 / 两融账户 / 全A个股快照 · 共 ${fmt(S.trade_rows)} 交易日</span></div>
      <div class="tc-note">快照仅当日不可回溯，集中度与涨跌家数自采集日起累计。五类原始表：exchange / margin / snapshot / breadth / congestion。</div></div></div></div>
    <div class="formula-note"><div><b>全A成交额</b> = 沪成交金额(亿元) + 深成交金额(元)÷1e8</div><div><b>成交集中度(前5%)</b> = 个股成交额降序前 5% 之和 ÷ 全A成交额 ×100% · <b>两融余额</b> = 融资余额 + 融券余额</div><div><b>融资余额占流通市值比</b> = 融资余额 ÷（沪流通市值 + 深流通市值÷1e8）×100% · <b>融资买入额占比</b> = 融资买入额 ÷ 全A成交额 ×100%</div><div><b>融资保证金比例</b> 为政策阶梯常量（当前 100%），按交易日匹配生效值 · <b>涨跌家数</b> 由快照涨跌幅统计</div></div>
    <div class="card"><div class="card-header"><h3><i class="ti ti-list-check" style="color:var(--blue)"></i> 10 项指标最新值</h3><span class="sub">${LAST}</span></div><div class="card-body">${tbl(['指标代码','指标名称','最新值','单位','数据来源'],TRADE_LATEST)}</div></div>`,

  /* ---------------- 数据看板 ---------------- */
  dashmacro:()=>dashShell('macro'), dashetf:()=>dashShell('etf'), dashtrade:()=>dashShell('trade'),

  /* ---------------- 运维监控 ---------------- */
  'mon-overview':()=>monitorView('overview'), 'mon-failures':()=>monitorView('failures'),
  'mon-health':()=>monitorView('health'), 'mon-detail':()=>monitorView('detail'),
  'mon-stats':()=>monitorView('stats'),

  tasksched:()=>head('任务调度','每日定时调度 · 采集完成后自动增量重算')+`<div class="trigger-grid" style="grid-template-columns:1fr">
    <div class="trigger-card"><div class="tc-head"><h3>⏰ 定时调度 <small>主触发</small></h3><label class="switch-row"><input type="checkbox" id="dpSchedEnabled" checked> 启用</label></div><div class="tc-body">
      <div class="tc-row"><label>触发时刻</label><input class="prec-input" type="time" value="03:17" style="width:100px"><label>采集范围</label><select class="form-select" style="width:160px"><option>全量 full</option><option>仅宏观 macro（当月）</option><option>仅 ETF etf</option><option>仅指数 index</option></select><label>下次运行</label><span class="hint" id="dpSchedNext">${S.scheduler.next_run}</span></div>
      <div class="tc-row tc-row-foot"><button class="btn btn-primary btn-sm" data-act="save-sched">保存调度配置</button><button class="btn btn-sm" data-act="sched-run-now"><i class="ti ti-player-play"></i> 立即触发一次</button></div>
      <div class="tc-note">采集完成后自动执行步骤 6/6 增量重算（只算本次变更涉及的指标/月份）· 每天仅触发一次 · 忙时跳过不补跑</div></div></div></div>
    <div class="card"><div class="card-header"><h3><i class="ti ti-info-circle" style="color:var(--muted)"></i> 触发逻辑说明</h3></div><div class="card-body"><ol class="steps">
      <li><b>触发条件</b>：服务运行时按本页配置每日到点触发一次，同一天只触发一次；未启用 / 未到点 / 今日已触发 / 任务占用时不执行，日志中给出原因。</li>
      <li><b>步骤 1~5</b>：触发 → 提交采集任务 → 宏观 / ETF / 指数采集 → 写 SQLite 业务表（幂等 upsert）→ 采集完成统计，全程写入实时监控日志。</li>
      <li><b>步骤 6/6 派生增量重算</b>：按本次 <code>run_id</code> 读取写入 <code>macro_observation</code> 的原始 <code>(指标, 月份)</code>，经依赖映射推导受影响的派生行，<b>只重算这些目标行</b>并按 <code>biz_key</code> 幂等 upsert。</li>
      <li><b>手动重算（兜底）</b>：「全部」全量重算并整表替换；「按月」「按指标」按业务键幂等覆盖只影响目标子集，在「数据处理」模块操作。</li>
      <li><b>宏观当月模式</b>：定时调度中宏观采集锁定 <code>start = end = 当月 YYYY-MM</code>，避免无谓回溯；手动全量采集不受此限制。</li>
      <li><b>看板联动</b>：数据看板各图直接读取派生表，重算完成后自动生效。</li>
    </ol></div></div>
    <div class="card"><div class="card-header"><h3><i class="ti ti-history" style="color:var(--blue)"></i> 最近调度记录</h3></div><div class="card-body">${tbl(['触发时间','运行ID','范围','状态','调用数','耗时'],[
      ['2026-11-26 03:17','20261126_031700_scheduled','全量','<span class="tag success">成功</span>','284','186s'],
      ['2026-11-25 03:17','20261125_031700_scheduled','全量','<span class="tag success">成功</span>','284','191s'],
      ['2026-11-24 03:17','20261124_031700_scheduled','全量','<span class="tag warn">跳过</span>','—','今日已触发'],
      ['2026-11-23 03:17','20261123_031700_scheduled','全量','<span class="tag success">成功</span>','282','188s'],
    ])}</div></div>`,
};

/* ==========================================================================
 * 渲染 / 导航
 * ========================================================================== */
function buildSidebar(){
  const v = $('.view.active');
  let h = '';
  NAV[curMod].groups.forEach(g=>{
    h += `<div class="cap">${g.cap}</div>`;
    g.items.forEach(it=>{
      h += `<div class="fn ${it.id===curView?'active':''}" data-view="${it.id}"><i class="ti ti-${it.icon}"></i><span>${it.name}</span>${it.badge?`<span class="badge badge-sm ${it.badge==='TODO'?'bg-secondary':'bg-red'}">${it.badge}</span>`:''}</div>`;
    });
  });
  h += `<div class="sidebar-foot">交互原型 v5.3 · 单文件自包含<br>© 2026 投研数据团队</div>`;
  $('#sidebar').innerHTML = h;
}
function renderViews(){
  $('#views').innerHTML = Object.entries(VIEWS).map(([k,v])=>`<section class="view ${k===curView?'active':''}" id="v-${k}">${v()}</section>`).join('');
  fillStatic();
}
function switchView(id){
  if(!VIEWS[id]) return;
  curView = id;
  $$('.view').forEach(x=>x.classList.remove('active'));
  const el = byId('v-'+id); if(el) el.classList.add('active');
  $$('.fn').forEach(x=>x.classList.toggle('active',x.dataset.view===id));
  if(id.startsWith('dash')) setTimeout(renderDash, 60);
  if(id==='ovCollect') setTimeout(renderOverview, 60);
  if(curMod==='derived') setTimeout(fillDerived, 40);
  window.scrollTo({top:0,behavior:'smooth'});
}
function switchMod(mod){
  if(!NAV[mod]) return;
  curMod = mod;
  $$('.mod').forEach(x=>x.classList.toggle('active',x.dataset.mod===mod));
  curView = NAV[mod].groups[0].items[0].id;
  buildSidebar(); switchView(curView);
}

/* ==========================================================================
 * 图表（ECharts）
 * ========================================================================== */
function filteredData(chart, months){
  const categories = (chart.series[0]?.data||[]).map(p=>p[0]).sort();
  if(!months || months>=999) return categories;
  // 有些图（如 ETF 净流入细分）x 轴是名称而非日期，按时间过滤时原样保留
  const end = new Date(categories[categories.length-1]||LAST);
  if(isNaN(end.getTime())) return categories;
  const start = new Date(end); start.setMonth(start.getMonth()-months);
  if(isNaN(start.getTime())) return categories;
  const lo = start.toISOString().slice(0,10);
  return categories.filter(c=>{
    const d = new Date(c);
    return isNaN(d.getTime()) || c >= lo;
  });
}
function axisOpt(ax,i){
  return {type:'value',name:ax?.name||'',position:ax?.position||'left',offset:ax?.offset||0,
    nameTextStyle:{fontSize:9,color:'#8a97ac'},axisLabel:{fontSize:9,color:'#8a97ac'},
    splitLine:{show:i===0,lineStyle:{color:'#edf1f7'}},axisLine:{show:i===0}};
}
function renderChartNode(node, chart, months){
  if(!node) return;
  const ins = echarts.getInstanceByDom(node)||echarts.init(node);
  const categories = filteredData(chart, months);
  const dataOf = s=>{ const m=new Map(s.data||[]); return categories.map(c=>m.has(c)?m.get(c):null); };
  const series = (chart.series||[]).map((s,i)=>({
    name:s.name, type:s.type||'line', data:dataOf(s), yAxisIndex:s.y_axis_index||0,
    smooth:true, showSymbol:false, connectNulls:true,
    lineStyle:{width:2,color:CLR[i%CLR.length]}, itemStyle:{color:CLR[i%CLR.length]},
  }));
  const maxIdx = Math.max(0,...series.map(s=>s.yAxisIndex));
  const ax = (chart.y_axes||[]).slice();
  while(ax.length<=maxIdx) ax.push({name:'占比 (%)',position:ax.length%2?'right':'left'});
  ins.setOption({animation:false,color:CLR,
    tooltip:{trigger:'axis',axisPointer:{type:'cross'},confine:true},
    legend:{top:2,type:'scroll',textStyle:{fontSize:10}},
    grid:{left:58,right:ax.length>1?40+(ax.length-1)*52:40,top:42,bottom:52},
    xAxis:{type:'category',data:categories,axisLabel:{fontSize:9,hideOverlap:true,rotate:categories.length>18?30:0},axisLine:{lineStyle:{color:'#e3e9f2'}}},
    yAxis:ax.map(axisOpt), series,
  },{notMerge:true});
  return ins;
}
/* 两融图 1:1 复刻生产 trade_margin_split：2 grid / 4 Y轴 / 2 X轴 / 2 dataZoom / 联动 */
function renderMarginSplitNode(node, chart, months){
  if(!node) return;
  const ins = echarts.getInstanceByDom(node)||echarts.init(node);
  const categories = filteredData(chart, months);
  const upper = chart.series.filter(s=>s.y_axis_index!==1 && s.y_axis_index!==3);
  const lower = chart.series.filter(s=>s.y_axis_index===1);
  const lowerRight = chart.series.filter(s=>s.y_axis_index===3);
  const dataOf = s=>{ const m=new Map(s.data||[]); return categories.map(c=>m.has(c)?m.get(c):null); };
  const line = (s,i,gi,yi)=>({name:s.name,type:'line',xAxisIndex:gi,yAxisIndex:yi,data:dataOf(s),
    smooth:true,showSymbol:false,symbolSize:5,connectNulls:true,
    lineStyle:{width:2,color:CLR[i%CLR.length]},itemStyle:{color:CLR[i%CLR.length]}});
  ins.setOption({animation:false,color:CLR,
    grid:[{left:62,right:158,top:54,height:'27%'},{left:62,right:158,top:'50%',bottom:96}],
    axisPointer:{link:[{xAxisIndex:'all'}]},
    legend:{top:4,type:'scroll',textStyle:{fontSize:10}},
    tooltip:{trigger:'axis',axisPointer:{type:'cross'},confine:true},
    xAxis:[
      {type:'category',gridIndex:0,data:categories,axisLabel:{show:false},axisTick:{show:false},splitLine:{show:true,lineStyle:{color:'#eef1f6',type:'dashed'}}},
      {type:'category',gridIndex:1,data:categories,splitLine:{show:true,lineStyle:{color:'#eef1f6',type:'dashed'}},axisLabel:{fontSize:10,hideOverlap:true,rotate:categories.length>18?30:0,margin:14}},
    ],
    yAxis:[
      {type:'value',name:'两融余额 (亿元)',position:'left',gridIndex:0,nameTextStyle:{fontSize:9},axisLabel:{fontSize:9},splitLine:{show:true,lineStyle:{color:'#e6eaf0'}}},
      {type:'value',name:'投资者数 (人)',position:'right',gridIndex:0,nameTextStyle:{fontSize:9},axisLabel:{fontSize:9},splitLine:{show:false}},
      {type:'value',name:'比例 (%)',position:'left',gridIndex:1,nameTextStyle:{fontSize:9},axisLabel:{fontSize:9},splitLine:{show:true,lineStyle:{color:'#e6eaf0'}}},
      {type:'value',name:'占比 (%)',position:'right',gridIndex:1,nameTextStyle:{fontSize:9},axisLabel:{fontSize:9},splitLine:{show:false}},
    ],
    dataZoom:[{type:'inside',xAxisIndex:[0,1]},{type:'slider',xAxisIndex:[0,1],height:20,bottom:8,brushSelect:false,showDetail:true,handleSize:'110%',dataBackground:{lineStyle:{opacity:.3},areaStyle:{opacity:.1}}}],
    series:[
      ...upper.map((s,i)=>line(s,i,0,s.y_axis_index===2?1:0)),
      ...lower.map((s,i)=>line(s,i+upper.length,1,2)),
      ...lowerRight.map((s,i)=>line(s,i+upper.length+lower.length,1,3)),
    ],
  },{notMerge:true});
  return ins;
}
function renderDash(){
  const grp = curView.replace('dash','');
  if(!DB[grp]) return;
  const view = $('.view.active'); if(!view) return;
  const box = view.querySelector('[data-dash-charts]'); if(!box) return;
  const list = DB[grp].charts;
  const macroSel = view.querySelector('[data-act="dash-macro-range"]');
  const months = grp==='macro'
    ? ({'近一年':12,'近6个月':6,'近3年':36,'近5年':60,'全部':999}[macroSel?.value]||12)
    : readQuickMonths(view, 12);
  box.innerHTML = list.map(c=>`<div class="card"><div class="card-header" style="padding:9px 13px"><h3 style="font-size:12.5px">${esc(c.title)}</h3>${c.note?`<i class="ti ti-info-circle" style="margin-left:auto;color:#9aabc2" title="${esc(c.note)}"></i>`:''}</div><div class="card-body" style="padding:6px"><div class="chart-box ${c.layout==='trade_margin_split'?'tall':''}"></div></div></div>`).join('');
  const meta = view.querySelector('[data-dash-meta]');
  if(meta) meta.textContent = `宏观 8 图 · ETF 4 图 · 交易 2 图 · 已渲染 ${list.length} 图`;
  setTimeout(()=>list.forEach(c=>{
    const node = box.querySelector('.chart-box');
    // 按卡片顺序取对应节点
    const idx = list.indexOf(c);
    const target = box.children[idx]?.querySelector('.chart-box');
    if(!target) return;
    if(c.layout==='trade_margin_split') renderMarginSplitNode(target,c,months); else renderChartNode(target,c,months);
  }),40);
}
function renderOverview(){
  const node = byId('ovAssetChart'); if(!node) return;
  const ins = echarts.getInstanceByDom(node)||echarts.init(node);
  const data=[{name:'宏观观测',v:S.macro_rows},{name:'ETF 日频',v:S.etf_rows},{name:'指数日线',v:S.index_rows},{name:'交易观测',v:S.trade_rows}];
  ins.setOption({
    animation:false, color:CLR,
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'}},
    grid:{left:88,right:46,top:14,bottom:26},
    xAxis:{type:'value',axisLabel:{fontSize:9,color:'#8a97ac'},splitLine:{lineStyle:{color:'#edf1f7'}}},
    yAxis:{type:'category',data:data.map(d=>d.name),axisLabel:{fontSize:10,color:'#51627e'}},
    series:[{
      type:'bar', data:data.map(d=>d.v), barWidth:18,
      itemStyle:{color:CLR[0],borderRadius:[0,5,5,0]},
      label:{show:true,position:'right',fontSize:10,color:'#51627e',formatter:p=>fmt(p.value)},
    }],
  },{notMerge:true});
}
function fillDerived(){
  const box = $('.view.active [data-derived-body]'); if(!box || box.dataset.filled) return;
  const groups=[
    ['A. 市场成交',[['TOTAL_AMOUNT','全A成交额','亿元','沪+深成交额'],['CONCENTRATION5','成交集中度(前5%)','%','个股成交额前5%占比'],['MARGIN_RATIO','平均维持担保比例','%','两融账户直取'],['MARGIN_FINANCING_RATIO','融资保证金比例','%','政策阶梯常量']]],
    ['B. 两融核心',[['MARGIN_BALANCE','两融余额','亿元','融资+融券'],['FINANCING_TO_FLOAT_MV','融资余额占流通市值比','%','融资÷流通市值'],['FINANCING_BUY_PCT','融资买入额占比','%','融资买入÷全A成交'],['DEBT_INVESTORS','有负债投资者数','人','直取']]],
    ['C. 市场宽度',[['UP_COUNT','上涨家数','家','快照涨跌幅>0'],['DOWN_COUNT','下跌家数','家','快照涨跌幅<0']]],
  ];
  box.innerHTML = groups.map(([title,items])=>`<div class="ind-group"><h4>${title}</h4>${items.map(x=>`<div class="ind-item"><span class="code">${x[0]}</span><span class="nm">${x[1]}</span><span class="mini_tag">${x[2]}</span><span class="formula">${x[3]}</span></div>`).join('')}</div>`).join('');
  box.dataset.filled='1';
}
function fillStatic(){
  fillDerived();
}

/* ==========================================================================
 * 通用交互工具
 * ========================================================================== */
function activeView(){ return $('.view.active'); }
function readQuickMonths(view, def=12){
  const b = view?.querySelector('.seg-btn.active[data-months]');
  return b ? Number(b.dataset.months) : def;
}
function applyQuickRange(btn){
  const view = activeView(); if(!view) return;
  const months = Number(btn.dataset.months||12);
  $$('.seg-btn[data-months]',view).forEach(x=>x.classList.toggle('active',x===btn));
  const s = view.querySelector('[data-range-start]'), e = view.querySelector('[data-range-end]');
  if(s && e){
    const end = new Date(LAST);
    const start = new Date(LAST);
    if(months>=999) start.setFullYear(start.getFullYear()-20); else start.setMonth(start.getMonth()-months);
    const lo = start.toISOString().slice(0,10);
    s.value = lo; e.value = LAST;
    const hint = view.querySelector('[data-range-hint]');
    if(hint) hint.textContent = `已回显 ${lo} ~ ${LAST}（快捷范围）`;
  } else {
    const hint = view.querySelector('[data-filter-count]');
    if(hint) hint.textContent = `已选择 ${btn.textContent.trim()} · 共 ${fmt(Math.round(2889*months/12))} 条`;
  }
  if(curView.startsWith('dash')) renderDash();
  toast(`已切换范围：${btn.textContent.trim()}`, 'info');
}
function setPagerActive(btn){
  const bar = btn.closest('[data-pager]'); if(!bar) return;
  const act = btn.dataset.act, cur = bar.querySelector('.pg.active');
  let target = cur;
  if(act==='page-go') target = btn;
  else if(act==='page-first') target = bar.querySelector('.pg');
  else if(act==='page-last') target = Array.from(bar.querySelectorAll('.pg')).pop();
  else if(act==='page-prev') target = cur?.previousElementSibling?.classList.contains('pg') ? cur.previousElementSibling : cur;
  else if(act==='page-next') target = cur?.nextElementSibling?.classList.contains('pg') ? cur.nextElementSibling : cur;
  if(target && target!==cur){
    $$('.pg',bar).forEach(x=>x.classList.remove('active')); target.classList.add('active');
    toast(`已切换到第 ${target.textContent.trim()} 页`,'info');
  }
}
function handleTableAction(act, btn){
  const view = activeView(); if(!view) return;
  const row = btn.closest('tr');
  if(act==='row-expand'){
    const next = row?.nextElementSibling;
    if(next && next.classList.contains('log-detail')){ next.remove(); btn.textContent='▸'; return; }
    const div = document.createElement('tr'); div.className='log-detail';
    div.innerHTML = `<td colspan="${row?.children.length||8}"><div class="log-detail" style="margin:0">接口调用明细：M1 同比 / 沪深300ETF / 上证指数 / 黄金ETF · 共 4 条（样例）</div></td>`;
    row?.after(div); btn.textContent='▾'; return;
  }
  if(act==='retry-one'){
    btn.textContent='再次重跑'; btn.disabled=true;
    const cells = row?.querySelectorAll('td');
    if(cells?.length>=6){
      const st = cells[5]; if(st) st.innerHTML='<span class="tag success">已重跑成功</span>';
    }
    toast('已重跑该条失败任务 · 结果：成功','ok'); return;
  }
  if(act==='etf-share-detail'){
    const next = row?.nextElementSibling;
    if(next?.classList.contains('log-detail')){ next.remove(); btn.textContent='展开 ▸'; return; }
    const tr=document.createElement('tr'); tr.className='log-detail';
    tr.innerHTML=`<td colspan="4"><div class="log-detail" style="margin:0">510300 沪深300ETF 1,024.8 亿份 · 510050 上证50ETF 586.2 亿份 · 518880 黄金ETF 78.6 亿份 …</div></td>`;
    row?.after(tr); btn.textContent='收起 ▾'; return;
  }
  if(act==='download'){ toast('已触发文件下载（原型模拟）','ok'); return; }
}

/* ==========================================================================
 * 动作分派
 * ========================================================================== */
function handleAction(act, btn, e){
  const view = activeView();
  switch(act){
    /* 采集控制台 */
    case 'collect-full': simulateCollect('全量采集'); break;
    case 'collect-prec': {
      const row = btn.closest('.ctl-row');
      const sel = row?.querySelector('[data-act="prec-cat"]');
      const start = row?.querySelector('[data-prec-start]')?.value || '';
      const end = row?.querySelector('[data-prec-end]')?.value || '';
      simulateCollect(`精确采集 · ${sel?.selectedOptions[0]?.text||''} · ${start} ~ ${end}`, [
        `[精确采集] 类别=${sel?.value||'macro'}，区间=${start} ~ ${end}`,
        `[采集] 按指定时间区间采集所选口径：${start} 至 ${end}`,
        '[采集] 写入 SQLite 业务表（幂等 upsert）',
        '[完成] 所选口径采集成功',
      ]);
      break;
    }
    case 'date-default': {
      const row = btn.closest('.ctl-row');
      const dates = row?.querySelectorAll('input[type="date"]');
      if(dates?.length===2){ dates[0].value=LAST; dates[1].value=LAST; }
      toast('日期已恢复为默认交易日','info'); break;
    }
    case 'import': {
      const f = view?.querySelector('[data-import-file]');
      const name = f?.files?.[0]?.name;
      toast(name?`已导入 ${name}（原型模拟）`:'已导入份额与规模文件（原型模拟）','ok'); break;
    }
    case 'retry-all': modal('批量重跑失败','将重跑最近一次失败批次的全部接口。此操作不可撤销。',()=>{ simulateCollect('批量重跑失败',['[重跑] 已定位最近失败批次','[重跑] 全部失败接口重新调用','[重跑] 失败项重试成功','[完成] 批量重跑结束，监控已更新']); },'确认重跑'); break;
    case 'log-bottom': { const w=byId('logBodyWrap'); if(w) w.scrollTop=w.scrollHeight; break; }
    case 'log-toggle': {
      const p = byId('logBodyWrap')?.closest('.log-panel');
      if(p){ p.classList.toggle('collapsed'); btn.textContent = p.classList.contains('collapsed')?'收起':'展开'; }
      break;
    }
    /* 配置 */
    case 'save-config': case 'save-sched': toast('配置已保存（原型模拟）','ok'); break;
    case 'sched-run-now': simulateCollect('定时调度立即触发',[
      '[调度] 手动触发一次，生成 run_id',
      '[宏观] 采集当月指标（start=end=当月）',
      '[ETF] 采集 26 只标的日频数据',
      '[派生] 增量重算受影响指标/月份',
      '[完成] 调度任务结束',
    ]); break;
    case 'idx-add': case 'etf-add': {
      const wrap = view?.querySelector(`[data-chips="${act==='idx-add'?'idx':'etf'}"]`);
      const inp = view?.querySelector(act==='idx-add'?'[data-idx-new]':'[data-etf-new]');
      const txt = inp?.tagName==='SELECT' ? inp.selectedOptions[0]?.text : (inp?.value||'').trim();
      if(wrap && txt){
        const chip=document.createElement('span'); chip.className='chip';
        chip.innerHTML=`<i class="ti ti-${act==='idx-add'?'chart-dots':'coins'}"></i> ${esc(txt)}<span class="x" data-act="chip-remove">×</span>`;
        wrap.appendChild(chip); toast(`已添加标的：${txt}`,'ok');
      } else toast('请先选择或输入代码','warn');
      break;
    }
    case 'chip-remove': btn.closest('.chip')?.remove(); toast('已移除标的','info'); break;
    case 'macro-add': {
      const g = s => view?.querySelector(`[data-macro-${s}]`)?.value?.trim() || '';
      const code = g('code'), name = g('name');
      if(!code || !name){ toast('请填写指标代码与名称','warn'); break; }
      const body = view?.querySelector('[data-macro-table] tbody');
      if(body){
        const tr = document.createElement('tr');
        tr.innerHTML = `<td><label class="switch-row"><input type="checkbox" checked></label></td>`
          + `<td><div class="cell-clip">${esc(code)}</div></td><td><div class="cell-clip">${esc(name)}</div></td>`
          + `<td><div class="cell-clip">${esc(g('cat')||'自定义')}</div></td><td><div class="cell-clip">${esc(g('src')||'手动')}</div></td>`
          + `<td><div class="cell-clip">${esc(g('freq')||'月')}</div></td>`
          + `<td><button class="btn btn-sm" data-act="macro-remove">删除</button></td>`;
        body.appendChild(tr);
      }
      ['code','name','cat','src','freq'].forEach(s=>{ const el=view?.querySelector(`[data-macro-${s}]`); if(el) el.value=''; });
      toast(`已新增指标：${name}`,'ok'); break;
    }
    case 'macro-remove': btn.closest('tr')?.remove(); toast('已删除宏观指标','info'); break;
    case 'idx-row-remove': case 'etf-row-remove': btn.closest('tr')?.remove(); toast('已删除标的','info'); break;
    /* 预览 */
    case 'filter': {
      const c = view?.querySelector('[data-filter-count]');
      const has = $$('input,p',view).some(x=>x.value?.trim());
      if(c) c.textContent = `筛选后 ${fmt(has?128:2889)} 条`;
      toast('筛选条件已应用','ok'); break;
    }
    case 'filter-reset': {
      $$('input[type="date"]',view).forEach(x=>x.value=LAST);
      const c=view?.querySelector('[data-filter-count]'); if(c) c.textContent=`共 ${fmt(2889)} 条`;
      toast('已恢复默认区间','info'); break;
    }
    case 'export': {
      const names={macro:'宏观数据',etf:'ETF 数据',index:'指数数据',full:'全量数据',monitor:'接口监控',derived:'派生指标',etfcalc:'ETF 计算结果'};
      toast(`${names[btn.dataset.k]||'数据'}导出完成 · 已生成 CSV / Excel / ZIP（原型模拟）`,'ok'); break;
    }
    case 'page-go': case 'page-first': case 'page-last': case 'page-prev': case 'page-next': setPagerActive(btn); break;
    /* 清理 */
    case 'clean-cat': {
      $$('[data-clean-cat] .seg-btn',view).forEach(x=>x.classList.toggle('active',x===btn));
      const sel=view?.querySelector('[data-clean-code]');
      if(sel) sel.innerHTML = btn.dataset.cat==='macro'?'<option>全部（该类别整体）</option><option>M1 同比</option><option>M2 同比</option>':'<option>全部（该类别整体）</option><option>510300 沪深300ETF</option><option>000001 上证指数</option>';
      break;
    }
    case 'clean-preview': {
      const r=view?.querySelector('[data-clean-result]');
      if(r){ r.className='clean-result warn'; r.innerHTML=`预览结果：<b>将删除 128 行</b>（类别=宏观指标，口径=全部）。此操作不可撤销。`; }
      toast('已计算影响范围','warn'); break;
    }
    case 'clean-exec': modal('执行数据清理','确认删除 128 行宏观指标数据？此操作不可撤销，且不影响配置、监控与导出记录。',()=>{
      const r=view?.querySelector('[data-clean-result]');
      if(r){ r.className='clean-result ok'; r.innerHTML='清理完成：<b>已删除 128 行</b>（原型模拟）。'; }
      toast('数据清理完成 · 已删除 128 行','ok');
    },'确认清理'); break;
    case 'clean-reset': {
      const r=view?.querySelector('[data-clean-result]');
      if(r){ r.className='clean-result'; r.innerHTML='预览结果：<span class="hint">将删除 0 行（请先预览影响）。派生指标表不在清理范围内。</span>'; }
      $$('input[type="date"]',view).forEach(x=>x.value=''); break;
    }
    /* 监控 */
    case 'monitor-clean-toggle': {
      const box=view?.querySelector('[data-monitor-clean]');
      if(box){ box.style.display = box.style.display==='none'?'block':'none'; toast(box.style.display==='none'?'已关闭清理面板':'已展开清理面板','info'); }
      break;
    }
    case 'mc-preview': {
      const r=view?.querySelector('[data-mc-result]');
      if(r){ r.className='clean-result warn'; r.innerHTML=`预览 ${S.failed_total} 条接口监控记录将被删除（共 ${fmt(S.today_calls)} 条）。`; }
      toast('已计算监控清理影响','warn'); break;
    }
    case 'mc-exec': modal('清理接口监控','确认按当前口径删除 82 条监控记录？',()=>{
      const r=view?.querySelector('[data-mc-result]');
      if(r){ r.className='clean-result ok'; r.innerHTML='已删除 82 条接口监控记录（原型模拟）。'; }
      toast('监控记录清理完成','ok');
    },'确认清理'); break;
    case 'mc-reset': {
      const r=view?.querySelector('[data-mc-result]'); if(r){ r.className='clean-result'; r.textContent='—'; }
      break;
    }
    case 'range': applyQuickRange(btn); break;
    case 'dash-refresh': renderDash(); toast('图表已刷新','ok'); break;
    /* 派生 / 交易计算 */
    case 'recalc-all': case 'recalc-month': case 'recalc-code':
      simulateCollect(act==='recalc-all'?'派生指标全量重算':act==='recalc-month'?'派生指标按月重算':'派生指标按指标重算',
        ['[派生] 读取 macro_observation 全量原始值','[派生] 按 biz_key 幂等 upsert','[完成] 重算结束，看板已联动生效']); break;
    case 'etfcalc-refresh': toast('ETF 计算结果已刷新 · 共 23 条','ok'); break;
    case 'trade-recalc-all': simulateCollect('交易指标全量重算',[
      '[交易] 读取 5 张原始表','[交易] 计算 10 项派生指标','[交易] 写入 trade_observation 宽表','[完成] 全量重算结束，看板已联动']); break;
    case 'trade-recalc-date': {
      const d = view?.querySelector('[data-trade-recalc-date]')?.value||LAST;
      simulateCollect(`交易指标重算 · ${d}`,['[交易] 读取指定交易日原始值','[交易] 计算并覆盖该日指标','[完成] 重算结束']); break;
    }
    /* 表格内操作 */
    case 'row-expand': case 'retry-one': case 'etf-share-detail': case 'download': handleTableAction(act,btn); break;
    /* 弹窗 */
    case 'modal-cancel': closeModal(); break;
    case 'modal-ok': { const cb=byId('modalRoot')._onOk; closeModal(); if(cb) cb(); break; }
    default: toast(`操作已触发：${act}`,'info');
  }
}

/* ==========================================================================
 * 事件绑定（事件委托）
 * ========================================================================== */
function bindEvents(){
  document.addEventListener('click', e=>{
    const mod = e.target.closest('.mod');
    if(mod){ switchMod(mod.dataset.mod); return; }
    const fn = e.target.closest('.fn');
    if(fn){ switchView(fn.dataset.view); return; }
    const act = e.target.closest('[data-act]');
    if(act){ handleAction(act.dataset.act, act, e); return; }
  });
  document.addEventListener('change', e=>{
    const t = e.target;
    if(t.matches('[data-act="prec-cat"]')){
      const sel = t.closest('.ctl-row')?.querySelector('[data-prec-code]');
      const map = {macro:MACRO_INDS.slice(0,4).map(x=>x[1]),etf:['510300 沪深300ETF','510050 上证50ETF','518880 黄金ETF'],index:['000001 上证指数','000300 沪深300'],trade:['exchange 交易所概况','margin 两融账户','snapshot 全A快照']};
      const opts = map[t.value]||[];
      if(sel) sel.innerHTML = opts.map(o=>`<option>${o}</option>`).join('');
      toast(`类别已切换为「${t.selectedOptions[0]?.text}」，口径已联动`,'info');
    }
    if(t.matches('[data-act="dash-macro-range"]')) renderDash();
    if(t.matches('[data-range-start],[data-range-end]')){
      const view=activeView();
      $$('.seg-btn[data-months]',view).forEach(x=>x.classList.remove('active'));
      const hint=view?.querySelector('[data-range-hint]'); if(hint) hint.textContent='已进入自定义日期范围';
      if(curView.startsWith('dash')) renderDash();
    }
    if(t.matches('[data-clean-cat] [data-act="clean-cat"]')) handleAction('clean-cat',t,e);
  });
  document.addEventListener('input', e=>{
    if(e.target.matches('[data-import-file]')) {
      const f=e.target.files?.[0]; if(f) toast(`已选择文件：${f.name}`,'info');
    }
  });
}

/* ==========================================================================
 * 初始化
 * ========================================================================== */
$('#hdDate').textContent = '交易日 ' + LAST;
$('#hdTime').textContent = now();
setInterval(()=>{ $('#hdTime').textContent = now(); },1000);
buildSidebar();
renderViews();
bindEvents();
switchView('ovCollect');


