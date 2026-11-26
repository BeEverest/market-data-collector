/* ==========================================================================
 * 投研大盘数据平台 · 生产版（Tabler 风格 UI + 真实 API）
 * 生产静态资源：Tabler 风格 UI，数据层与动作层均对接后端接口。
 * ========================================================================== */
const CLR = ['#2563eb','#16a36a','#ea8c28','#8056d8','#e0484b','#0d9a9a','#db2777','#64748b'];
const CHART_INK = '#d7e3f4';
const CHART_GRID = '#223852';
function darkenChartOption(opt){
  if(!opt || typeof opt!=='object') return opt;
  const o = {...opt};
  o.backgroundColor = 'transparent';
  o.textStyle = {...(o.textStyle||{}), color: CHART_INK};
  if(o.title) o.title = {...o.title, textStyle:{...(o.title.textStyle||{}), color:'#eaf1fb'}};
  if(o.legend) o.legend = {...o.legend, textStyle:{...(o.legend.textStyle||{}), color:'#a8bad4'}};
  if(o.tooltip) o.tooltip = {...o.tooltip, backgroundColor:'#0d1d33', borderColor:'#2b4467', textStyle:{...(o.tooltip.textStyle||{}), color:'#eaf1fb'}};
  const darkAxis = a => {
    if(!a) return a;
    const x = {...a};
    x.axisLabel = {...(x.axisLabel||{}), color:(x.axisLabel&&x.axisLabel.color)||'#9fb3d1'};
    x.nameTextStyle = {...(x.nameTextStyle||{}), color:(x.nameTextStyle&&x.nameTextStyle.color)||'#9fb3d1'};
    if(x.splitLine) x.splitLine = {...x.splitLine, lineStyle:{...(x.splitLine.lineStyle||{}), color:CHART_GRID}};
    return x;
  };
  ['xAxis','yAxis'].forEach(k=>{
    if(Array.isArray(o[k])) o[k] = o[k].map(darkAxis);
    else if(o[k]) o[k] = darkAxis(o[k]);
  });
  if(o.dataZoom) o.dataZoom = (Array.isArray(o.dataZoom)?o.dataZoom:[o.dataZoom]).map(z=>({
    ...z, backgroundColor:z.backgroundColor||'#10243d', fillerColor:z.fillerColor||'rgba(91,155,255,.18)',
    borderColor:z.borderColor||'#2b4467', textStyle:{...(z.textStyle||{}), color:(z.textStyle&&z.textStyle.color)||'#9fb3d1'}
  }));
  return o;
}
function initChart(node){
  const ins = echarts.getInstanceByDom(node) || echarts.init(node);
  if(!ins.__darkReady){
    const rawSet = ins.setOption.bind(ins);
    ins.setOption = (opt, ...rest) => rawSet(darkenChartOption(opt), ...rest);
    ins.__darkReady = true;
  }
  return ins;
}
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
const byId = id => document.getElementById(id);
const fmt = n => (n == null ? 0 : n).toLocaleString('zh-CN');
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const now = () => new Date().toLocaleTimeString('zh-CN', {hour12:false});
const pad = n => String(n).padStart(2, '0');
const runId = () => `20261126_${now().replace(/:/g,'')}_manual`;

/* ---------------- API 层 ---------------- */
async function api(url, opts={}) {
  const r = await fetch(url, opts);
  let body;
  try { body = await r.json(); } catch { body = {}; }
  if (!r.ok) throw new Error(body.detail || body.error || `HTTP ${r.status}`);
  return body;
}
/* 带超时与重试的请求：看板等并发加载场景，个别请求可能在连接复用时被丢弃/挂起，
   这里用 AbortController 限时，失败后重试，保证首屏明细一定能加载出来。 */
async function apiRetry(url, opts={}, attempts=3, timeoutMs=6000){
  let lastErr;
  for(let i=0;i<attempts;i++){
    let ctrl, tid;
    try{
      ctrl = new AbortController();
      tid = setTimeout(()=>ctrl.abort(), timeoutMs);
      const r = await fetch(url, {...opts, cache:'no-store', signal:ctrl.signal});
      clearTimeout(tid);
      let body; try{ body = await r.json(); }catch{ body={}; }
      if(!r.ok) throw new Error(body.detail||body.error||`HTTP ${r.status}`);
      return body;
    }catch(e){
      if(tid) clearTimeout(tid);
      lastErr = e;
      if(i<attempts-1) await new Promise(res=>setTimeout(res, 250*(i+1)));
    }
  }
  throw lastErr;
}
const postJSON = (url, payload) => api(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload||{})});

/* 全局数据：从真实 API 加载后填充（视图函数读取这些变量） */
let S = { last_run:{}, last_counts:{}, failed_total:0, today_calls:0, macro_rows:0, macro_indicators:0,
  etf_rows:0, etf_codes:0, index_rows:0, index_codes:0, trade_rows:0, trade_raw_rows:{},
  today_new:{}, scheduler:{enabled:true,time:'03:17',run_type:'full',macro_lookback_months:2,next_run:'-'},
  running:false, server_time:'', last_trade_date:'' };
let DB = { macro:{charts:[]}, etf:{charts:[]}, trade:{charts:[]} };
let LAST = '', WEEK_START = '';
function shiftDate(iso, days){
  if(!iso) return '';
  const d = new Date(`${String(iso).slice(0,10)}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + Number(days||0));
  return d.toISOString().slice(0,10);
}
function shiftMonth(iso, months){
  if(!iso) return '';
  const d = new Date(`${String(iso).slice(0,10)}T00:00:00Z`);
  d.setUTCMonth(d.getUTCMonth() + Number(months||0));
  return d.toISOString().slice(0,10);
}
const CURRENT_MONTH = new Date().toISOString().slice(0,7);
/* 定时调度宏观滚动窗口：当前月往前 lookback 个月（默认 2），返回 YYYY-MM 起止 */
function macroSchedWindow(){
  const lb = Number(S.scheduler?.macro_lookback_months ?? 2);
  const end = CURRENT_MONTH;
  const [y,mo] = end.split('-').map(Number);
  const total = y*12 + (mo-1) - Math.max(0,Math.min(lb,12));
  const start = `${Math.floor(total/12).toString().padStart(4,'0')}-${(total%12+1).toString().padStart(2,'0')}`;
  return {start, end};
}
let MACRO_INDS = [], ETF_ITEMS = [], INDEX_ITEMS = [], TRADE_LATEST = [];
let MONITOR_DATA = {overview:[],failures:[],health:[],detail:[],stats:[]};
let MONITOR_RAW = {overview:[],failures:[],health:[],detail:[],stats:[]};
const MONITOR_STATE = Object.fromEntries(['overview','failures','health','detail','stats'].map(k=>
  [k,{page:1,size:25,keyword:'',run_type:'',category:'',status:'',start:'',end:''}]));
const PREVIEW_STATE = Object.fromEntries(['macro','etf','index','trade','files'].map(k=>
  [k,{page:1,size:25,pages:1,total:0,code:'',start:'',end:'',order:'desc'}]));
let DATA_TABS = {macro:[['日期','指标代码','指标名称','数值','单位','数据源','采集时间'],[]],
  etf:[['交易日期','基金代码','基金简称','份额(亿份)','单位净值(元)','净流入(亿元)','采集时间'],[]],
  index:[['交易日期','指数代码','指数名称','开盘价','最高价','最低价','收盘价','涨跌幅(%)','采集时间'],[]],
  trade:[[],[]],
  files:[['文件名','大小(KB)','修改时间','操作'],[]]};
let CATALOG = {macro:[],etf:[],index:[]};
let ETF_CFG = {codes:[],items:[]}, INDEX_CFG = {codes:[],items:[],start_date:'',end_date:'',lookback_days:1};
let MACRO_CFG = {start:'',end:''};
let DERIVED_ROWS = [], DERIVED_MONTH = CURRENT_MONTH;
let liveLogSeq = 0;
const num = v => (v==null || v==='' ? '—' : (typeof v === 'number' ? (Number.isInteger(v)?fmt(v):v.toLocaleString('zh-CN',{maximumFractionDigits:2})) : v));

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

/* ---------------- 实时监控日志（真实 /api/live-logs + /api/jobs） ---------------- */
function appendLog(text, level='INFO'){
  const body = byId('logBody'); if(!body) return;
  body.insertAdjacentHTML('beforeend', `<div class="log-line"><span class="t">${now()}</span> <span class="${level}">${esc(text)}</span></div>`);
  const strip = byId('logMarquee'); if(strip){ strip.className = level; strip.textContent = text; }
  const wrap = byId('logBodyWrap'); if(wrap && byId('logAutoScroll')?.checked) wrap.scrollTop = wrap.scrollHeight;
}
function setBusy(busy, label){
  const badge = $('#hdStatus'); if(!badge) return;
  badge.textContent = busy ? '采集中…' : '系统正常';
  badge.parentElement.classList.toggle('busy', !!busy);
}
/* 轮询 live-logs 增量写入日志面板 */
async function pollLiveOnce(){
  try {
    const res = await api('/api/live-logs?after=' + liveLogSeq);
    const lines = res.logs || [];
    lines.forEach(l=>{
      liveLogSeq = l.seq || liveLogSeq;
      const lv = String(l.level||'INFO').toUpperCase();
      appendLog(l.text || l.message || '', ['INFO','OK','WARN','ERROR'].includes(lv)?lv:'INFO');
    });
  } catch {}
}
/* 提交任务并轮询 job 直到完成 */
async function submitJob(url, payload, label){
  setBusy(true, label);
  appendLog(`[${label}] 任务已提交`, 'INFO');
  try {
    const res = await postJSON(url, payload);
    const jobId = res.job_id;
    appendLog(`[${label}] 已生成 job_id=${jobId||'—'}`, 'INFO');
    await pollLiveOnce();
    if(!jobId){ setBusy(false); toast(`${label}已提交（无任务句柄）`,'ok'); loadStatus(); return; }
    const timer = setInterval(async ()=>{
      try {
        const job = await api('/api/jobs/' + encodeURIComponent(jobId));
        await pollLiveOnce();
        const st = String(job.status||'');
        if(['done','success','finished','failed','error','completed'].includes(st)){
          clearInterval(timer); setBusy(false);
          const ok = !/fail|error/i.test(st);
          appendLog(`[${label}] 任务${ok?'完成':'失败'}（status=${st}）`, ok?'OK':'ERROR');
          toast(`${label}${ok?'完成':'失败'}`, ok?'ok':'err');
          await Promise.allSettled([loadStatus(), loadMonitor(), loadDashboard()]);
        }
      } catch(e){ clearInterval(timer); setBusy(false); toast(`${label}查询失败：${e.message}`,'err'); }
    }, 1200);
  } catch(e){ setBusy(false); toast(`${label}提交失败：${e.message}`,'err'); appendLog(`[${label}] 提交失败：${e.message}`,'ERROR'); }
}
/* ==========================================================================
 * 真实数据加载层：把后端 API 响应映射为视图函数所需的变量
 * ========================================================================== */
async function loadStatus(){
  try {
    S = await api('/api/status');
    LAST = S.last_trade_date || '';
    WEEK_START = shiftDate(LAST, -6);
    // 运维监控日期控件默认最近一周（按最近交易日回推 6 天）；仅初始化，用户改动后不覆盖
    Object.values(MONITOR_STATE).forEach(st=>{
      if(!st.start && !st.end && WEEK_START){ st.start = WEEK_START; st.end = LAST; }
    });
    const scheduler = S.scheduler || {};
    if($('#dpSchedEnabled')) $('#dpSchedEnabled').checked = !!scheduler.enabled;
    if($('#dpSchedTime') && scheduler.time) $('#dpSchedTime').value = scheduler.time;
    if($('#dpSchedRunType') && scheduler.run_type) $('#dpSchedRunType').value = scheduler.run_type;
    if($('#dpSchedLookback') && scheduler.macro_lookback_months != null) $('#dpSchedLookback').value = scheduler.macro_lookback_months;
    if($('#dpSchedNext')) $('#dpSchedNext').textContent = scheduler.next_run || '-';
    const hdStatus = $('#hdStatus');
    if(hdStatus) hdStatus.textContent = S.running ? '采集中…' : '系统正常';
  } catch(e){ console.warn('loadStatus', e); }
}
async function loadCatalog(){
  try {
    CATALOG = await api('/api/catalog');
    // 宏观指标：code / name / category(推断) / source / granularity
    MACRO_INDS = (CATALOG.macro||[]).map(x=>[
      x.code, x.name, x.category || x.granularity || '', x.source || '', (x.granularity||'').replace('月度','月').replace('季度','季')
    ]);
  } catch(e){ console.warn('loadCatalog', e); }
}
async function loadConfigs(){
  try {
    MACRO_CFG = await api('/api/macro/config');
  } catch(e){ console.warn('macro cfg', e); }
  try {
    ETF_CFG = await api('/api/etf/config');
    ETF_ITEMS = (ETF_CFG.items||[]).map(x=>[x.code, x.name, (x.tags||[]).join('/')||'—', x.listing_date||'—', '—']);
  } catch(e){ console.warn('etf cfg', e); }
  try {
    INDEX_CFG = await api('/api/index/config');
    INDEX_ITEMS = (INDEX_CFG.items||[]).map(x=>[x.code, x.name, '—','—','—']);
  } catch(e){ console.warn('index cfg', e); }
}
async function loadTradeLatest(){
  try {
    const res = await api('/api/trade/latest');
    const L = res.latest || {};
    const val = code => {
      const it = (res.indicators||[]).find(x=>x.code===code);
      const v = L[code.toLowerCase()];
      return v==null ? '—' : (typeof v==='number' ? v.toLocaleString('zh-CN',{maximumFractionDigits:2}) : v);
    };
    TRADE_LATEST = [
      ['TOTAL_AMOUNT','全A成交额', val('TOTAL_AMOUNT'),'亿元','交易所'],
      ['CONCENTRATION5','成交集中度(前5%)', val('CONCENTRATION5'),'%','乐咕/插值'],
      ['MARGIN_BALANCE','两融余额', val('MARGIN_BALANCE'),'亿元','交易所'],
      ['MARGIN_FINANCING_RATIO','融资保证金比例', val('MARGIN_FINANCING_RATIO'),'%','政策阶梯'],
      ['FINANCING_TO_FLOAT_MV','融资余额占流通市值比', val('FINANCING_TO_FLOAT_MV'),'%','计算'],
      ['FINANCING_BUY_PCT','融资买入额占比', val('FINANCING_BUY_PCT'),'%','计算'],
      ['AVG_MAINTENANCE_RATIO','平均维持担保比例', val('margin_ratio'),'%','交易所'],
      ['DEBT_INVESTORS','有负债投资者数', val('debt_investors'),'人','交易所'],
      ['UP_COUNT','上涨家数', val('up_count'),'家','快照'],
      ['DOWN_COUNT','下跌家数', val('down_count'),'家','快照'],
    ];
  } catch(e){ console.warn('trade latest', e); }
}
async function loadMonitor(){
  try {
    const rep = await api('/api/monitor/report');
    const map = {overview:'运行总览', failures:'失败明细', health:'接口健康度', detail:'调用明细', stats:'采集量统计'};
    Object.entries(map).forEach(([k,cn])=>{
      const rows = (rep[cn]||[]).filter(r=>!r['提示']);
      MONITOR_RAW[k] = rows;
      MONITOR_DATA[k] = rows.map(r=>Object.values(r).map(v=>v==null?'—':v));
    });
    const box = $('[data-recent-runs]'); if(box) box.innerHTML = recentRunsTable();
  } catch(e){ console.warn('loadMonitor', e); }
}
/* 最近运行表：读取真实监控 overview */
function recentRunsTable(){
  const rows = (MONITOR_DATA.overview||[]).slice(0,5).map(r=>{
    // 字段顺序：运行ID,运行日期,运行时间,运行类型,接口调用数,成功数,失败数,成功率,总耗时
    const rid=r[0]||'', rt=r[2]||'', type=r[3]||'', fail=Number(r[6]||0);
    const tmap={scheduled:'调度',interface:'接口',full:'全量'};
    const cost=r[8]||'—';
    const status=fail>0?'失败':'成功';
    return [rid.length>18?'…'+rid.slice(-18):rid, rt.slice(11)||rt, tmap[type]||type, `<span class="tag ${fail>0?'failed':'success'}">${status}</span>`, String(cost).replace('秒','s')];
  });
  return rows.length ? tbl(['运行ID','时间','类型','状态','耗时'], rows)
    : '<p class="hint">暂无运行记录（监控库为空）</p>';
}
async function loadDerived(month){
  try {
    month = month || '';
    const res = await api('/api/derived' + (month ? '?month='+encodeURIComponent(month) : ''));
    DERIVED_MONTH = res.month || '';
    DERIVED_ROWS = res.rows || [];
    const box = $('.view.active [data-derived-body]');
    if(box){
      const groups = {};
      DERIVED_ROWS.forEach(r=>{ (groups[r.grp||'其他'] ||= []).push(r); });
      box.innerHTML = Object.entries(groups).map(([title,items])=>`<div class="ind-group"><h4>${esc(title)}</h4><div class="indicator-card-grid">${items.map(x=>`<div class="indicator-card">
        <div class="ic-top"><span class="code">${esc(x.code)}</span><span class="mini_tag">${esc(x.unit||'')}</span></div>
        <div class="ic-name">${esc(x.name)}</div>
        <div class="ic-value">${esc(x.value==null?'—':x.value)} <small>${esc(x.unit||'')}</small></div>
        <div class="ic-formula" title="${esc(x.formula||'')}">${esc(x.formula||'口径未提供')}</div>
        <div class="ic-source">${esc(x.source||'')} · ${esc(x.date||month)}</div>
      </div>`).join('')}</div></div>`).join('') || '<p class="hint">该月暂无宏观指标数据</p>';
      box.dataset.filled='1';
    }
    const formulaBox = $('.view.active [data-derived-formulas]');
    if(formulaBox){
      const seen = new Map();
      DERIVED_ROWS.forEach(r=>{ if(r.code && !seen.has(r.code)) seen.set(r.code, r); });
      formulaBox.innerHTML = seen.size
        ? `<div class="formula-grid">${Array.from(seen.values()).map(x=>`<div class="formula-item"><b>${esc(x.name)}</b><span>${esc(x.formula||'口径未提供')}</span></div>`).join('')}</div>`
        : '<p class="hint">当前月份暂无口径说明，可切换月份查看。</p>';
    }
  } catch(e){ console.warn('loadDerived', e); }
}
const ETF_CALC_TABLES = {
  daily:{rows:[], page:1, size:25, pages:1, total:0, rangeParams:{}},
  shares:{rows:[], page:1, size:25, pages:1, total:0, rangeParams:{}},
};
function etfCalcParams(view){
  const months = readQuickMonths(view, 12);
  const range = chartRange(view);
  return {months:String(months), start:range.start, end:range.end};
}
function renderEtfCalcTable(kind){
  const st = ETF_CALC_TABLES[kind];
  const box = $(kind==='daily' ? '[data-etfcalc-body]' : '[data-etfcalc-shares-body]');
  const pagerBox = $(`[data-etfcalc-pager="${kind}"]`);
  if(!st || !box) return;
  st.total = st.rows.length;
  st.pages = Math.max(1, Math.ceil(st.total / st.size));
  st.page = Math.min(Math.max(1, Number(st.page)||1), st.pages);
  const slice = st.rows.slice((st.page-1)*st.size, st.page*st.size);
  const cfg = kind==='daily'
    ? {cols:['交易日期','汇金份额变化(亿份)','规模变化(亿元)','申赎净流入(亿元)','ETF数'],
       row:r=>`<tr><td>${esc(r.date)}</td><td class="num">${signed(r.share??0,'')}</td><td class="num">${signed(r.cap??0,'')}</td><td class="num">${r.net==null?'<span class="muted-inline">折算日</span>':signed(r.net,'')}</td><td class="num">${r.count}</td></tr>`}
    : {cols:['交易日期','汇金总份额(亿份)','较上日变化(亿份)','累计份额变化(亿份)','ETF数','当日明细'],
       row:r=>`<tr class="etf-shares-row" data-date="${esc(r.date)}"><td>${esc(r.date)}</td><td class="num"><b>${fmtNum(r.shares)}</b></td><td class="num"><span class="${(r.change||0)>=0?'up':'down'}">${r.change==null?'—':((r.change>=0?'+':'')+fmtNum(r.change))}</span></td><td class="num"><span class="${r._cum>=0?'up':'down'}">${r._cum>=0?'+':''}${fmtNum(r._cum)}</span></td><td class="num">${r.count}</td><td><button type="button" class="btn btn-sm etf-shares-toggle" data-date="${esc(r.date)}">展开</button></td></tr><tr class="etf-shares-detail-row" data-detail-date="${esc(r.date)}" hidden><td colspan="6"></td></tr>`};
  box.innerHTML = slice.length
    ? `<div class="table-wrap"><table><thead><tr>${cfg.cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${slice.map(cfg.row).join('')}</tbody></table></div>`
    : '<p class="hint">当前范围暂无数据</p>';
  if(pagerBox) pagerBox.innerHTML = pagerBar(st.total, st.page, st.pages, 'calc:'+kind, st.size);
  if(kind==='shares'){
    box.querySelectorAll('.etf-shares-toggle').forEach(b=>b.addEventListener('click',()=>etfToggleSharesDayDetail(b.dataset.date, box, st.rangeParams)));
  }
}
let ETF_CALC_SEQ = 0;
async function loadEtfCalc(){
  const seq = ++ETF_CALC_SEQ;
  const view = activeView();
  if(!view || curView!=='etfcalc') return;
  const body = view.querySelector('[data-etfcalc-body]');
  if(!body) return;
  const p = etfCalcParams(view);
  const rangeParams = {months:p.months};
  if(p.start) rangeParams.start = p.start;
  if(p.end) rangeParams.end = p.end;
  body.innerHTML = '<div class="hint">加载中…</div>';
  try{
    const qs = new URLSearchParams(rangeParams);
    const res = await apiRetry('/api/dashboard/etf-daily?'+qs.toString());
    if(seq!==ETF_CALC_SEQ || curView!=='etfcalc') return;
    const box = activeView()?.querySelector('[data-etfcalc-body]') || body;
    const count = activeView()?.querySelector('[data-etfcalc-count]');
    const summary = activeView()?.querySelector('[data-etfcalc-summary]');
    const rows = res.rows||[], t = res.total||{};
    if(count) count.textContent = `共 ${fmtNum(rows.length)} 个交易日 · ${res.range||''}`;
    if(summary) summary.innerHTML = `汇金合计净流入 ${signed(t.net??0,'亿元')} · 份额变化 ${signed(t.share??0,'亿份')} · 规模变化 ${signed(t.cap??0,'亿元')}`;
    Object.assign(ETF_CALC_TABLES.daily,{rows,page:1,total:rows.length,rangeParams:p});
    renderEtfCalcTable('daily');
  }catch(e){
    const box = activeView()?.querySelector('[data-etfcalc-body]') || body;
    box.innerHTML = `<div class="hint">加载失败：${esc(e.message||e)}</div>`;
  }
  try{
    const totalQs = new URLSearchParams({...rangeParams, view:'total'});
    const res2 = await apiRetry('/api/dashboard/etf-daily?'+totalQs.toString());
    if(seq!==ETF_CALC_SEQ || curView!=='etfcalc') return;
    const sumBox = activeView()?.querySelector('[data-etfcalc-shares-summary]');
    const tableBox = activeView()?.querySelector('[data-etfcalc-shares-body]');
    if(sumBox && tableBox) etfRenderSharesBlock(sumBox, tableBox, res2, rangeParams);
  }catch(e){
    const tableBox = activeView()?.querySelector('[data-etfcalc-shares-body]');
    if(tableBox) tableBox.innerHTML = `<div class="hint">总份额明细加载失败：${esc(e.message||e)}</div>`;
  }
}
async function loadPreview(kind, opts={}){
  try {
    if(kind==='files'){
      const res = await api('/api/files');
      const fileRows = (res.files||[]).map(f=>[f.name, f.size, f.mtime, `<a class="link" href="/api/download?name=${encodeURIComponent(f.name)}">下载</a>`]);
      DATA_TABS.files = [['文件名','大小(KB)','修改时间','操作'], fileRows];
      Object.assign(PREVIEW_STATE.files,{page:1,pages:1,total:fileRows.length,page_size:fileRows.length||25});
      return;
    }
    const params = new URLSearchParams({page:String(opts.page||1), page_size:String(opts.page_size||25)});
    if(opts.code) params.set('code', opts.code);
    if(opts.start) params.set('start', opts.start);
    if(opts.end) params.set('end', opts.end);
    if(opts.order) params.set('order', opts.order);
    const res = await api(`/api/data/${kind}?` + params.toString());
    if(PREVIEW_STATE[kind]) Object.assign(PREVIEW_STATE[kind],{
      page:res.page||1, pages:res.pages||1, total:res.total||0, page_size:res.page_size||PREVIEW_STATE[kind].size,
      code:opts.code||'', start:opts.start||'', end:opts.end||'', order:opts.order||PREVIEW_STATE[kind].order,
    });
    const colsMap = {
      macro:['日期','指标代码','指标名称','数值','单位','数据源','采集时间'],
      etf:['交易日期','基金代码','基金简称','份额(亿份)','单位净值(元)','净流入(亿元)','采集时间'],
      index:['交易日期','指数代码','指数名称','开盘价','最高价','最低价','收盘价','涨跌幅(%)','采集时间'],
    };
    // 日交易 5 张原始表列集合不同：按接口实际返回字段动态渲染，不能沿用 exchange 固定列。
    // 业务键是内部主键，不进入预览；首列保持交易日期以兼容日期排序入口。
    const tradeColsFallback = {
      exchange:['交易日期','沪市成交额(万亿)','沪市成交量(手)','沪市流通市值(万亿)','沪市总市值(万亿)','深市成交额(万亿)','深市流通市值(万亿)','深市总市值(万亿)','数据来源','采集时间'],
      margin:['交易日期','融资余额(万亿)','融券余额(万亿)','融资买入额(万亿)','融券卖出额(万亿)','平均维持担保比例(%)','有负债投资者数(人)','担保物总价值(万亿)','参与交易投资者数(人)','个人投资者数(人)','机构投资者数(人)','证券公司数(家)','营业部数(家)','数据来源','采集时间'],
      snapshot:['交易日期','代码','名称','涨跌幅(%)','成交量(手)','成交额(百万)','最新价','数据来源','采集时间'],
      breadth:['交易日期','上涨家数','下跌家数','平盘家数','数据来源','采集时间'],
      congestion:['交易日期','收盘价','前5%成交额(万亿)','全A成交额(万亿)','成交集中度前5%(%)','数据来源','采集时间'],
    };
    let cols;
    if(kind==='trade'){
      const table = String(opts.code||'exchange').toLowerCase();
      const actual = Object.keys(res.rows?.[0]||{}).filter(k=>k!=='业务键');
      cols = actual.length ? actual : (tradeColsFallback[table]||tradeColsFallback.exchange);
    } else {
      cols = colsMap[kind] || Object.keys(res.rows?.[0]||{});
    }
    const cellFmt = v => (typeof v==='number' ? (Number.isInteger(v)?fmt(v):v.toLocaleString('zh-CN',{maximumFractionDigits:2})) : (v==null||v===''?'—':v));
    const picked = (res.rows||[]).map(r=>cols.map(k=>cellFmt(r[k])));
    DATA_TABS[kind] = [cols, picked];
    return res;
  } catch(e){ console.warn('loadPreview '+kind, e); }
}
let dashLoadSeq = 0;
async function loadDashboard(){
  const seq = ++dashLoadSeq;
  try {
    const view = activeView();
    const months = readQuickMonths(view, 12);
    const range = chartRange(view);
    const params = new URLSearchParams({months:String(months), etf_months:String(months), trade_months:String(months)});
    // 三组看板统一按当前日期区间取数：避免后端始终只返回默认近一年
    if(range.start){ params.set('macro_start',range.start); params.set('etf_start',range.start); params.set('trade_start',range.start); }
    if(range.end){ params.set('macro_end',range.end); params.set('etf_end',range.end); params.set('trade_end',range.end); }
    const overlay = view?.querySelector('[data-act="overlay-index"]')?.value;
    if(overlay) params.set('overlay_index',overlay);
    const res = await api('/api/dashboard?' + params.toString());
    if(seq !== dashLoadSeq) return;  // 过期请求：视图已切换，丢弃，避免重复重建 DOM
    DB = { macro:res.macro||{charts:[]}, etf:res.etf||{charts:[]}, trade:res.trade||{charts:[]} };
    if(res.index_options && res.index_options.length){
      $$('[data-act="overlay-index"]').forEach(sel=>{
        const cur = sel.value;
        sel.innerHTML = res.index_options.map(o=>`<option value="${o.code}">${esc(o.name)}</option>`).join('');
        if(cur) sel.value = cur;
      });
    }
    renderDash();
  } catch(e){ console.warn('loadDashboard', e); }
}
async function loadAllBase(){
  await Promise.allSettled([loadStatus(), loadCatalog(), loadConfigs(), loadTradeLatest(), loadMonitor()]);
}

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
      {id:'macrocalc',name:'宏观指标',icon:'chart-area'},
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
const statCardWide = (ic,c,val,lbl,trend,tc) => `<div class="col-sm-6 col-lg-4"><div class="stat-card stat-card-wide"><div class="ic ${c}"><i class="ti ti-${ic}"></i></div><div class="val">${val}</div><div class="lbl">${lbl}</div>${trend?`<span class="trend ${tc}">${trend}</span>`:''}</div></div>`;
const statRow = (...c) => `<div class="row row-cards">${c.join('')}</div>`;
const head = (t,d) => ''; // 视图标题条已移除，导航与卡片标题即可标识当前位置
const tbl = (cols, rows, opts={}) => {
  const headCells = [...(opts.expand?['<th></th>']:[]), ...cols.map((c,ci)=>{
    if(opts.sortFirst && ci===0){
      const ord = opts.sortOrder||'desc';
      const icon = ord==='asc'?'↑':'↓';
      const next = ord==='asc'?'降序':'升序';
      return `<th><button class="th-sort" data-act="sort-date" data-kind="${esc(opts.sortFirst)}" title="点击切换日期${next}">${esc(c)} <span class="sort-ico">${icon}</span></button></th>`;
    }
    return `<th>${esc(c)}</th>`;
  }), ...(opts.action?['<th>操作</th>']:[])];
  const h = `<thead><tr>${headCells.join('')}</tr></thead>`;
  const b = rows.map((r,i)=>{
    const lead = opts.expand ? `<td><button class="btn btn-sm" data-act="row-expand" data-i="${i}">▸</button></td>` : '';
    const cells = r.map(v=>`<td><div class="cell-clip">${v}</div></td>`).join('');
    const tail = opts.action ? `<td><button class="btn btn-sm" data-act="${esc(opts.action)}" data-i="${i}">${esc(opts.actionLabel||'操作')}</button></td>` : '';
    return `<tr${opts.fail&&String(r[opts.fail]).includes('失败')?' class="row-fail"':''}>${lead}${cells}${tail}</tr>`;
  }).join('');
  return `<div class="table-responsive"><table class="table table-hover">${h}<tbody>${b}</tbody></table></div>`;
};
const pagerBar = (total=0, page=1, pages=1, scope='', size=25) => {
  const cur = Math.max(1, page), all = Math.max(1, pages);
  const from = Math.max(1, Math.min(cur-2, all-4));
  const nums = Array.from({length:Math.min(5,all)},(_,i)=>from+i);
  return `<div class="pager" data-pager data-scope="${esc(scope)}">共 <b>${fmt(total)}</b> 条 · `+
    `<span class="pgn" data-act="page-first">«</span><span class="pgn" data-act="page-prev">‹</span>`+
    nums.map(n=>`<span class="pg ${n===cur?'active':''}" data-act="page-go" data-p="${n}">${n}</span>`).join('')+
    `<span class="pgn" data-act="page-next">›</span><span class="pgn" data-act="page-last">»</span> 每页 `+
    `<select class="form-select" data-page-size data-scope="${esc(scope)}" style="width:82px;padding:2px 4px">`+
    [25,50,100].map(n=>`<option value="${n}" ${n===size?'selected':''}>${n} 行/页</option>`).join('')+`</select></div>`;
};
const rangeQuick = (prefix, active=12) => `<span class="seg">${[[6,'近半年'],[12,'近1年'],[36,'近3年'],[60,'近5年'],[120,'近10年']].map(([m,n])=>`<button class="seg-btn ${m===active?'active':''}" data-act="range" data-prefix="${prefix}" data-months="${m}">${n}</button>`).join('')}</span>`;
function previewCodeOptions(kind){
  if(kind==='macro') return (CATALOG.macro||[]).map(x=>({v:x.code, l:`${x.name}（${x.code}）`}));
  if(kind==='etf') return (ETF_CFG.items||[]).map(x=>({v:x.code, l:`${x.code} ${x.name||''}`.trim()}));
  if(kind==='index') return (INDEX_CFG.items||[]).map(x=>({v:x.code, l:`${x.code} ${x.name||''}`.trim()}));
  if(kind==='trade') return [['exchange','交易所概况'],['margin','两融账户'],['snapshot','全A个股快照'],['breadth','涨跌家数'],['congestion','成交集中度']].map(([v,l])=>({v,l}));
  return [];
}
/* 精确采集代码下拉：首项“全部”，其后为具体指标/标的（value=代码） */
function precCodeOptionsHtml(kind){
  const opts = previewCodeOptions(kind);
  return `<option value="">全部（该类别全部）</option>` +
    opts.map(o=>`<option value="${esc(o.v)}">${esc(o.l)}</option>`).join('');
}
const filterBar = (kind='macro') => {
  const st = PREVIEW_STATE[kind] || {code:'',start:'',end:'',size:25,total:0};
  const opts = previewCodeOptions(kind);
  const listId = `filterCodeOptions-${esc(kind)}`;
  return `<div class="ctl-params" data-preview-filter data-kind="${esc(kind)}">`+
    `<input class="prec-input" list="${listId}" data-filter-code placeholder="选择或输入代码" style="width:150px" value="${esc(st.code)}">`+
    `<datalist id="${listId}">${opts.map(o=>`<option value="${esc(o.v)}">${esc(o.l)}</option>`).join('')}</datalist>`+
    `<input class="prec-input" type="date" data-filter-start style="width:130px" value="${esc(st.start||LAST)}">`+
    `<span class="hint">~</span>`+
    `<input class="prec-input" type="date" data-filter-end style="width:130px" value="${esc(st.end||LAST)}">`+
    rangeQuick('data',12)+
    `<select class="form-select" data-filter-size style="width:88px">`+
      [25,50,100].map(n=>`<option value="${n}" ${n===st.size?'selected':''}>${n} 行/页</option>`).join('')+`</select>`+
    `<button class="btn btn-primary btn-sm" data-act="filter">筛选</button>`+
    `<button class="btn btn-sm" data-act="filter-reset">默认区间</button>`+
    `<span class="hint" data-filter-count>共 ${fmt(st.total)} 条</span></div>`;
};
/* ==========================================================================
 * 视图定义（覆盖原系统全部功能）
 * ========================================================================== */
function previewView(kind){
  const names = {macro:'宏观数据',etf:'ETF 日频',index:'指数日线',trade:'日交易',files:'输出文件'};
  return head(`采集数据 · ${names[kind]}`,'筛选 / 排序 / 分页 · 数据读取自业务库；导出请到「输出文件」')
    + `<div class="card"><div class="card-header"><h3><i class="ti ti-table" style="color:var(--blue)"></i> ${names[kind]}</h3></div>
    <div class="card-body"><div style="margin-bottom:10px">${filterBar(kind)}</div>
    <div data-preview-table="${kind}"></div><div data-preview-pager="${kind}"></div></div></div>`;
}
function monitorView(kind){
  const names = {overview:'运行总览',failures:'失败明细',health:'接口健康度',detail:'调用明细',stats:'采集量统计'};
  const st = MONITOR_STATE[kind];
  const opts = (arr,val) => arr.map(([v,l])=>`<option value="${esc(v)}" ${v===val?'selected':''}>${l}</option>`).join('');
  const runTypes = [['','全部运行类型'],['full','全量 full'],['scheduled','定时 scheduled'],['interface','精确采集 interface'],['import','文件导入 import'],['retry','重跑 retry']];
  const cats = [['','全部类别'],['宏观','宏观'],['ETF','ETF'],['指数','指数'],['交易','交易']];
  const states = [['','全部状态'],['success','成功'],['failed','失败']];
  return head(`接口监控 · ${names[kind]}`,'成功率 / 失败明细 / 接口健康度 / 调用明细 / 采集量统计')
    + statRow(
      statCard('api','ic-blue',fmt(S.today_calls),'今日调用','全通道','')+
      statCard('circle-check','ic-green',fmt(Math.max(0,S.today_calls)),'今日成功',`成功率 ${S.today_calls?Math.round((S.today_calls-Math.min(S.failed_total,S.today_calls))/S.today_calls*100):100}%`,'t-green')+
      statCard('circle-x','ic-red',fmt(S.failed_total),'累计失败','历史留痕','t-orange')+
      statCard('calendar','ic-purple',LAST,'最新交易日','',''))
    + `<div class="card"><div class="card-header"><h3><i class="ti ti-activity" style="color:var(--blue)"></i> ${names[kind]}</h3>
      ${kind==='failures'?`<button class="btn btn-warn btn-sm" style="margin-left:auto" data-act="retry-all"><i class="ti ti-rotate-clockwise"></i> 批量重跑</button>`:''}
      <button class="btn btn-sm" ${kind!=='failures'?'style="margin-left:auto"':''} data-act="export" data-k="monitor"><i class="ti ti-file-export"></i> 导出监控</button>
      <button class="btn btn-danger btn-sm" data-act="monitor-clean-toggle"><i class="ti ti-eraser"></i> 清理监控</button></div>
    <div class="card-body">
      <div class="card" data-monitor-clean style="display:none;background:#132842;margin-bottom:12px"><div class="card-body">
        <div class="ctl-params" style="margin-bottom:8px"><label class="prec-field">时间范围</label><input class="prec-input" type="date" data-mc-start style="width:130px" value="${esc(WEEK_START)}"><span class="hint">~</span><input class="prec-input" type="date" data-mc-end style="width:130px" value="${esc(LAST)}">
          <select class="form-select" style="width:100px"><option>全部状态</option><option>成功</option><option>失败</option></select>
          <select class="form-select" style="width:110px"><option>全部类型</option><option>全量</option><option>定时</option></select>
          <select class="form-select" style="width:90px"><option>全部类别</option><option>宏观</option><option>ETF</option></select></div>
        <div class="ctl-params" style="margin-bottom:8px"><label class="prec-field">关键词 <span class="muted-inline">接口名 / 接口键 / 失败原因</span></label><input class="prec-input" placeholder="留空不限" style="width:220px"></div>
        <div class="ctl-params"><button class="btn btn-sm" data-act="mc-preview">预览影响</button><button class="btn btn-danger btn-sm" data-act="mc-exec">执行清理</button><button class="btn btn-sm" data-act="mc-reset">重置</button></div>
        <div class="clean-result" data-mc-result>—</div></div></div>
      <div class="ctl-params" style="margin-bottom:10px" data-monitor-filter data-kind="${kind}">
        <input class="prec-input" data-mon-keyword placeholder="接口名/接口键/失败原因 关键词" style="width:200px" value="${esc(st.keyword)}">
        <select class="form-select" data-mon-run-type style="width:135px">${opts(runTypes,st.run_type)}</select>
        <select class="form-select" data-mon-category style="width:105px">${opts(cats,st.category)}</select>
        <select class="form-select" data-mon-status style="width:100px">${opts(states,st.status)}</select>
        <input class="prec-input" type="date" data-mon-start style="width:125px" value="${esc(st.start)}">
        <span class="hint">~</span><input class="prec-input" type="date" data-mon-end style="width:125px" value="${esc(st.end)}">
        <select class="form-select" data-mon-size style="width:88px">${[25,50,100].map(n=>`<option value="${n}" ${n===st.size?'selected':''}>${n} 行/页</option>`).join('')}</select>
        <button class="btn btn-primary btn-sm" data-act="monitor-filter"><i class="ti ti-filter"></i> 筛选</button>
        <button class="btn btn-sm" data-act="monitor-reset">重置</button>
        <span class="hint" data-filter-count>共 ${fmt(MONITOR_RAW[kind].length)} 行</span>${kind==='failures'?'<span class="hint">失败明细每行可单独重跑</span>':''}</div>
      <div data-monitor-table="${kind}"></div><div data-monitor-pager="${kind}"></div></div></div>`;
}
const MONITOR_HEADERS = {
  overview:['运行ID','运行日期','运行时间','运行类型','接口调用数','成功数','失败数','成功率(%)','总耗时(秒)'],
  failures:['任务ID','失败日期','运行ID','类别','接口名称','调用参数','尝试次数','失败原因','重跑结果','失败详情'],
  health:['接口键','接口名称','类别','累计调用','成功数','失败数','成功率(%)','最近失败日期','最近失败原因'],
  detail:['运行日期','运行时间','运行ID','运行类型','类别','接口键','接口名称','调用参数','状态','尝试次数','数据行数','失败原因','耗时(秒)'],
  stats:['运行日期','运行ID','运行类型','类别口径','细分口径','名称','采集行数','统计时间'],
};
function monitorStatus(row, kind){
  if(kind==='failures') return 'failed';
  if(row['状态'] != null && row['状态'] !== '') return String(row['状态']);
  if(row['失败数'] != null) return Number(row['失败数'])>0 ? 'failed' : 'success';
  return '';
}
function monitorFilteredRows(kind){
  const st = MONITOR_STATE[kind];
  const kw = String(st.keyword||'').trim().toLowerCase();
  return (MONITOR_RAW[kind]||[]).filter(r=>{
    if(kw && !Object.values(r).some(v=>String(v ?? '').toLowerCase().includes(kw))) return false;
    if(st.run_type && String(r['运行类型']||'')!==st.run_type) return false;
    if(st.category && String(r['类别']||r['类别口径']||'')!==st.category) return false;
    if(st.status && monitorStatus(r,kind)!==st.status) return false;
    const date = String(r['运行日期']||r['失败日期']||r['最近失败日期']||r['运行时间']||'').slice(0,10);
    if(st.start && date && date<st.start) return false;
    if(st.end && date && date>st.end) return false;
    return true;
  });
}
function renderMonitorTable(kind){
  const box=document.querySelector(`[data-monitor-table="${kind}"]`); if(!box) return;
  const st=MONITOR_STATE[kind], rows=monitorFilteredRows(kind);
  const size=Math.min(Math.max(1,Number(st.size)||25),100);
  const pages=Math.max(1,Math.ceil(rows.length/size));
  if(st.page>pages) st.page=pages;
  const slice=rows.slice((st.page-1)*size, st.page*size);
  const cols=MONITOR_HEADERS[kind]||[];
  const view=box.closest('.view');
  const hint=view?.querySelector('[data-filter-count]');
  const active=['keyword','run_type','category','status','start','end'].some(k=>st[k]);
  if(hint) hint.textContent=`${active?'筛选后':'共'} ${fmt(rows.length)} 行`;
  if(!slice.length){ box.innerHTML='<p class="hint">无匹配记录，请调整筛选条件</p>'; return; }
  const headCells=(kind==='overview'?['']:[]).concat(cols,kind==='failures'?['操作']:[]);
  const body=slice.map(r=>{
    const status=monitorStatus(r,kind);
    const fail=status==='failed' || !!(r['失败原因'] && !String(r['重跑结果']||'').startsWith('已'));
    let cells=cols.map(c=>{
      const v=r[c]==null?'':String(r[c]);
      let inner=v?`<div class="cell-clip" title="${esc(v)}">${esc(v)}</div>`:'<span class="hint">—</span>';
      if(c==='状态') inner=`<span class="tag ${status==='success'?'success':'failed'}">${status==='success'?'成功':'失败'}</span>`;
      if(c==='重跑结果' && v) inner=`<span class="tag ${v.startsWith('已')?'success':'failed'}">${esc(v)}</span>`;
      if(c==='成功率(%)' && v) inner=`<span class="${Number(v)>=100?'up':'down'}">${esc(v)}%</span>`;
      return `<td>${inner}</td>`;
    }).join('');
    if(kind==='overview'){
      const runId=esc(r['运行ID']||'');
      cells=`<td><button class="btn btn-sm" data-act="monitor-expand" data-run-id="${runId}">▸</button></td>`+cells;
      return `<tr class="${fail?'row-fail':''}">${cells}</tr>`;
    }
    if(kind==='failures'){
      const done=String(r['重跑结果']||'').startsWith('已重跑成功');
      cells+=`<td><button class="btn btn-sm" data-act="retry-one" data-task-id="${esc(r['任务ID']||'')}">${done?'再次重跑':'重跑此条'}</button></td>`;
    }
    return `<tr class="${fail?'row-fail':''}">${cells}</tr>`;
  }).join('');
  box.innerHTML=`<div class="table-responsive"><table class="table table-hover"><thead><tr>${headCells.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function renderMonitorPager(kind){
  const box=document.querySelector(`[data-monitor-pager="${kind}"]`); if(!box) return;
  const st=MONITOR_STATE[kind], rows=monitorFilteredRows(kind);
  const size=Math.min(Math.max(1,Number(st.size)||25),100);
  const pages=Math.max(1,Math.ceil(rows.length/size));
  if(st.page>pages) st.page=pages;
  box.innerHTML=pagerBar(rows.length,st.page,pages,'mon:'+kind,size);
}
function monitorGo(kind,page,size){
  const st=MONITOR_STATE[kind]; if(!st) return;
  st.page=Math.max(1,Number(page)||1);
  if(size) st.size=Math.min(Math.max(1,Number(size)||25),100);
  renderMonitorTable(kind); renderMonitorPager(kind);
}
function renderPreviewTable(kind){
  const box=document.querySelector(`[data-preview-table="${kind}"]`); if(!box) return;
  const [cols,rows]=DATA_TABS[kind]||[[],[]];
  const sortOpts = kind==='files' ? {} : {sortFirst:kind, sortOrder:PREVIEW_STATE[kind]?.order||'desc'};
  box.innerHTML=rows.length ? tbl(cols,rows,{...sortOpts,action:kind==='files'?'download':null,actionLabel:'下载'})
    : '<p class="hint">暂无数据，请调整筛选条件</p>';
}
function renderPreviewPager(kind){
  const box=document.querySelector(`[data-preview-pager="${kind}"]`); if(!box) return;
  const st=PREVIEW_STATE[kind]; if(!st) return;
  box.innerHTML=pagerBar(st.total,st.page,st.pages,'prev:'+kind,st.size);
  // 同步当前视图顶部筛选条计数，避免快捷范围/排序后仍显示旧总数
  const filterBar = document.querySelector(`[data-preview-filter][data-kind="${kind}"]`);
  const cnt = filterBar?.querySelector('[data-filter-count]');
  if(cnt) cnt.textContent = `共 ${fmt(st.total)} 条`;
}
function previewGo(kind,page,size){
  const st=PREVIEW_STATE[kind]; if(!st) return;
  st.page=Math.max(1,Number(page)||1);
  if(size){ st.size=Math.min(Math.max(1,Number(size)||25),100); st.page=1; }
  loadPreview(kind,{page:st.page,page_size:st.size,code:st.code,start:st.start,end:st.end,order:st.order})
    .then(()=>{ renderPreviewTable(kind); renderPreviewPager(kind); });
}
function hydrateTables(){
  Object.keys(MONITOR_STATE).forEach(k=>{ renderMonitorTable(k); renderMonitorPager(k); });
  Object.keys(PREVIEW_STATE).forEach(k=>{ renderPreviewTable(k); renderPreviewPager(k); });
}
function dashShell(grp){
  const overlay = grp==='etf'
    ? `<label class="prec-field">叠加指数 <select class="form-select" style="width:135px" data-act="overlay-index" title="汇金图上方叠加的指数日线"><option value="000001">上证指数 000001</option><option value="000300">沪深300 000300</option><option value="000688">科创50 000688</option><option value="399006">创业板指 399006</option></select></label>`
    : '';
  const hintText = grp==='macro' ? '数据来源：宏观指标 / 宏观观测 / 上交所日线' : '';
  const ctl = `${overlay}
       <label class="prec-field">开始 <input class="prec-input" type="date" data-range-start value="" style="width:125px"></label>
       <label class="prec-field">结束 <input class="prec-input" type="date" data-range-end value="${LAST}" style="width:125px"></label>
       ${rangeQuick('dash-'+grp,12)}${hintText?`<span class="hint">${hintText}</span>`:''}`;
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
      <div class="card"><div class="card-header"><h3><i class="ti ti-clock" style="color:var(--ok)"></i> 最近运行</h3></div><div class="card-body" data-recent-runs>${recentRunsTable()}</div></div></div>`,

  /* ---------------- 数据采集：控制台 ---------------- */
  console:()=>head('采集控制台','整体 / 部分采集 · 3 种采样方式 + 实时监控')+statRow(
    statCardWide('database','ic-blue',fmt(S.macro_rows+S.etf_rows+S.index_rows+S.trade_rows),'采集总数',`含 ${fmt(S.trade_rows)} 条交易观测`,'t-blue')+
    statCardWide('bullseye','ic-green',fmt(S.macro_indicators+S.etf_codes+S.index_codes),'采集指标数据量',`${fmt(S.macro_indicators)} 宏观 / ${fmt(S.etf_codes)} ETF / ${fmt(S.index_codes)} 指数`,'t-green')+
    statCardWide('plus','ic-purple',fmt(Object.values(S.today_new||{}).reduce((a,b)=>a+Number(b||0),0)),'当日新增采集数量','按 collection_stat 统计','t-blue'))
    +`<div class="card"><div class="card-header"><h3><i class="ti ti-player-play" style="color:var(--blue)"></i> 采样方式</h3><span class="sub">整体 / 部分 · 参数行内可调 · 提交任务后查看实时日志</span></div>
      <div class="card-body"><div style="display:grid;grid-template-columns:150px 70px 1fr 70px 90px;gap:8px;font-size:10px;color:#8fa6c4;padding:0 0 6px;border-bottom:1px solid #243a59"><span>采样方式</span><span>范围</span><span>参数（行内可调）</span><span>启用</span><span>操作</span></div>
      <div class="ctl-row" style="grid-template-columns:150px 70px 1fr 70px 90px"><div class="ctl-name"><div class="ci" style="background:#3b5bdb"><i class="ti ti-refresh"></i></div><div><b>全量采集</b><small>宏观 + ETF + 指数</small></div></div><span class="scope-pill scope-whole">整体</span>
        <div class="ctl-params"><input class="prec-input" type="date" value="${LAST}" style="width:120px"><span class="hint">~</span><input class="prec-input" type="date" value="${LAST}" style="width:120px"><button class="btn btn-sm" data-act="date-default">默认</button></div>
        <div style="text-align:center"><span class="s-always">手动</span></div><div><button class="btn btn-primary btn-sm" data-act="collect-full">立即采集</button></div></div>
      <div class="ctl-row" style="grid-template-columns:150px 70px 1fr 70px 90px"><div class="ctl-name"><div class="ci" style="background:#16855b"><i class="ti ti-target"></i></div><div><b>精确采集</b><small>类别 → 具体口径 + 时间区间</small></div></div><span class="scope-pill scope-part">部分</span>
        <div class="ctl-params"><select class="form-select" style="width:100px" data-act="prec-cat"><option value="macro">宏观指标</option><option value="etf">ETF 基金</option><option value="index">指数</option><option value="trade">交易（原始）</option></select><span class="hint">→</span><select class="form-select" style="width:150px" data-prec-code>${precCodeOptionsHtml('macro')}</select><input class="prec-input" type="date" value="${LAST}" style="width:118px" data-prec-start><span class="hint">~</span><input class="prec-input" type="date" value="${LAST}" style="width:118px" data-prec-end></div>
        <div style="text-align:center"><span class="s-always">手动</span></div><div><button class="btn btn-primary btn-sm" data-act="collect-prec">采集所选</button></div></div>
      <div class="ctl-row" style="grid-template-columns:150px 70px 1fr 70px 90px"><div class="ctl-name"><div class="ci" style="background:#c2410c"><i class="ti ti-file-import"></i></div><div><b>文件导入</b><small>份额与规模_代码.xlsx</small></div></div><span class="scope-pill scope-ops">ETF数据</span>
        <div class="ctl-params"><input class="prec-input" type="file" style="width:220px;font-size:10px" data-import-file><button class="btn btn-sm" data-act="import">导入数据</button></div>
        <div style="text-align:center"><span class="s-always">手动</span></div><div><span class="hint" style="font-size:10px">≤10 文件</span></div></div>
      <div class="log-panel" style="margin-top:12px;display:block"><div class="log-head"><span>实时监控 · 实时日志</span><span class="log-scope">全部线程 · 最近5000行</span><div class="log-tools"><label><input type="checkbox" id="logAutoScroll" checked> 自动滚动</label><button class="log-tool" data-act="log-bottom" title="回到底部">↓</button><button class="log-tool" data-act="log-toggle">展开</button><span class="log-dot"></span></div></div>
        <div class="log-strip" data-act="log-toggle"><span class="log-strip-label">最新</span><span id="logMarquee" class="INFO">正在加载实时日志…</span></div>
        <div class="log-body-wrap" id="logBodyWrap"><div id="logBody"></div></div></div></div></div>`,

  /* ---------------- 数据采集：配置 ---------------- */
  maccfg:()=>head('宏观采集配置','指标可增删 · 数据源与区间配置')+`<div class="card"><div class="card-header"><h3><i class="ti ti-adjustments" style="color:var(--ok)"></i> 宏观指标配置</h3><span class="sub">17 原指标 + GDP + 海外 · 可增删 / 启停</span></div><div class="card-body">
    <div class="ctl-params" style="margin-bottom:11px;padding:9px 10px;background:#132842;border:1px solid var(--line);border-radius:7px">
      <label class="prec-field"><b>新增指标</b></label>
      <input class="prec-input" placeholder="指标代码" style="width:125px" data-macro-code>
      <input class="prec-input" placeholder="名称" style="width:135px" data-macro-name>
      <input class="prec-input" placeholder="类别" style="width:78px" data-macro-cat>
      <input class="prec-input" placeholder="数据源" style="width:88px" data-macro-src>
      <input class="prec-input" placeholder="频率" style="width:60px" data-macro-freq>
      <button class="btn btn-primary btn-sm" data-act="macro-add"><i class="ti ti-plus"></i> 新增</button></div>
    <div class="ctl-params" style="margin-bottom:12px"><label class="prec-field">默认区间 <input class="prec-input" type="date" value="${esc(MACRO_CFG.start_date||LAST)}" style="width:125px" data-macro-start></label><span class="hint">~</span><input class="prec-input" type="date" value="${esc(MACRO_CFG.end_date||LAST)}" style="width:125px" data-macro-end><button class="btn btn-primary btn-sm" data-act="save-config">保存配置</button><span class="hint" data-config-hint>默认最近一个交易日；留空 = 动态取最近一期</span></div>
    <div class="table-responsive" data-macro-table>${tbl(['启用','指标代码','名称','类别','数据源','频率'],MACRO_INDS.map(r=>[
      `<label class="switch-row"><input type="checkbox" checked data-macro-enable></label>`,...r]),{action:'macro-remove',actionLabel:'删除'})}</div></div></div>`,

  idxcfg:()=>head('指数采集配置','4 个指数 · 日线采集')+`<div class="card"><div class="card-header"><h3><i class="ti ti-chart-dots" style="color:var(--blue)"></i> 指数标的配置</h3><span class="sub">灵活增删 · 自定义区间</span></div><div class="card-body">
    <div class="ctl-params"><select class="form-select" style="width:170px" data-idx-new><option>000001 上证指数</option><option>000300 沪深300</option><option>000688 科创50</option><option>399006 创业板指</option></select><button class="btn btn-sm" data-act="idx-add"><i class="ti ti-plus"></i> 添加</button></div>
    <div class="chips" data-chips="idx">${INDEX_ITEMS.map(x=>`<span class="chip"><i class="ti ti-chart-dots"></i> ${x[0]} ${x[1]}<span class="x" data-act="chip-remove">×</span></span>`).join('')}</div>
    <div class="ctl-params"><label class="prec-field">采集区间 <input class="prec-input" type="date" value="${esc(INDEX_CFG.start_date||LAST)}" style="width:125px" data-idx-start></label><span class="hint">~</span><input class="prec-input" type="date" value="${esc(INDEX_CFG.end_date||LAST)}" style="width:125px" data-idx-end><label class="prec-field">回看 <input class="prec-input" type="number" value="${Number(INDEX_CFG.lookback_days||1)}" style="width:60px" data-idx-lookback></label><button class="btn btn-primary btn-sm" data-act="save-config">保存配置</button><span class="hint">默认最近一个交易日</span></div>
    <div class="table-responsive" style="margin-top:12px">${tbl(['代码','名称'],INDEX_ITEMS.map(x=>[x[0],x[1]]),{action:'idx-row-remove',actionLabel:'删除'})}</div></div></div>`,

  etfcfg:()=>head('ETF 采集配置','26 只标的 · 分组标签与上市日期缓存')+`<div class="card"><div class="card-header"><h3><i class="ti ti-coins" style="color:var(--ok)"></i> ETF 标的配置</h3><span class="sub">宽基 / 汇金 / 黄金 · 自动解析上市日</span></div><div class="card-body">
    <div class="ctl-params"><input class="prec-input" placeholder="输入 6 位基金代码" maxlength="6" style="width:150px" data-etf-new><button class="btn btn-sm" data-act="etf-add"><i class="ti ti-plus"></i> 添加</button></div>
    <div class="chips" data-chips="etf">${ETF_ITEMS.slice(0,6).map(x=>`<span class="chip"><i class="ti ti-coins"></i> ${x[0]} ${x[1]} <span class="mini_tag">${x[2].split('/')[0]}</span><span class="x" data-act="chip-remove">×</span></span>`).join('')}</div>
    <div class="ctl-params"><label class="prec-field">默认区间 <input class="prec-input" type="date" value="${esc(ETF_CFG.start_date||LAST)}" style="width:125px" data-etf-start></label><span class="hint">~</span><input class="prec-input" type="date" value="${esc(ETF_CFG.end_date||LAST)}" style="width:125px" data-etf-end><label class="prec-field">最近 <input class="prec-input" type="number" value="${Number(ETF_CFG.default_trade_days||1)}" style="width:60px" data-etf-days></label><button class="btn btn-primary btn-sm" data-act="save-config">保存配置</button><span class="hint">默认最近一个交易日</span></div>
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
    <div class="clean-result" data-clean-result>预览结果：<span class="hint">将删除 0 行（请先预览影响）。宏观指标计算表不在清理范围内。</span></div></div></div>`,

  /* ---------------- 数据处理 ---------------- */
  macrocalc:()=>head('宏观指标','14 项指标 · 按月切换查看 · 手动重算兜底')+`<div class="trigger-grid" style="grid-template-columns:1fr">
    <div class="trigger-card"><div class="tc-head"><h3>🔁 手动重算 <small>兜底</small></h3><span class="badge bg-blue-lt">已就绪</span></div><div class="tc-body">
      <div class="tc-row"><button class="btn btn-primary btn-sm" data-act="recalc-all">重新计算全部</button><button class="btn btn-sm" data-act="export" data-k="derived">导出 CSV</button></div>
      <div class="tc-row"><label class="prec-field">按月 <input class="prec-input" type="month" value="${esc(DERIVED_MONTH||CURRENT_MONTH)}" style="width:110px" data-derived-recalc-month></label><button class="btn btn-sm" data-act="recalc-month">重算该月</button></div>
      <div class="tc-row"><label class="prec-field">按指标 <select class="form-select" style="width:150px"><option>M1 同比</option><option>社融增量</option><option>CPI-PPI 剪刀差</option></select></label><button class="btn btn-sm" data-act="recalc-code">重算该指标</button></div>
      <div class="tc-note">默认月份为当前月；局部重算按业务键幂等覆盖，全量重算才整表替换清理陈旧口径行</div></div></div></div>
    <div class="card"><div class="card-header"><h3><i class="ti ti-list-check" style="color:var(--blue)"></i> 宏观指标卡片</h3>
      <div class="month-switch" style="margin-left:auto">
        <button class="btn btn-sm" data-act="derived-month-step" data-step="-1" title="上个月"><i class="ti ti-chevron-left"></i></button>
        <input class="prec-input" type="month" value="${esc(DERIVED_MONTH||CURRENT_MONTH)}" style="width:120px;text-align:center" data-derived-view-month>
        <button class="btn btn-sm" data-act="derived-month-step" data-step="1" title="下个月"><i class="ti ti-chevron-right"></i></button>
        <button class="btn btn-sm" data-act="derived-month-now" title="回到当前月">本月</button>
      </div>
    </div><div class="card-body" data-derived-body></div></div>
    <div class="card"><div class="card-header"><h3><i class="ti ti-info-circle" style="color:var(--muted)"></i> 指标口径说明</h3><span class="sub">随当前月份指标自动更新</span></div><div class="card-body" data-derived-formulas><p class="hint">加载中…</p></div></div>`,

  etfcalc:()=>head('ETF 计算','流通份额变化 / 申赎净流入 · 汇金维度')+`<div class="card"><div class="card-header"><h3><i class="ti ti-calculator" style="color:var(--blue)"></i> ETF 计算</h3><span class="sub">数据来自 etf_observation 全量历史 · 日期区间与 ETF 看板一致</span></div><div class="card-body">
    <div class="formula-note"><div><b>流通份额变化</b> = 当日份额 − 上一交易日份额（亿份）</div><div><b>申赎净流入</b> = 份额变化 × 当日单位净值（亿元），剔除价格涨跌的市值波动；净值单日涨跌超 ±20% 视为份额折算，净流入置空</div></div>
    <div class="ctl-params" style="margin-bottom:12px"><label class="prec-field">开始 <input class="prec-input" type="date" style="width:125px" data-range-start></label><span class="hint">~</span><label class="prec-field">结束 <input class="prec-input" type="date" style="width:125px" data-range-end></label>${rangeQuick('etfcalc',12)}<button class="btn btn-sm" data-act="etfcalc-refresh"><i class="ti ti-refresh"></i> 刷新</button><button class="btn btn-sm" data-act="export" data-k="etfcalc"><i class="ti ti-file-export"></i> 导出 CSV</button><span class="hint" data-range-hint></span><span class="hint" data-etfcalc-count>加载中…</span></div>
    <div class="mini_tag" style="display:inline-block;margin-bottom:8px" data-etfcalc-summary>汇金合计净流入统计中…</div>
    <div data-etfcalc-body><div class="hint">加载中…</div></div><div data-etfcalc-pager="daily"></div>
    <div class="card-header" style="margin:16px -14px 10px;padding:10px 14px"><h3>汇金维度 · 每个交易日总份额</h3><span class="sub">按当前日期范围统计 · 表格分页 · 点击展开明细</span></div>
    <div data-etfcalc-shares-summary></div>
    <div data-etfcalc-shares-body><div class="hint">加载中…</div></div><div data-etfcalc-pager="shares"></div></div></div>`,

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

  tasksched:()=>{
    const mw = macroSchedWindow();
    const lb = Number(S.scheduler?.macro_lookback_months ?? 2);
    // 各指标/类别的默认采集时间窗口（定时调度口径）
    const timeRows = [
      ['宏观指标（全部）', `月度 · 次月发布`, `${mw.start} ~ ${mw.end}`, `当前月往前 ${lb} 个月（共 ${lb+1} 期），幂等 upsert；覆盖刚发布的上月数据`],
      ['ETF 基金（全部）', `日频 · 每交易日`, `最近 ${Number(ETF_CFG.default_trade_days||1)} 个交易日`, `按 etf.default_trade_days，默认最近 1 个已结束交易日`],
      ['指数日线（全部）', `日频 · 每交易日`, `最近 1 个交易日`, `两日期留空时取最近已结束交易日`],
      ['交易原始数据（全部）', `日频 · 每交易日`, `最近 1 个交易日`, `交易所 / 两融 / 快照；快照仅当日不可回溯`],
    ];
    (CATALOG.macro||[]).slice(0,12).forEach(x=>timeRows.push([
      `　└ ${esc(x.name)}`, esc(x.code), esc(x.granularity||'月度'), `${mw.start} ~ ${mw.end}`,
    ]));
    return head('任务调度','每日定时调度 · 采集完成后自动增量重算')+`<div class="trigger-grid" style="grid-template-columns:1fr">
    <div class="trigger-card"><div class="tc-head"><h3>⏰ 定时调度 <small>主触发</small></h3><label class="switch-row"><input type="checkbox" id="dpSchedEnabled" checked> 启用</label></div><div class="tc-body">
      <div class="tc-row"><label>触发时刻</label><input class="prec-input" type="time" id="dpSchedTime" value="03:17" style="width:100px"><label>采集范围</label><select class="form-select" id="dpSchedRunType" style="width:160px"><option value="full">全量 full</option><option value="macro">仅宏观 macro（滚动窗口）</option><option value="etf">仅 ETF etf</option><option value="index">仅指数 index</option></select><label>宏观回溯月数</label><input class="prec-input" type="number" id="dpSchedLookback" min="0" max="12" value="${lb}" style="width:64px"><label>下次运行</label><span class="hint" id="dpSchedNext">${S.scheduler.next_run}</span></div>
      <div class="tc-row tc-row-foot"><button class="btn btn-primary btn-sm" data-act="save-sched">保存调度配置</button><button class="btn btn-sm" data-act="sched-run-now"><i class="ti ti-player-play"></i> 立即触发一次</button></div>
      <div class="tc-note">宏观采集窗口：${mw.start} ~ ${mw.end}（当前月 + 前 ${lb} 月）· 采集完成后自动增量重算 · 每天仅触发一次 · 忙时跳过不补跑</div></div></div></div>
    <div class="card"><div class="card-header"><h3><i class="ti ti-info-circle" style="color:var(--muted)"></i> 触发逻辑说明</h3></div><div class="card-body"><ol class="steps">
      <li><b>触发条件</b>：服务运行时按本页配置每日到点触发一次，同一天只触发一次；未启用 / 未到点 / 今日已触发 / 任务占用时不执行，日志中给出原因。</li>
      <li><b>步骤 1~5</b>：触发 → 提交采集任务 → 宏观 / ETF / 指数采集 → 写 SQLite 业务表（幂等 upsert）→ 采集完成统计，全程写入实时监控日志。</li>
      <li><b>步骤 6/6 派生增量重算</b>：按本次 <code>run_id</code> 读取写入 <code>macro_observation</code> 的原始 <code>(指标, 月份)</code>，经依赖映射推导受影响的派生行，<b>只重算这些目标行</b>并按 <code>biz_key</code> 幂等 upsert。</li>
      <li><b>手动重算（兜底）</b>：「全部」全量重算并整表替换；「按月」「按指标」按业务键幂等覆盖只影响目标子集，在「数据处理」模块操作。</li>
      <li><b>宏观滚动窗口</b>：定时调度宏观采集 <code>${mw.start} ~ ${mw.end}</code>（当前月 + 前 ${lb} 月，可通过上方「宏观回溯月数」调整）。宏观月度数据多在次月发布，只采当月会漏掉刚发布的上月数据；窗口内按业务键幂等 upsert，重复采集不产生冗余。</li>
      <li><b>看板联动</b>：数据看板各图直接读取派生表，重算完成后自动生效。</li>
    </ol></div></div>
    <div class="card"><div class="card-header"><h3><i class="ti ti-clock" style="color:var(--blue)"></i> 各指标默认采集时间</h3><span class="sub">定时调度下每类数据的采集口径与窗口</span></div><div class="card-body">${tbl(['数据类别 / 指标','代码 / 频率','默认采集窗口','说明'], timeRows)}</div></div>
    <div class="card"><div class="card-header"><h3><i class="ti ti-history" style="color:var(--blue)"></i> 最近调度记录</h3></div><div class="card-body">${tbl(['触发时间','运行ID','范围','状态','调用数','耗时'],[
      ['2026-11-26 03:17','20261126_031700_scheduled','全量','<span class="tag success">成功</span>','284','186s'],
      ['2026-11-25 03:17','20261125_031700_scheduled','全量','<span class="tag success">成功</span>','284','191s'],
      ['2026-11-24 03:17','20261124_031700_scheduled','全量','<span class="tag warn">跳过</span>','—','今日已触发'],
      ['2026-11-23 03:17','20261123_031700_scheduled','全量','<span class="tag success">成功</span>','282','188s'],
    ])}</div></div>`;
  },
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
  h += `<div class="sidebar-foot">投研大盘数据平台 · 生产系统<br>© 2026 投研数据团队</div>`;
  $('#sidebar').innerHTML = h;
}
function renderViews(){
  $('#views').innerHTML = Object.entries(VIEWS).map(([k,v])=>`<section class="view ${k===curView?'active':''}" id="v-${k}">${v()}</section>`).join('');
  hydrateTables();
}
function switchView(id){
  if(!VIEWS[id]) return;
  curView = id;
  $$('.view').forEach(x=>x.classList.remove('active'));
  const el = byId('v-'+id); if(el) el.classList.add('active');
  $$('.fn').forEach(x=>x.classList.toggle('active',x.dataset.view===id));
  if(id.startsWith('dash')) setTimeout(()=>{ loadDashboard(); }, 30);
  if(id==='ovCollect') setTimeout(renderOverview, 60);
  if(id==='macrocalc') setTimeout(()=>loadDerived(DERIVED_MONTH||CURRENT_MONTH), 30);
  if(id==='etfcalc') setTimeout(()=>loadEtfCalc(), 30);
  if(id.startsWith('prev-')) setTimeout(()=>loadPreview(id.replace('prev-','')).then(()=>rerenderView(id)), 30);
  if(id.startsWith('mon-')) setTimeout(()=>loadMonitor().then(()=>rerenderView(id)), 30);
  window.scrollTo({top:0,behavior:'smooth'});
}
/* 数据到达后重渲染当前视图内容（保留激活态） */
function rerenderView(id){
  if(curView!==id) return;
  const el = byId('v-'+id); if(!el || !VIEWS[id]) return;
  el.innerHTML = VIEWS[id]();
  hydrateTables();
  if(id.startsWith('dash')) renderDash();
}
async function refreshViewData(id){
  await loadAllBase();
  await loadDashboard();
  renderViews();
  switchView(id);
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
/* 从当前看板视图读取绝对日期区间（日期框优先；快捷范围会写入日期框，天然覆盖） */
function chartRange(view){
  const s = view?.querySelector('[data-range-start]')?.value || '';
  const e = view?.querySelector('[data-range-end]')?.value || '';
  return { start:s, end:e };
}
/* 判断某个数据点日期是否落在区间内（兼容 YYYY-MM 与 YYYY-MM-DD） */
function inChartRange(date, range){
  if(!range || (!range.start && !range.end)) return true;
  const raw = String(date||'');
  if(!raw) return false;
  // 宏观日期为 YYYY-MM，ETF/交易为 YYYY-MM-DD；统一按日粒度比较，避免月份边界被误剔除
  const d = raw.length===7 ? raw+'-01' : raw;
  const lo = range.start ? (range.start.length===7 ? range.start+'-01' : range.start) : '';
  const hi = range.end ? (range.end.length===7 ? range.end+'-31' : range.end) : '';
  if(lo && d < lo) return false;
  if(hi && d > hi) return false;
  return true;
}
/* 按日期区间生成过滤后的图表副本，供各渲染函数直接使用，避免漏过滤。
   ETF 净流入排名（etf_rank）x 轴是 ETF 名称，series 不过滤，但更新明细区间。 */
function rangeChart(chart, range, months){
  if(!chart) return chart;
  const copy = {...chart};
  const isNameAxis = chart.layout==='etf_rank';
  if(!isNameAxis && Array.isArray(chart.series)){
    copy.series = chart.series.map(s=>({...s, data:(s.data||[]).filter(p=>inChartRange(p?.[0], range))}));
  }
  if(Array.isArray(chart.index_data)){
    copy.index_data = chart.index_data.filter(p=>inChartRange(p?.[0], range));
  }
  if(isNameAxis && range){
    if(range.start) copy.detail_start = range.start;
    if(range.end) copy.detail_end = range.end;
  }
  return copy;
}
function axisOpt(ax,i){
  return {type:'value',name:ax?.name||'',position:ax?.position||'left',offset:ax?.offset||0,
    nameTextStyle:{fontSize:9,color:'#8a97ac'},axisLabel:{fontSize:9,color:'#8a97ac'},
    splitLine:{show:i===0,lineStyle:{color:'#edf1f7'}},axisLine:{show:i===0}};
}
function renderChartNode(node, chart, months){
  if(!node) return;
  const ins = initChart(node);
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
  const ins = initChart(node);
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
/* ==========================================================================
 * ETF 维度图表（1:1 复刻生产）：
 *  - etf_overlay：上下双 grid（上=指数折线，下=份额/净流入柱），联动 dataZoom
 *  - etf_rank：ETF 净流入横向排名柱，点击柱子展开该 ETF 每日明细
 *  - 普通 ETF 时间序列：份额/净流入柱线 + 竖向网格
 * ========================================================================== */
const ETF_RANGE = () => {
  const view = $('.view.active');
  const s = view?.querySelector('[data-range-start]')?.value || '';
  const e = view?.querySelector('[data-range-end]')?.value || '';
  const months = readQuickMonths(view, 12);
  const p = { months };
  if (s) p.start = s;
  if (e) p.end = e;
  return p;
};
const fmtNum = (v, d=2) => (v==null||v==='') ? '—' : Number(v).toLocaleString('zh-CN',{maximumFractionDigits:d});
const signed = (v, unit) => { const n=Number(v||0); return `<span class="${n>=0?'up':'down'}">${n>=0?'+':''}${fmtNum(n)} <small>${unit}</small></span>`; };

function renderEtfChartNode(node, chart, months){
  if(!node) return null;
  const ins = initChart(node);
  const isRank = chart.layout==='etf_rank';
  const allCats = [...new Set((chart.series||[]).flatMap(s=>(s.data||[]).map(p=>p[0])))];
  const categories = isRank ? allCats : allCats.sort();
  const dataOf = s => { const m=new Map(s.data||[]); return categories.map(c=>m.has(c)?m.get(c):null); };
  const hasBar = (chart.series||[]).some(s=>s.type==='bar');

  if(chart.layout==='etf_overlay'){
    const idxName = chart.index_name || '上证指数 (点)';
    const idxLookup = new Map(chart.index_data||[]);
    const idxValues = categories.map(c=>idxLookup.has(c)?idxLookup.get(c):null);
    const lowerAxes = (chart.y_axes||[]).map((a,i)=>({
      type:'value', name:a.name||'', position:a.position||'left', offset:a.offset||0, gridIndex:1,
      nameTextStyle:{fontSize:9,padding:[0,0,0,a.offset?32:0]}, axisLabel:{fontSize:9},
      splitLine:{show:true,lineStyle:{color:i===0?'#e6eaf0':'#eef1f6',type:i===0?'solid':'dashed'}},
    }));
    ins.setOption({animation:false,color:CLR,
      grid:[{left:62,right:34+(lowerAxes.length-1)*62,top:52,height:'30%'},
            {left:62,right:34+(lowerAxes.length-1)*62,top:'50%',bottom:96}],
      axisPointer:{link:[{xAxisIndex:'all'}]},
      legend:{top:4,type:'scroll',textStyle:{fontSize:10}},
      tooltip:{trigger:'axis',axisPointer:{type:'cross'},confine:true},
      xAxis:[
        {type:'category',gridIndex:0,data:categories,boundaryGap:hasBar,axisLabel:{show:false},axisTick:{show:false},splitLine:{show:true,lineStyle:{color:'#eef1f6',type:'dashed'}}},
        {type:'category',gridIndex:1,data:categories,boundaryGap:hasBar,splitLine:{show:true,lineStyle:{color:'#eef1f6',type:'dashed'}},axisLabel:{fontSize:9,hideOverlap:true,rotate:30,margin:14}},
      ],
      yAxis:[{type:'value',name:idxName,position:'left',gridIndex:0,scale:true,nameTextStyle:{fontSize:9},axisLabel:{fontSize:9,formatter:v=>Number(v).toFixed(0)},splitLine:{show:true,lineStyle:{color:'#e6eaf0'}}},...lowerAxes],
      dataZoom:[{type:'inside',xAxisIndex:[0,1]},{type:'slider',xAxisIndex:[0,1],height:20,bottom:8,brushSelect:false,showDetail:true,handleSize:'110%',dataBackground:{lineStyle:{opacity:.3},areaStyle:{opacity:.1}}}],
      series:[
        {name:idxName,type:'line',xAxisIndex:0,yAxisIndex:0,data:idxValues,smooth:true,showSymbol:false,connectNulls:true,lineStyle:{width:2,color:'#3b5bdb'},itemStyle:{color:'#3b5bdb'},areaStyle:{color:'#3b5bdb',opacity:.08}},
        ...(chart.series||[]).map((s,i)=>({name:s.name,type:s.type||'bar',xAxisIndex:1,yAxisIndex:(s.y_axis_index||0)+1,data:dataOf(s),barMaxWidth:20,barGap:'20%',itemStyle:{color:CLR[(i+1)%CLR.length]}})),
      ],
    },{notMerge:true});
    return ins;
  }

  const axisCount=(chart.y_axes||[]).length||1;
  ins.setOption({animation:false,color:CLR,
    grid: isRank ? {left:62,right:24,top:30,bottom:82} : {left:62,right:34+(axisCount-1)*62,top:54,bottom:92},
    legend: isRank?{show:false}:{top:6,type:'scroll',textStyle:{fontSize:10}},
    tooltip:{trigger:'axis',axisPointer:{type:isRank?'shadow':'cross'},confine:true,
      valueFormatter:isRank?(v)=>(v==null?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:2})+' 亿元'):undefined},
    xAxis:{type:'category',data:categories,boundaryGap:hasBar,
      splitLine:{show:!isRank,lineStyle:{color:'#eef1f6',type:'dashed'}},
      axisLabel:{fontSize:9,hideOverlap:!isRank,rotate:isRank?42:30,interval:isRank?0:'auto',margin:isRank?8:14}},
    yAxis:(chart.y_axes||[{name:''}]).map((a,i)=>({type:'value',name:a.name||'',position:a.position||'left',axisLabel:{fontSize:9},nameTextStyle:{fontSize:9},splitLine:{show:i===0,lineStyle:{color:i===0?'#e6eaf0':'#eef1f6',type:i===0?'solid':'dashed'}}})),
    series:(chart.series||[]).map((s,i)=>({name:s.name,type:s.type||'line',data:dataOf(s),yAxisIndex:s.y_axis_index||0,
      smooth:s.type!=='bar',showSymbol:false,connectNulls:true,barMaxWidth:isRank?16:20,barGap:'20%',
      lineStyle:{width:2,color:CLR[i%CLR.length]},itemStyle:{color:CLR[i%CLR.length]}})),
  },{notMerge:true});
  return ins;
}

/* ---- ETF 明细交互（按天 / 单 ETF / 总份额） ---- */
let etfDetailState = {code:'', page:1, start:'', end:''};

let _etfDailySeq = 0;
async function etfLoadDailyDetail(){
  const seq = ++_etfDailySeq;
  let tableBox = byId('etfDailyTable'), totalBox = byId('etfDailyTotal');
  if(!tableBox||!totalBox) return;
  const p = ETF_RANGE();
  const qs = new URLSearchParams({months:String(p.months||12)});
  if(p.start) qs.set('start',p.start); if(p.end) qs.set('end',p.end);
  try{
    const res = await apiRetry('/api/dashboard/etf-daily?'+qs.toString());
    if(seq !== _etfDailySeq) return;  // 已有更新的加载请求，丢弃本次结果
    // DOM 可能在 await 期间被重建，重新取当前文档中的节点
    totalBox = byId('etfDailyTotal') || totalBox;
    tableBox = byId('etfDailyTable') || tableBox;
    const t=res.total||{share:0,cap:0,net:0,days:0};
    totalBox.innerHTML=`
      <div class="etf-daily-total-card"><span>区间合计申赎净流入</span><b class="${t.net>=0?'up':'down'}">${t.net>=0?'+':''}${fmtNum(t.net)} <small>亿元</small></b></div>
      <div class="etf-daily-total-card"><span>区间合计流通份额变化</span><b class="${t.share>=0?'up':'down'}">${t.share>=0?'+':''}${fmtNum(t.share)} <small>亿份</small></b></div>
      <div class="etf-daily-total-card"><span>统计范围</span><b class="neutral">${t.days} 个交易日 · ${esc(res.range||'')}</b></div>`;
    const rows=res.rows||[];
    tableBox.innerHTML=`<table><thead><tr><th>交易日期</th><th>申赎净流入(亿元)</th><th>流通份额变化(亿份)</th><th>ETF数</th><th style="width:72px">当日明细</th></tr></thead>
      <tbody>${rows.map(r=>`<tr class="etf-day-row" data-date="${esc(r.date)}">
        <td>${esc(r.date)}</td><td class="num"><span class="${(r.net||0)>=0?'up':'down'}">${r.net==null?'—':((r.net>=0?'+':'')+fmtNum(r.net))}</span></td>
        <td class="num"><span class="${(r.share||0)>=0?'up':'down'}">${r.share>=0?'+':''}${fmtNum(r.share)}</span></td><td class="num">${r.count}</td>
        <td><button type="button" class="btn btn-sm etf-day-toggle" data-date="${esc(r.date)}">展开</button></td></tr>
        <tr class="etf-day-detail-row" data-detail-date="${esc(r.date)}" hidden><td colspan="5"></td></tr>`).join('')}</tbody></table>`;
    tableBox.querySelectorAll('.etf-day-toggle').forEach(b=>b.addEventListener('click',()=>etfToggleDayDetail(b.dataset.date)));
  }catch(e){ tableBox.innerHTML=`<div class="hint">按天明细加载失败：${esc(e.message||e)}</div>`; }
}
async function etfToggleDayDetail(date){
  const row=$(`.etf-day-row[data-date="${date}"]`), detail=$(`.etf-day-detail-row[data-detail-date="${date}"]`);
  if(!row||!detail) return;
  const btn=row.querySelector('.etf-day-toggle');
  if(!detail.hidden){ detail.hidden=true; btn.textContent='展开'; return; }
  detail.hidden=false; btn.textContent='收起';
  const cell=detail.querySelector('td'); cell.innerHTML='<div class="hint">加载当日明细…</div>';
  try{
    const p=ETF_RANGE(), qs=new URLSearchParams({months:String(p.months||12),date});
    if(p.start)qs.set('start',p.start); if(p.end)qs.set('end',p.end);
    const res=await api('/api/dashboard/etf-daily?'+qs.toString());
    const rows=res.rows||[];
    cell.innerHTML=`<div class="etf-day-detail-title">${esc(date)} 当日各 ETF 明细（按申赎净流入降序，共 ${rows.length} 只）· 申赎净流入=份额变化×当日净值，剔除市值波动与份额折算</div>
      <table class="etf-day-detail-table"><thead><tr><th>ETF 名称</th><th>代码</th><th>申赎净流入(亿元)</th><th>份额变化(亿份)</th><th>变化率</th></tr></thead>
      <tbody>${rows.map(r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.code)}</td>
        <td class="num"><span class="${(r.net||0)>=0?'up':'down'}">${r.net==null?'折算日':((r.net>=0?'+':'')+fmtNum(r.net))}</span></td>
        <td class="num"><span class="${(r.share||0)>=0?'up':'down'}">${r.share>=0?'+':''}${fmtNum(r.share)}</span></td>
        <td class="num">${r.share_rate==null?'—':fmtNum(r.share_rate)+'%'}</td></tr>`).join('')}</tbody></table>`;
  }catch(e){ cell.innerHTML=`<div class="hint">加载失败：${esc(e.message||e)}</div>`; }
}
async function etfOpenRankDetail(chart, meta){
  const panel=$(`[data-detail-for="${chart.id}"]`); if(!panel) return;
  if(!panel.hidden && etfDetailState.code===meta.code){ panel.hidden=true; etfDetailState.code=''; return; }
  etfDetailState.code=meta.code; etfDetailState.page=1;
  etfDetailState.start=chart.detail_start||''; etfDetailState.end=chart.detail_end||'';
  panel.hidden=false; panel.innerHTML='<div class="hint">加载每日明细…</div>';
  await etfRenderRankPage(panel, chart, meta);
}
async function etfRenderRankPage(panel, chart, meta){
  const qs=new URLSearchParams({code:meta.code,page:String(etfDetailState.page),page_size:'100',order:'desc'});
  if(etfDetailState.start)qs.set('start',etfDetailState.start); if(etfDetailState.end)qs.set('end',etfDetailState.end);
  try{
    const res=await api('/api/data/etf?'+qs.toString());
    if(etfDetailState.code!==meta.code) return;
    const rows=res.rows||[];
    const netCell=v=>v==null?'<span class="muted-inline">折算日</span>':`<span class="${v>=0?'up':'down'}">${v>=0?'+':''}${fmtNum(v)} <small>亿元</small></span>`;
    panel.innerHTML=`<div class="etf-day-detail-title">${esc(meta.name)}（${esc(meta.code)}）· 每日申赎明细（共 ${fmtNum(res.total)} 条，第 ${res.page} 页）</div>
      <table class="etf-day-detail-table"><thead><tr><th>交易日期</th><th>申赎净流入(亿元)</th><th>份额(亿份)</th><th>份额变化(亿份)</th><th>变化率(%)</th></tr></thead>
      <tbody>${rows.map(r=>`<tr><td>${esc(r['交易日期'])}</td><td class="num">${netCell(r['净流入(亿元)'])}</td>
        <td class="num">${fmtNum(r['份额(亿份)'])}</td><td class="num">${signed(r['份额变化(亿份)'],'亿份')}</td>
        <td class="num">${r['份额变化率(%)']==null?'—':fmtNum(r['份额变化率(%)'])+'%'}</td></tr>`).join('')}</tbody></table>
      <div class="pager" style="margin-top:8px">${res.page>1?`<button class="btn btn-sm" data-act="etf-rank-prev">上一页</button>`:''}<span class="hint"> 第 ${res.page}/${res.pages} 页 </span>${res.page<res.pages?`<button class="btn btn-sm" data-act="etf-rank-next">下一页</button>`:''}</div>`;
    panel.querySelector('[data-act="etf-rank-prev"]')?.addEventListener('click',()=>{etfDetailState.page--;etfRenderRankPage(panel,chart,meta);});
    panel.querySelector('[data-act="etf-rank-next"]')?.addEventListener('click',()=>{etfDetailState.page++;etfRenderRankPage(panel,chart,meta);});
  }catch(e){ panel.innerHTML=`<div class="hint">加载失败：${esc(e.message||e)}</div>`; }
}
function etfRenderSharesBlock(totalBox, tableBox, res, rangeParams){
  const t=res.total||{shares:0,days:0};
  const asc=[...(res.rows||[])].sort((a,b)=>String(a.date).localeCompare(String(b.date)));
  let running=0; asc.forEach(r=>{running+=(r.change||0); r._cum=Number(running.toFixed(4));});
  const cum=Number(running.toFixed(4));
  totalBox.innerHTML=`<div class="etf-daily-total-card"><span>最新交易日总份额</span><b class="neutral">${fmtNum(t.shares)} <small>亿份</small></b></div>
    <div class="etf-daily-total-card"><span>区间累计份额变化</span><b class="${cum>=0?'up':'down'}">${cum>=0?'+':''}${fmtNum(cum)} <small>亿份</small></b></div>
    <div class="etf-daily-total-card"><span>统计交易日数</span><b class="neutral">${t.days} 个交易日 · ${esc(res.range||'')}</b></div>`;
  const desc=[...asc].reverse();
  Object.assign(ETF_CALC_TABLES.shares,{rows:desc,page:1,total:desc.length,rangeParams});
  renderEtfCalcTable('shares');
  return {asc,cum};
}
let _etfSharesSeq = 0;
async function etfLoadSharesDetail(){
  const seq = ++_etfSharesSeq;
  let tableBox=byId('etfSharesTable'), totalBox=byId('etfSharesTotal');
  if(!tableBox||!totalBox) return;
  const p=ETF_RANGE(); const rangeParams={months:p.months||12};
  if(p.start)rangeParams.start=p.start; if(p.end)rangeParams.end=p.end;
  try{
    const qs=new URLSearchParams({...rangeParams,view:'total'});
    const res=await apiRetry('/api/dashboard/etf-daily?'+qs.toString());
    if(seq !== _etfSharesSeq) return;  // 已有更新的加载请求，丢弃本次结果
    // DOM 可能在 await 期间被重建，重新取当前文档中的节点
    totalBox = byId('etfSharesTotal') || totalBox;
    tableBox = byId('etfSharesTable') || tableBox;
    const {asc}=etfRenderSharesBlock(totalBox,tableBox,res,rangeParams);
    const box=byId('etfSharesCumChart');
    if(box){
      const ins=initChart(box);
      ins.setOption({animation:false,color:CLR,
        grid:{left:62,right:44,top:30,bottom:70},
        legend:{top:2,type:'scroll',textStyle:{fontSize:9}},tooltip:{trigger:'axis',axisPointer:{type:'shadow'},confine:true},
        xAxis:{type:'category',data:asc.map(r=>r.date),axisLabel:{fontSize:9,hideOverlap:true,rotate:30,margin:12}},
        yAxis:[{type:'value',name:'总份额 (亿份)',position:'left',axisLabel:{fontSize:9},nameTextStyle:{fontSize:9},splitLine:{show:true,lineStyle:{color:'#e6eaf0'}}},
               {type:'value',name:'累计变化 (亿份)',position:'right',axisLabel:{fontSize:9},nameTextStyle:{fontSize:9},splitLine:{show:false}}],
        series:[{name:'汇金总份额',type:'bar',data:asc.map(r=>r.shares),yAxisIndex:0,itemStyle:{color:CLR[0]}},
                {name:'累计份额变化',type:'line',data:asc.map(r=>r._cum),yAxisIndex:1,smooth:true,showSymbol:false,lineStyle:{width:2,color:CLR[1]},itemStyle:{color:CLR[1]}}],
      },{notMerge:true});
      ins.off('click');
      ins.on('click',pp=>{
        if(pp.componentType!=='series'||pp.seriesType!=='bar'||pp.dataIndex==null) return;
        const date=asc[pp.dataIndex]?.date; if(!date) return;
        const st=ETF_CALC_TABLES.shares, idx=st.rows.findIndex(r=>r.date===date);
        if(idx>=0){ st.page=Math.floor(idx/st.size)+1; renderEtfCalcTable('shares'); }
        etfToggleSharesDayDetail(date,tableBox,rangeParams);
        const row=tableBox.querySelector(`.etf-shares-row[data-date="${date}"]`);
        if(row) row.scrollIntoView({behavior:'smooth',block:'center'});
      });
    }
  }catch(e){ tableBox.innerHTML=`<div class="hint">总份额明细加载失败：${esc(e.message||e)}</div>`; }
}
async function etfToggleSharesDayDetail(date, tableBox, rangeParams){
  const row=tableBox.querySelector(`.etf-shares-row[data-date="${date}"]`), detail=tableBox.querySelector(`.etf-shares-detail-row[data-detail-date="${date}"]`);
  if(!row||!detail) return;
  const btn=row.querySelector('.etf-shares-toggle');
  if(!detail.hidden){ detail.hidden=true; btn.textContent='展开'; return; }
  detail.hidden=false; btn.textContent='收起';
  const cell=detail.querySelector('td'); cell.innerHTML='<div class="hint">加载当日明细…</div>';
  try{
    const qs=new URLSearchParams({...(rangeParams||{}),view:'total',date});
    ['start','end'].forEach(k=>{if(!qs.get(k))qs.delete(k);});
    const res=await api('/api/dashboard/etf-daily?'+qs.toString());
    const rows=res.rows||[];
    cell.innerHTML=`<div class="etf-day-detail-title">${esc(date)} 当日各 ETF 份额明细（按份额降序，共 ${rows.length} 只 · 合计 ${fmtNum(res.total_shares||0)} 亿份）· 占比=该 ETF 份额 / 汇金总份额</div>
      <table class="etf-day-detail-table"><thead><tr><th>ETF 名称</th><th>代码</th><th>份额(亿份)</th><th>占比</th><th>较上日变化(亿份)</th></tr></thead>
      <tbody>${rows.map(r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.code)}</td><td class="num"><b>${fmtNum(r.shares)}</b></td>
        <td class="num">${fmtNum(r.pct)}%</td><td class="num"><span class="${(r.change||0)>=0?'up':'down'}">${r.change==null?'—':((r.change>=0?'+':'')+fmtNum(r.change))}</span></td></tr>`).join('')}</tbody></table>`;
  }catch(e){ cell.innerHTML=`<div class="hint">加载失败：${esc(e.message||e)}</div>`; }
}

function renderDash(){
  const grp = curView.replace('dash','');
  if(!DB[grp]) return;
  const view = $('.view.active'); if(!view) return;
  const box = view.querySelector('[data-dash-charts]'); if(!box) return;
  const months = readQuickMonths(view, 12);
  // 宏观 / ETF / 交易统一用日期框的绝对区间过滤（快捷范围会写入日期框）
  const range = chartRange(view);
  const hasRange = range && (range.start || range.end);
  const list = DB[grp].charts.map(c=>hasRange ? rangeChart(c, range, months) : c);
  // 有绝对区间时不再让渲染函数按 months 二次裁剪
  const renderMonths = hasRange ? null : months;
  // ETF 图1（叠指数的汇金图）与图2 各自的明细卡片：按卡片类型占满一行
  const cardHtml = (c)=>{
    const wide = (c.layout==='etf_overlay' || c.layout==='etf_rank' || c.layout==='trade_margin_split');
    const tall = c.layout==='trade_margin_split' || c.layout==='etf_rank';
    const extra = c.layout==='etf_overlay' ? `<div class="card extra-detail-card" data-extra="daily">
        <div class="card-header"><h3 style="font-size:12.5px">汇金维度 · 按天流通份额 / 申赎净流入明细</h3><span class="hint" style="margin-left:auto">点击某天展开当日各 ETF 明细</span></div>
        <div class="card-body"><div class="etf-daily-total" id="etfDailyTotal"></div><div class="table-wrap" id="etfDailyTable"><div class="hint">加载中…</div></div></div></div>
      <div class="card extra-detail-card" data-extra="shares">
        <div class="card-header"><h3 style="font-size:12.5px">汇金维度 · 每日总份额（含累计变化）</h3><span class="hint" style="margin-left:auto">柱子=每日份额存量，折线=累计变化，点击柱子展开当日 ETF 明细</span></div>
        <div class="card-body"><div class="chart-box" id="etfSharesCumChart" style="height:300px"></div><div class="etf-daily-total" id="etfSharesTotal"></div><div class="table-wrap" id="etfSharesTable"><div class="hint">加载中…</div></div></div></div>` : '';
    const rankDetail = c.layout==='etf_rank' ? `<div class="etf-daily-detail" data-detail-for="${esc(c.id)}" hidden></div>` : '';
    return `<div class="card ${wide?'wide-card':''}" data-chart-card="${esc(c.id)}"><div class="card-header" style="padding:9px 13px"><h3 style="font-size:12.5px">${esc(c.title)}</h3>${c.note?`<i class="ti ti-info-circle" style="margin-left:auto;color:#8fa6c4" title="${esc(c.note)}"></i>`:''}</div>
      <div class="card-body" style="padding:6px"><div class="chart-box ${tall?'tall':''} ${c.layout==='etf_overlay'?'overlay':''}"></div>${rankDetail}</div></div>${extra}`;
  };
  box.innerHTML = list.map(cardHtml).join('');
  const meta = view.querySelector('[data-dash-meta]');
  if(meta) meta.textContent = `宏观 8 图 · ETF 4 图 · 交易 2 图 · 已渲染 ${list.length} 图`;
  setTimeout(()=>{
    list.forEach((c,idx)=>{
      const card = box.querySelector(`[data-chart-card="${c.id}"]`);
      const target = card?.querySelector('.chart-box');
      if(!target) return;
      let ins;
      if(grp==='etf') ins = renderEtfChartNode(target,c,renderMonths);
      else if(c.layout==='trade_margin_split') ins = renderMarginSplitNode(target,c,renderMonths);
      else ins = renderChartNode(target,c,renderMonths);
      if(c.layout==='etf_rank' && ins){
        target.style.cursor='pointer'; target.title='点击任意 ETF 柱子，展开该 ETF 每日明细';
        ins.off('click');
        ins.on('click', p=>{
          if(p.componentType!=='series'||p.dataIndex==null) return;
          const m=(c.rank_meta||[])[p.dataIndex]; if(!m?.code) return;
          etfOpenRankDetail(c,m);
        });
      }
    });
    // ETF 明细数据加载（防抖：快速切换/连续筛选时合并为一次，避免请求风暴）
    if(grp==='etf') scheduleEtfDetailLoad();
  },40);
}
let _etfDetailTimer = null;
function scheduleEtfDetailLoad(){
  clearTimeout(_etfDetailTimer);
  _etfDetailTimer = setTimeout(()=>{
    if(curView!=='dashetf') return;
    if(byId('etfDailyTable')) etfLoadDailyDetail();
    if(byId('etfSharesTable')) etfLoadSharesDetail();
  }, 180);
}
function renderOverview(){
  const node = byId('ovAssetChart'); if(!node) return;
  const ins = initChart(node);
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
  const end = new Date(LAST);
  const start = new Date(LAST);
  if(months>=999) start.setFullYear(start.getFullYear()-20); else start.setMonth(start.getMonth()-months);
  const lo = start.toISOString().slice(0,10);
  // 看板视图：data-range-start/end，直接重绘
  const s = view.querySelector('[data-range-start]'), e = view.querySelector('[data-range-end]');
  if(s && e){
    s.value = lo; e.value = LAST;
    const hint = view.querySelector('[data-range-hint]');
    if(hint) hint.textContent = `已回显 ${lo} ~ ${LAST}（快捷范围）`;
    // 看板与 ETF 计算都需要按新范围重新请求后端，否则只能拿到默认近一年数据
    if(curView.startsWith('dash')) loadDashboard();
    if(curView==='etfcalc') loadEtfCalc();
    toast(`已切换范围：${btn.textContent.trim()}`, 'info');
    return;
  }
  // 预览视图：data-filter-start/end，写入状态并真实重载
  const fs = view.querySelector('[data-filter-start]'), fe = view.querySelector('[data-filter-end]');
  const bar = view.querySelector('[data-preview-filter]');
  if(fs && fe && bar){
    fs.value = lo; fe.value = LAST;
    const kind = bar.dataset.kind||curView.replace('prev-','');
    const st = PREVIEW_STATE[kind]; if(!st) return;
    st.start = lo; st.end = LAST; st.page = 1;
    loadPreview(kind,{page:st.page,page_size:st.size,code:st.code,start:st.start,end:st.end,order:st.order})
      .then(()=>{ renderPreviewTable(kind); renderPreviewPager(kind); });
    toast(`已切换范围：${btn.textContent.trim()}（${lo} ~ ${LAST}）`, 'info');
    return;
  }
  toast(`已切换范围：${btn.textContent.trim()}`, 'info');
}
function setPagerActive(btn){
  const bar = btn.closest('[data-pager]'); if(!bar) return;
  const [scope,kind] = String(bar.dataset.scope||'').split(':');
  const st = scope==='mon' ? MONITOR_STATE[kind] : scope==='prev' ? PREVIEW_STATE[kind] : scope==='calc' ? ETF_CALC_TABLES[kind] : null;
  if(!st) return;
  const act = btn.dataset.act;
  const pages = scope==='mon'
    ? Math.max(1,Math.ceil(monitorFilteredRows(kind).length/(Number(st.size)||25)))
    : (Number(st.pages)||1);
  let page = Number(st.page)||1;
  if(act==='page-go') page = Number(btn.dataset.p)||1;
  else if(act==='page-first') page = 1;
  else if(act==='page-last') page = pages;
  else if(act==='page-prev') page = Math.max(1,page-1);
  else if(act==='page-next') page = Math.min(pages,page+1);
  if(scope==='mon') monitorGo(kind,page);
  else if(scope==='calc'){ st.page=page; renderEtfCalcTable(kind); }
  else previewGo(kind,page);
}
function handleTableAction(act, btn){
  const view = activeView(); if(!view) return;
  const row = btn.closest('tr');
  if(act==='monitor-expand'){
    let next=row?.nextElementSibling;
    if(next?.hasAttribute('data-monitor-detail') && !next.hidden){
      next.hidden=true; btn.textContent='▸'; return;
    }
    const runId=btn.dataset.runId||'';
    if(!next?.hasAttribute('data-monitor-detail')){
      next=document.createElement('tr');
      next.className='etf-day-detail-row';
      next.setAttribute('data-monitor-detail',runId);
      next.innerHTML=`<td colspan="${row?.children.length||10}"></td>`;
      row?.after(next);
    }
    const details=(MONITOR_RAW.detail||[]).filter(d=>String(d['运行ID']||'')===runId);
    const cols=['运行时间','接口名称','类别','状态','数据行数','失败原因','耗时(秒)'];
    const head=`<div class="etf-day-detail-title">运行 ${esc(runId)} · 接口调用明细（${details.length} 条）</div>`+
      `<table class="etf-day-detail-table"><thead><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>`;
    const body=details.map(d=>'<tr>'+cols.map(c=>{
      const v=d[c]==null?'':String(d[c]);
      if(c==='状态') return `<td><span class="tag ${v==='success'?'success':'failed'}">${v==='success'?'成功':'失败'}</span></td>`;
      return `<td>${v?esc(v):'—'}</td>`;
    }).join('')+'</tr>').join('');
    const td=next?.querySelector('td');
    if(td){ td.innerHTML=details.length?head+body+'</tbody></table>':`<div class="hint">运行 ${esc(runId)} 无接口调用明细</div>`; next.hidden=false; btn.textContent='▾'; }
    return;
  }
  if(act==='retry-one'){
    const cells = row?.querySelectorAll('td');
    const taskId = btn.dataset.taskId || cells?.[0]?.textContent?.trim()||'';
    if(!taskId){ toast('缺少任务ID','warn'); return; }
    btn.textContent='重跑中…'; btn.disabled=true;
    submitJob('/api/retry/task', {task_id:taskId}, `重跑任务 ${taskId}`).then(()=>{
      btn.textContent='再次重跑'; btn.disabled=false;
    });
    return;
  }
  if(act==='etf-share-detail'){
    const next = row?.nextElementSibling;
    if(next?.classList.contains('log-detail')){ next.remove(); btn.textContent='展开 ▸'; return; }
    const tr=document.createElement('tr'); tr.className='log-detail';
    tr.innerHTML=`<td colspan="4"><div class="log-detail" style="margin:0">汇金 ETF 每日份额明细读取自 etf_observation；点击看板「汇金维度」图可展开每日明细。</div></td>`;
    row?.after(tr); btn.textContent='收起 ▾'; return;
  }
  if(act==='download'){
    const link = row?.querySelector('a[href]') || btn.closest('td')?.querySelector('a[href]');
    if(link){ window.location.href = link.href; } else toast('该文件无下载链接','warn');
    return;
  }
}

/* ==========================================================================
 * 动作分派
 * ========================================================================== */
function handleAction(act, btn, e){
  const view = activeView();
  switch(act){
    /* 采集控制台 */
    case 'collect-full': {
      const row = btn.closest('.ctl-row');
      const dates = row?.querySelectorAll('input[type="date"]');
      const start = dates?.[0]?.value||'', end = dates?.[1]?.value||'';
      submitJob('/api/collect', {macro_only:false,etf_only:false,index_only:false,start,end}, '全量采集');
      break;
    }
    case 'collect-prec': {
      const row = btn.closest('.ctl-row');
      const cat = row?.querySelector('[data-act="prec-cat"]')?.value || 'macro';
      const code = row?.querySelector('[data-prec-code]')?.value || '';
      const codes = code ? [code] : [];
      const start = row?.querySelector('[data-prec-start]')?.value||'';
      const end = row?.querySelector('[data-prec-end]')?.value||'';
      submitJob('/api/collect/interface', {category:cat,codes,start,end}, `精确采集(${cat}${code?'·'+code:'·全部'})`);
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
      if(!f?.files?.length){ toast('请先选择文件','warn'); break; }
      const form = new FormData();
      Array.from(f.files).forEach(x=>form.append('files', x));
      setBusy(true,'文件导入'); appendLog(`[导入] 上传 ${f.files.length} 个文件`, 'INFO');
      api('/api/import/etf', {method:'POST', body:form}).then(res=>{
        setBusy(false); appendLog(`[导入] 完成：${res.message||res.imported||'成功'}`,'OK');
        toast('文件导入完成','ok'); loadStatus();
      }).catch(err=>{ setBusy(false); toast('导入失败：'+err.message,'err'); });
      break;
    }
    case 'retry-all': modal('批量重跑失败','将重跑最近一次失败批次的全部接口。此操作不可撤销。',()=>{
      submitJob('/api/retry', {}, '批量重跑失败');
    },'确认重跑'); break;
    case 'log-bottom': { const w=byId('logBodyWrap'); if(w) w.scrollTop=w.scrollHeight; break; }
    case 'log-toggle': {
      const p = byId('logBodyWrap')?.closest('.log-panel');
      if(p){ p.classList.toggle('collapsed'); btn.textContent = p.classList.contains('collapsed')?'收起':'展开'; }
      break;
    }
    /* 配置 */
    case 'save-config': {
      if(curView==='etfcfg') { saveEtfConfigUI(view); }
      else if(curView==='idxcfg') { saveIndexConfigUI(view); }
      else if(curView==='maccfg') { saveMacroConfigUI(view); }
      else toast('配置已保存','ok');
      break;
    }
    case 'save-sched': {
      const en = $('#dpSchedEnabled')?.checked ?? true;
      const tm = $('#dpSchedTime')?.value || '03:17';
      const rt = $('#dpSchedRunType')?.value || 'full';
      const lb = Number($('#dpSchedLookback')?.value ?? 2);
      postJSON('/api/scheduler', {enabled:en, time:tm, run_type:rt, macro_lookback_months:lb})
        .then(()=>{ toast('调度配置已保存','ok'); loadStatus().then(()=>rerenderView('tasksched')); })
        .catch(err=>toast('保存失败：'+err.message,'err'));
      break;
    }
    case 'sched-run-now': submitJob('/api/collect', {macro_only:false,etf_only:false,index_only:false}, '定时调度立即触发'); break;
    case 'idx-add': case 'etf-add': {
      const isIdx = act==='idx-add';
      const inp = view?.querySelector(isIdx?'[data-idx-new]':'[data-etf-new]');
      const val = inp?.tagName==='SELECT' ? inp.value : (inp?.value||'').trim();
      if(!val){ toast('请先选择或输入代码','warn'); break; }
      const cfg = isIdx ? INDEX_CFG : ETF_CFG;
      if(!cfg.items.some(x=>x.code===val)) cfg.items.push({code:val, name:inp?.selectedOptions?.[0]?.text?.split(' ').slice(1).join(' ')||val, tags:[]});
      const wrap = view?.querySelector(`[data-chips="${isIdx?'idx':'etf'}"]`);
      if(wrap){ const chip=document.createElement('span'); chip.className='chip';
        const label=inp?.tagName==='SELECT'?inp.selectedOptions[0].text:val;
        chip.innerHTML=`<i class="ti ti-${isIdx?'chart-dots':'coins'}"></i> ${esc(label)}<span class="x" data-act="chip-remove">×</span>`;
        wrap.appendChild(chip); }
      (isIdx?saveIndexConfigUI:saveEtfConfigUI)(view, true).then(()=>toast(`已添加${isIdx?'指数':'ETF'}：${val}`,'ok'));
      break;
    }
    case 'chip-remove': {
      btn.closest('.chip')?.remove();
      toast('已移除标的（保存配置后生效）','info'); break;
    }
    case 'macro-add': {
      const g = s => view?.querySelector(`[data-macro-${s}]`)?.value?.trim() || '';
      const code=g('code'), name=g('name');
      if(!code||!name){ toast('请填写指标代码与名称','warn'); break; }
      const body = view?.querySelector('[data-macro-table] tbody');
      if(body){ const tr=document.createElement('tr');
        tr.innerHTML=`<td><label class="switch-row"><input type="checkbox" checked></label></td><td><div class="cell-clip">${esc(code)}</div></td><td><div class="cell-clip">${esc(name)}</div></td><td><div class="cell-clip">${esc(g('cat')||'自定义')}</div></td><td><div class="cell-clip">${esc(g('src')||'手动')}</div></td><td><div class="cell-clip">${esc(g('freq')||'月')}</div></td><td><button class="btn btn-sm" data-act="macro-remove">删除</button></td>`;
        body.appendChild(tr); }
      ['code','name','cat','src','freq'].forEach(s=>{const el=view?.querySelector(`[data-macro-${s}]`);if(el)el.value='';});
      toast(`已加入配置表：${name}`,'ok'); break;
    }
    case 'macro-remove': btn.closest('tr')?.remove(); toast('已从配置表删除宏观指标','info'); break;
    case 'idx-row-remove': case 'etf-row-remove': {
      const row=btn.closest('tr'); const code=row?.querySelector('td')?.textContent?.trim();
      const isIdx=act==='idx-row-remove';
      const cfg=isIdx?INDEX_CFG:ETF_CFG;
      cfg.items=cfg.items.filter(x=>x.code!==code);
      row?.remove();
      (isIdx?saveIndexConfigUI:saveEtfConfigUI)(view, true).then(()=>toast(`已删除标的 ${code}`,'ok'));
      break;
    }
    /* 预览 */
    case 'filter': {
      const bar=view?.querySelector('[data-preview-filter]'); if(!bar) break;
      const kind=bar.dataset.kind||curView.replace('prev-','');
      const st=PREVIEW_STATE[kind]; if(!st) break;
      st.code=bar.querySelector('[data-filter-code]')?.value?.trim()||'';
      st.start=bar.querySelector('[data-filter-start]')?.value||'';
      st.end=bar.querySelector('[data-filter-end]')?.value||'';
      st.size=Number(bar.querySelector('[data-filter-size]')?.value)||st.size;
      st.page=1;
      loadPreview(kind,{page:st.page,page_size:st.size,code:st.code,start:st.start,end:st.end,order:st.order}).then(()=>{
        rerenderView(curView); toast('筛选条件已应用','ok');
      });
      break;
    }
    case 'filter-reset': {
      const bar=view?.querySelector('[data-preview-filter]'); if(!bar) break;
      const kind=bar.dataset.kind||curView.replace('prev-','');
      const st=PREVIEW_STATE[kind]; if(!st) break;
      Object.assign(st,{page:1,code:'',start:LAST,end:LAST});
      st.order='desc';
      loadPreview(kind,{page:st.page,page_size:st.size,code:'',start:LAST,end:LAST,order:st.order}).then(()=>{
        rerenderView(curView); toast('已恢复默认区间','info');
      });
      break;
    }
    case 'sort-date': {
      const kind = btn.dataset.kind || curView.replace('prev-','');
      const st = PREVIEW_STATE[kind]; if(!st) break;
      st.order = st.order==='asc' ? 'desc' : 'asc';
      st.page = 1;
      loadPreview(kind,{page:st.page,page_size:st.size,code:st.code,start:st.start,end:st.end,order:st.order})
        .then(()=>{ renderPreviewTable(kind); renderPreviewPager(kind); toast(`日期已切换为${st.order==='asc'?'升序':'降序'}`,'info'); });
      break;
    }
    /* 接口监控筛选 */
    case 'monitor-filter': {
      const bar=view?.querySelector('[data-monitor-filter]'); if(!bar) break;
      const kind=bar.dataset.kind||curView.replace('mon-','');
      const st=MONITOR_STATE[kind]; if(!st) break;
      st.keyword=bar.querySelector('[data-mon-keyword]')?.value?.trim()||'';
      st.run_type=bar.querySelector('[data-mon-run-type]')?.value||'';
      st.category=bar.querySelector('[data-mon-category]')?.value||'';
      st.status=bar.querySelector('[data-mon-status]')?.value||'';
      st.start=bar.querySelector('[data-mon-start]')?.value||'';
      st.end=bar.querySelector('[data-mon-end]')?.value||'';
      st.size=Number(bar.querySelector('[data-mon-size]')?.value)||st.size;
      st.page=1;
      renderMonitorTable(kind); renderMonitorPager(kind);
      toast('筛选条件已应用','ok');
      break;
    }
    case 'monitor-reset': {
      const bar=view?.querySelector('[data-monitor-filter]'); if(!bar) break;
      const kind=bar.dataset.kind||curView.replace('mon-','');
      const st=MONITOR_STATE[kind]; if(!st) break;
      Object.assign(st,{page:1,keyword:'',run_type:'',category:'',status:'',start:WEEK_START,end:LAST});
      rerenderView(curView); toast('已重置筛选（最近一周）','info');
      break;
    }
    case 'export': {
      const urls={macro:'/api/export/macro',etf:'/api/export/etf',index:'/api/export/index',full:'/api/export/full',monitor:'/api/export/monitor',derived:'/api/derived/export',etfcalc:'/api/export/etf'};
      const url=urls[btn.dataset.k];
      if(!url){ toast('未知导出类型','warn'); break; }
      toast('导出任务已提交，请稍后在输出文件查看','ok');
      const req = url.includes('derived/export') ? api(url) : postJSON(url, {});
      req.then(()=>{ pollLiveOnce(); }).catch(err=>toast('导出失败：'+err.message,'err'));
      break;
    }
    case 'page-go': case 'page-first': case 'page-last': case 'page-prev': case 'page-next': setPagerActive(btn); break;
    /* 清理 */
    case 'clean-cat': {
      $$('[data-clean-cat] .seg-btn',view).forEach(x=>x.classList.toggle('active',x===btn));
      const sel=view?.querySelector('[data-clean-code]');
      const items = CATALOG[btn.dataset.cat]||[];
      if(sel) sel.innerHTML='<option value="">全部（该类别整体）</option>'+items.slice(0,80).map(x=>`<option value="${esc(x.code)}">${esc(x.name)}（${esc(x.code)}）</option>`).join('');
      break;
    }
    case 'clean-preview': {
      const p = cleanPayloadFrom(view);
      const r=view?.querySelector('[data-clean-result]');
      postJSON('/api/clean/preview', p).then(res=>{
        if(r) r.innerHTML=`预览结果：<b>将删除 ${res.matched} 行</b>（该类别当前共 ${res.total} 行）。此操作不可撤销。`;
        toast('已计算影响范围','warn');
      }).catch(err=>{ if(r) r.textContent='预览失败：'+err.message; });
      break;
    }
    case 'clean-exec': {
      const p = cleanPayloadFrom(view);
      postJSON('/api/clean/preview', p).then(pre=>{
        if(!pre.matched){ toast('无匹配数据，无需清理','info'); return; }
        modal('执行数据清理',`确认删除 ${pre.matched} 行数据？此操作不可撤销，且不影响配置、监控与导出记录。`,()=>{
          postJSON('/api/clean', {...p, confirm:true}).then(res=>{
            const r=activeView()?.querySelector('[data-clean-result]');
            if(r) r.innerHTML=`清理完成：<b>已删除 ${res.deleted} 行</b>，剩余 ${res.after} 行。`;
            toast(`数据清理完成 · 已删除 ${res.deleted} 行`,'ok');
            loadAllBase();
          }).catch(err=>toast('清理失败：'+err.message,'err'));
        },'确认清理');
      });
      break;
    }
    case 'clean-reset': {
      const r=view?.querySelector('[data-clean-result]');
      if(r) r.innerHTML='预览结果：<span class="hint">将删除 0 行（请先预览影响）。宏观指标计算表不在清理范围内。</span>';
      $$('input[type="date"]',view).forEach(x=>x.value=''); break;
    }
    /* 监控 */
    case 'monitor-clean-toggle': {
      const box=view?.querySelector('[data-monitor-clean]');
      if(box){ box.style.display = box.style.display==='none'?'block':'none'; toast(box.style.display==='none'?'已关闭清理面板':'已展开清理面板','info'); }
      break;
    }
    case 'mc-preview': {
      const p = monitorCleanPayload(view);
      const r=view?.querySelector('[data-mc-result]');
      postJSON('/api/monitor/clean/preview', p).then(res=>{
        if(r) r.innerHTML=`预览 <b>${res.matched}</b> 条接口监控记录将被删除（共 ${res.total||fmt(S.today_calls)} 条）。`;
        toast('已计算监控清理影响','warn');
      }).catch(err=>{ if(r) r.textContent='预览失败：'+err.message; });
      break;
    }
    case 'mc-exec': {
      const p = monitorCleanPayload(view);
      postJSON('/api/monitor/clean/preview', p).then(pre=>{
        modal('清理接口监控',`确认按当前口径删除 ${pre.matched} 条监控记录？`,()=>{
          postJSON('/api/monitor/clean', {...p, confirm:true}).then(res=>{
            const r=activeView()?.querySelector('[data-mc-result]');
            if(r) r.innerHTML=`已删除 <b>${res.deleted||pre.matched}</b> 条接口监控记录。`;
            toast('监控记录清理完成','ok'); loadAllBase();
          }).catch(err=>toast('清理失败：'+err.message,'err'));
        },'确认清理');
      });
      break;
    }
    case 'mc-reset': {
      const r=view?.querySelector('[data-mc-result]'); if(r){ r.className='clean-result'; r.textContent='—'; }
      break;
    }
    case 'range': applyQuickRange(btn); break;
    case 'dash-refresh': loadDashboard().then(()=>toast('图表已刷新','ok')); break;
    /* 宏观指标 / 交易计算 */
    case 'derived-month-step': case 'derived-month-now': {
      const inp = view?.querySelector('[data-derived-view-month]');
      let m = (inp?.value || DERIVED_MONTH || CURRENT_MONTH).slice(0,7);
      if(act==='derived-month-now') m = CURRENT_MONTH;
      else {
        const step = Number(btn.dataset.step||0);
        m = shiftMonth(m+'-01', step).slice(0,7);
      }
      if(inp) inp.value = m;
      loadDerived(m);
      break;
    }
    case 'recalc-all': {
      submitJob('/api/derived/recompute', {month:'', code:''}, '宏观指标全量重算'); break;
    }
    case 'recalc-month': case 'recalc-code': {
      const m = view?.querySelector('[data-derived-recalc-month]')?.value||DERIVED_MONTH;
      const code = view?.querySelector('select')?.value||'';
      submitJob('/api/derived/recompute', {month:act==='recalc-month'?m:'', code:act==='recalc-code'?code:''}, act==='recalc-month'?'宏观指标按月重算':'宏观指标按指标重算');
      break;
    }
    case 'etfcalc-refresh': loadEtfCalc().then(()=>toast('ETF 计算已按当前日期范围刷新','ok')); break;
    case 'trade-recalc-all': submitJob('/api/trade/recompute', {}, '交易指标全量重算'); break;
    case 'trade-recalc-date': {
      const d = view?.querySelector('[data-trade-recalc-date]')?.value||LAST;
      submitJob('/api/trade/recompute', {trade_date:d}, `交易指标重算(${d})`); break;
    }
    /* 表格内操作 */
    case 'row-expand': case 'monitor-expand': case 'retry-one': case 'etf-share-detail': case 'download': handleTableAction(act,btn); break;
    /* 弹窗 */
    case 'modal-cancel': closeModal(); break;
    case 'modal-ok': { const cb=byId('modalRoot')._onOk; closeModal(); if(cb) cb(); break; }
    default: toast(`操作已触发：${act}`,'info');
  }
}

/* ---- 动作辅助：从 UI 组装 payload ---- */
function cleanPayloadFrom(view){
  const cat = view?.querySelector('[data-clean-cat] .seg-btn.active')?.dataset.cat || 'macro';
  const code = view?.querySelector('[data-clean-code]')?.value?.trim()||'';
  const dates = view?.querySelectorAll('.card-body > .ctl-params input[type="date"]');
  const allDates = view?.querySelectorAll('input[type="date"]')||[];
  return {category:cat, code, start:allDates[0]?.value?.trim()||'', end:allDates[1]?.value?.trim()||''};
}
function monitorCleanPayload(view){
  const inputs = view?.querySelectorAll('[data-monitor-clean] input')||[];
  const sels = view?.querySelectorAll('[data-monitor-clean] select')||[];
  const p = {status:'', run_type:'', category:'', keyword:inputs[2]?.value?.trim()||'', start:view?.querySelector('[data-mc-start]')?.value||'', end:view?.querySelector('[data-mc-end]')?.value||''};
  const norm = s => (s||'').includes('成功')?'success':(s||'').includes('失败')?'failed':'';
  if(sels[0]) p.status=norm(sels[0].value);
  if(sels[1]) p.run_type=(sels[1].value||'').includes('全量')?'full':(sels[1].value||'').includes('定时')?'scheduled':(sels[1].value||'').includes('精确')?'interface':'';
  if(sels[2]) p.category=(sels[2].value||'').includes('宏观')?'macro':(sels[2].value||'').includes('ETF')?'etf':'';
  return p;
}
async function saveMacroConfigUI(view, quiet){
  const valOr = (sel, fallback) => { const el=view?.querySelector(sel); return el ? el.value.trim() : (fallback||''); };
  const payload = {start_date:valOr('[data-macro-start]', MACRO_CFG.start_date), end_date:valOr('[data-macro-end]', MACRO_CFG.end_date)};
  try {
    const res = await postJSON('/api/macro/config', payload);
    if(res.config) MACRO_CFG = res.config;
    if(!quiet) toast('宏观采集默认区间已保存','ok');
    return true;
  } catch(e){ if(!quiet)toast('保存失败：'+e.message,'err'); return false; }
}
async function saveEtfConfigUI(view, quiet){
  const valOr = (sel, fallback) => { const el=view?.querySelector(sel); return el ? el.value.trim() : (fallback||''); };
  const payload = {
    codes:(ETF_CFG.items||[]).map(x=>x.code),
    tags:{},
    start_date:valOr('[data-etf-start]', ETF_CFG.start_date),
    end_date:valOr('[data-etf-end]', ETF_CFG.end_date),
    default_trade_days:Number(valOr('[data-etf-days]', ETF_CFG.default_trade_days)) || Number(ETF_CFG.default_trade_days||60),
  };
  (ETF_CFG.items||[]).forEach(x=>{ if(x.tags?.length) payload.tags[x.code]=x.tags; });
  try { const res=await postJSON('/api/etf/config', payload); if(res.config) ETF_CFG=res.config; loadCatalog(); if(!quiet)toast('ETF 配置已保存','ok'); return true; }
  catch(e){ if(!quiet)toast('保存失败：'+e.message,'err'); return false; }
}
async function saveIndexConfigUI(view, quiet){
  const valOr = (sel, fallback) => { const el=view?.querySelector(sel); return el ? el.value.trim() : (fallback||''); };
  const payload = {
    codes:(INDEX_CFG.items||[]).map(x=>x.code),
    start_date:valOr('[data-idx-start]', INDEX_CFG.start_date),
    end_date:valOr('[data-idx-end]', INDEX_CFG.end_date),
    lookback_days:Number(valOr('[data-idx-lookback]', INDEX_CFG.lookback_days)) || Number(INDEX_CFG.lookback_days||1),
  };
  try { const res=await postJSON('/api/index/config', payload); if(res.config) INDEX_CFG=res.config; loadCatalog(); if(!quiet)toast('指数配置已保存','ok'); return true; }
  catch(e){ if(!quiet)toast('保存失败：'+e.message,'err'); return false; }
}
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
      if(sel) sel.innerHTML = precCodeOptionsHtml(t.value);
      const n = previewCodeOptions(t.value).length;
      toast(`类别已切换为「${t.selectedOptions[0]?.text}」，可选「全部」或单个（共 ${n} 项）`,'info');
    }
    if(t.matches('[data-act="overlay-index"]')) loadDashboard();
    if(t.matches('[data-derived-view-month]')) loadDerived(t.value);
    if(t.matches('[data-range-start],[data-range-end]')){
      const view=activeView();
      $$('.seg-btn[data-months]',view).forEach(x=>x.classList.remove('active'));
      const hint=view?.querySelector('[data-range-hint]'); if(hint) hint.textContent='已进入自定义日期范围';
      if(curView.startsWith('dash')) loadDashboard();
      if(curView==='etfcalc') loadEtfCalc();
    }
    if(t.matches('[data-page-size]')){
      const [scope,kind] = String(t.dataset.scope||'').split(':');
      const size = Number(t.value)||25;
      if(scope==='mon' && MONITOR_STATE[kind]){
        Object.assign(MONITOR_STATE[kind],{page:1,size});
        renderMonitorTable(kind); renderMonitorPager(kind);
      } else if(scope==='prev' && PREVIEW_STATE[kind]){
        previewGo(kind,1,size);
      } else if(scope==='calc' && ETF_CALC_TABLES[kind]){
        Object.assign(ETF_CALC_TABLES[kind],{page:1,size});
        renderEtfCalcTable(kind);
      }
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
$('#hdDate').textContent = LAST ? ('交易日 ' + LAST) : '—';
$('#hdTime').textContent = now();
setInterval(()=>{ $('#hdTime').textContent = now(); },1000);
bindEvents();
(async function init(){
  await loadAllBase();
  $('#hdDate').textContent = LAST ? ('交易日 ' + LAST) : '—';
    buildSidebar();
  renderViews();
  // 看板日期控件默认近一年
  if(LAST){ const e=new Date(LAST); const st=new Date(e); st.setMonth(st.getMonth()-12);
    $$('[data-range-start]').forEach(x=>x.value=st.toISOString().slice(0,10));
    $$('[data-range-end]').forEach(x=>x.value=LAST); }
  switchView('ovCollect');
  // 预热日志面板
  pollLiveOnce();
  setInterval(pollLiveOnce, 4000);
})();


