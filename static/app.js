'use strict';

const $ = (id) => document.getElementById(id);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
};

let monitorData = null;
let currentJobId = null;
let jobPollTimer = null;
let monitorTab = 'overview';
let dataTab = 'macro';
let catalogData = null;
let precCategory = 'macro';
let dataPage = 1;
let dataPages = 1;
let dataTotalRows = 0;
let dataDateOrder = 'desc';  // 采集数据日期排序方向：desc 最新在前 / asc 最早在前
const MAX_PAGE_SIZE = 100;

function dateText(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function todayText() {
  return dateText(new Date());
}
// 从今天往前推 N 个月（ clamp 到目标月的最后一天，避免 1/31 -> 2/31 溢出）
function monthsAgoText(months) {
  const d = new Date();
  const day = d.getDate();
  d.setDate(1);
  d.setMonth(d.getMonth() - months);
  const lastDay = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
  d.setDate(Math.min(day, lastDay));
  return dateText(d);
}
// 日期控件默认值：优先用后端返回的「最近一个已结束交易日」，未加载时用当天
let lastTradeDate = '';               // 由 /api/status 注入，形如 2026-11-20
const DEFAULT_START_DATE = todayText();

function defaultTradeDay() {
  return lastTradeDate || todayText();
}
function applyDefaultRange(startId, endId) {
  const d = defaultTradeDay();
  $(startId).value = d;
  $(endId).value = d;
}
let monitorPage = 1;
let monitorPages = 1;
let monitorTotalRows = 0;
let monitorPageSize = 25;
let renderedLogCount = 0;
let logAutoScroll = true;
let liveLogSeq = 0;          // 全局实时日志已渲染到的序号
let liveLogTimer = null;     // 常驻轮询：含定时调度在内的所有线程日志
const LIVE_LOG_LIMIT = 5000;  // 与后端环形缓冲一致：保留最近 5000 行

/* ---------- 概览 ---------- */
async function loadStatus() {
  try {
    const s = await api('/api/status');
    if (s.last_trade_date) lastTradeDate = s.last_trade_date;
    $('sysTime').textContent = s.server_time;
    const sched = s.scheduler;
    const sb = $('schedBadge');
    sb.textContent = sched.enabled ? `定时 ${sched.time} 已启用` : '定时未启用';
    sb.className = 'pill ' + (sched.enabled ? 'ok' : 'ghost');
    $('schedEnabled').checked = sched.enabled;
    $('schedTime').value = sched.time;
    $('schedType').value = sched.run_type;
    $('schedNext').textContent = '下次运行：' + sched.next_run;
    // 数据处理页的触发控制区同步显示（采集页同名字段已只读，仅作展示）
    if ($('dpSchedEnabled')) {
      $('dpSchedEnabled').checked = sched.enabled;
      $('dpSchedTime').value = sched.time;
      $('dpSchedType').value = sched.run_type;
      $('dpSchedNext').textContent = '下次运行：' + sched.next_run;
    }
    const rb = $('runBadge');
    rb.textContent = s.running ? '采集中…' : '空闲';
    rb.className = 'pill ' + (s.running ? 'warn' : 'ghost');
    setButtons(!s.running);

    const last = s.last_run;
    const stats = [
      { label: '最近采集', value: last.date || '—', sub: last.id ? `${last.time.slice(11)} · ${last.type}` : '尚无运行记录', alert: false },
      { label: '宏观数据', value: s.macro_rows, sub: `${s.macro_indicators} 个指标 · 月度`, alert: false },
      { label: 'ETF 日频', value: s.etf_rows, sub: `${s.etf_codes} 只 ETF · 逐交易日`, alert: false },
      { label: '指数日线', value: s.index_rows || 0, sub: `${s.index_codes || 0} 个指数 · 近一年`, alert: false },
      { label: '今日接口调用', value: s.today_calls, sub: '按接口逐次记录', alert: false },
      { label: '历史失败累计', value: s.failed_total, sub: s.failed_total ? '存在失败，请查看监控看板' : '全部成功', alert: s.failed_total > 0 },
    ];
    $('stats').innerHTML = stats.map(x => `
      <div class="stat ${x.alert ? 'alert' : ''}">
        <div class="stat-label">${x.label}</div>
        <div class="stat-value">${x.value}</div>
        <div class="stat-sub">${x.sub}</div>
      </div>`).join('');
  } catch (e) { console.error(e); }
}

function setButtons(enabled) {
  document.querySelectorAll('button[data-action]').forEach(b => b.disabled = !enabled);
  const prec = $('precRun');
  if (prec) prec.disabled = !enabled;
  ['fullStart', 'fullEnd', 'fullRangeReset'].forEach(id => {
    const el = $(id);
    if (el) el.disabled = !enabled;
  });
}

/* ---------- 调度设置 ---------- */
async function saveScheduler(enabled, time, runType) {
  const payload = { enabled, time, run_type: runType };
  try {
    await api('/api/scheduler', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    await loadStatus();
    flashLog('调度配置已保存', 'INFO');
  } catch (e) { alert('保存失败：' + e.message); }
}
// 采集页按钮已只读，保留监听以防旧 DOM；主入口在数据处理页 dpSchedSave
if ($('schedSave')) {
  $('schedSave').addEventListener('click', () =>
    saveScheduler($('schedEnabled').checked, $('schedTime').value, $('schedType').value));
}
if ($('dpSchedSave')) {
  $('dpSchedSave').addEventListener('click', () =>
    saveScheduler($('dpSchedEnabled').checked, $('dpSchedTime').value, $('dpSchedType').value));
}

/* ---------- 采集 / 重跑 ---------- */
document.querySelectorAll('button[data-action]').forEach(btn => {
  btn.addEventListener('click', async () => {
    const action = btn.dataset.action;
    let url, payload;
    if (action === 'collect') {
      url = '/api/collect';
      const kind = btn.dataset.kind;
      payload = { macro_only: kind === 'macro', etf_only: kind === 'etf', index_only: kind === 'index' };
      if (kind === 'full') {
        payload.start = $('fullStart').value.trim();
        payload.end = $('fullEnd').value.trim();
      }
    } else {
      url = '/api/retry';
      payload = {};
    }
    try {
      const res = await api(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      startJob(res.job_id);
    } catch (e) { alert('任务启动失败：' + e.message); }
  });
});
$('fullRangeReset').addEventListener('click', () => applyDefaultRange('fullStart', 'fullEnd'));

/* ---------- 文件导入（Choice 份额与规模） ---------- */
const MAX_IMPORT_FILES = 10;
$('importFiles').addEventListener('change', () => {
  const n = $('importFiles').files.length;
  const result = $('importResult');
  if (n > MAX_IMPORT_FILES) {
    $('importFiles').value = '';
    result.textContent = `单次最多导入 ${MAX_IMPORT_FILES} 个文件，当前选择了 ${n} 个，请分批导入`;
    result.className = 'import-result err';
  } else {
    result.textContent = n ? `已选择 ${n} 个文件` : '—';
    result.className = 'import-result';
  }
});
$('importBtn').addEventListener('click', async () => {
  const input = $('importFiles');
  const files = input.files;
  const result = $('importResult');
  if (!files || files.length === 0) {
    result.textContent = '请先选择要导入的 .xlsx 文件';
    result.className = 'import-result err';
    return;
  }
  if (files.length > MAX_IMPORT_FILES) {
    result.textContent = `单次最多导入 ${MAX_IMPORT_FILES} 个文件，当前 ${files.length} 个，请分批导入`;
    result.className = 'import-result err';
    return;
  }
  const form = new FormData();
  for (const f of files) form.append('files', f);
  const btn = $('importBtn');
  btn.disabled = true;
  result.textContent = '导入中…';
  result.className = 'import-result';
  try {
    const res = await api('/api/import/etf', { method: 'POST', body: form });
    // 逐文件打印日志，均带文件名称，便于批量导入时定位
    res.imported.forEach(x => flashLog(`文件导入成功 [${x.file}]：标的 ${x.code}，${x.rows} 行（${x.start} ~ ${x.end}）`, 'INFO'));
    res.errors.forEach(e => flashLog(`文件导入失败 [${e.file}]：${e.error}`, 'ERROR'));
    const parts = [`成功 ${res.imported.length} 个文件 / ${res.total_rows} 行`];
    if (res.errors.length) parts.push(`失败 ${res.errors.length} 个：${res.errors.map(e => e.file + '（' + e.error + '）').join('；')}`);
    result.textContent = parts.join(' · ');
    result.className = 'import-result ' + (res.ok ? 'ok' : 'err');
    flashLog(`文件导入汇总：成功 ${res.imported.length} / 失败 ${res.errors.length} / 共 ${res.total_rows} 行`, res.ok ? 'INFO' : 'WARN');
    await Promise.all([loadStatus(), loadData()]);
    input.value = '';
  } catch (e) {
    result.textContent = '导入失败：' + e.message;
    result.className = 'import-result err';
  } finally {
    btn.disabled = false;
  }
});

function startJob(jobId) {
  currentJobId = jobId;
  // 日志统一由全局实时流渲染（含定时调度任务），此处不再清空 logBody，
  // 以满足「保留最近 5000 行」的连续展示。
  setLogMarquee(`任务 ${jobId} 已提交，日志见下方实时流…`, 'INFO');
  $('logDot').classList.add('live');
  setButtons(false);
  $('jobHint').textContent = `任务 ${jobId} 运行中…（含定时调度日志持续刷新）`;
  if (jobPollTimer) clearInterval(jobPollTimer);
  jobPollTimer = setInterval(() => pollJob(jobId), 900);
  pollJob(jobId);
}

async function pollJob(jobId) {
  try {
    const job = await api('/api/jobs/' + jobId);
    // job.logs 不再渲染（已由 /api/live-logs 全局流承接，避免重复）
    if (job.status !== 'running') {
      clearInterval(jobPollTimer);
      jobPollTimer = null;
      $('logDot').classList.remove('live');
      $('jobHint').textContent = `任务 ${jobId} ${job.status === 'success' ? '完成' : '失败'} · ${job.end}`;
      setButtons(true);
      await Promise.all([loadStatus(), loadMonitor(), loadData()]);
    }
  } catch (e) { console.error(e); }
}

// 全局实时日志流：常驻轮询，承接定时调度/后台采集/手动任务等所有线程日志。
async function pollLiveLogs() {
  try {
    const res = await api('/api/live-logs?after=' + liveLogSeq);
    const logs = res.logs || [];
    if (logs.length) {
      const body = $('logBody');
      const empty = body.querySelector('.log-empty');
      if (empty) empty.remove();
      for (const l of logs) {
        body.insertAdjacentHTML('beforeend',
          `<div class="log-line ${esc(l.level)}"><span class="t">[${esc(l.time)}]</span> ${esc(l.message)}</div>`);
      }
      while (body.children.length > LIVE_LOG_LIMIT) body.removeChild(body.firstChild);
      liveLogSeq = res.latest;
      const last = logs[logs.length - 1];
      setLogMarquee(`[${last.time}] ${last.message}`, last.level);
      if (logAutoScroll) scrollLogToBottom(false);
    } else if (res.latest > liveLogSeq) {
      liveLogSeq = res.latest;  // 缓冲滚动导致的纯删除，同步游标
    }
  } catch (e) { console.error(e); }
}

function startLiveLogPolling() {
  pollLiveLogs();
  if (liveLogTimer) clearInterval(liveLogTimer);
  liveLogTimer = setInterval(pollLiveLogs, 1500);
}

function flashLog(msg, level) {
  const time = new Date().toLocaleTimeString('zh-CN', {hour12: false});
  const body = $('logBody');
  const empty = body.querySelector('.log-empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.className = 'log-line ' + level;
  div.textContent = `[${time}] ${msg}`;
  body.appendChild(div);
  while (body.children.length > LIVE_LOG_LIMIT) body.removeChild(body.firstChild);
  body.scrollTop = body.scrollHeight;
  setLogMarquee(`[${time}] ${msg}`, level);
}

// 顶部滚动条：始终显示最新一条日志
function setLogMarquee(text, level = 'INFO') {
  const el = $('logMarquee');
  if (!el) return;
  el.textContent = text;
  el.className = ['ERROR', 'WARNING', 'INFO'].includes(level) ? level : 'INFO';
  el.style.animation = 'none';
  void el.offsetWidth;  // 重启动画
  el.style.animation = '';
}

// 完整日志：只追加新增行，避免每次轮询重置滚动位置
function renderJobLogs(logs) {
  const body = $('logBody');
  if (logs.length < renderedLogCount) {
    body.innerHTML = '';
    renderedLogCount = 0;
  }
  if (renderedLogCount === 0) body.innerHTML = '';
  if (logs.length === renderedLogCount) return;
  for (let i = renderedLogCount; i < logs.length; i++) {
    const l = logs[i];
    body.insertAdjacentHTML('beforeend',
      `<div class="log-line ${esc(l.level)}"><span class="t">[${esc(l.time)}]</span> ${esc(l.message)}</div>`);
  }
  renderedLogCount = logs.length;
  const last = logs[logs.length - 1];
  setLogMarquee(`[${last.time}] ${last.message}`, last.level);
  if (logAutoScroll) scrollLogToBottom(false);
}

function scrollLogToBottom(resume = true) {
  const body = $('logBody');
  if (!body) return;
  body.scrollTop = body.scrollHeight;
  if (resume) {
    logAutoScroll = true;
    $('logAutoScroll').checked = true;
  }
}

$('logToggle').addEventListener('click', () => {
  const panel = $('logPanel');
  const expanded = panel.classList.toggle('expanded');
  $('logToggle').textContent = expanded ? '收起' : '展开';
  if (expanded) scrollLogToBottom();
});
$('logStrip').addEventListener('click', () => $('logToggle').click());
$('logBottom').addEventListener('click', () => scrollLogToBottom());
$('logAutoScroll').addEventListener('change', (e) => {
  logAutoScroll = e.target.checked;
  if (logAutoScroll) scrollLogToBottom(false);
});
$('logBody').addEventListener('scroll', () => {
  const body = $('logBody');
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight <= 20;
  if (!atBottom && logAutoScroll) {
    logAutoScroll = false;
    $('logAutoScroll').checked = false;
  }
});

/* ---------- 精确采集（按接口/指标 + 时间范围） ---------- */
async function loadCatalog() {
  try {
    catalogData = await api('/api/catalog');
    renderPrecItems();
    renderCleanItems();
    const cfg = catalogData.defaults || {};
    if (!$('precStart').value && precCategory === 'macro') $('precStart').placeholder = cfg.macro_start || '起始，留空=默认';
  } catch (e) {
    $('precCode').innerHTML = '<option value="">目录加载失败</option>';
    console.error(e);
  }
}

function renderPrecItems() {
  if (!catalogData) return;
  const items = catalogData[precCategory] || [];
  const sel = $('precCode');
  const isMacro = precCategory === 'macro';
  // 统一使用日期控件；宏观在后端自动按月归并
  $('precStart').type = 'date';
  $('precEnd').type = 'date';
  $('precDateHint').textContent = isMacro
    ? '按日期 · YYYY-MM-DD（宏观自动按月归并）'
    : '按交易日 · YYYY-MM-DD';
  sel.innerHTML = '<option value="">全部（该类别整体）</option>' + items.map(it => {
    const meta = [it.iface, it.unit, it.granularity].filter(Boolean).join(' · ');
    const listing = it.listing_date ? `，上市 ${it.listing_date}` : '';
    const label = `${it.name || it.code}（${it.code}${listing}）`;
    return `<option value="${esc(it.code)}" data-meta="${esc(meta)}" data-last="${esc(it.last_date || '')}" data-listing="${esc(it.listing_date || '')}">${esc(label)}</option>`;
  }).join('');
  $('precItemCount').textContent = `共 ${items.length} 项`;
  updatePrecNote();
}

function updatePrecNote() {
  const sel = $('precCode');
  const opt = sel.options[sel.selectedIndex];
  if (!catalogData) return;
  let note = `数据快照保留最近 ${catalogData.keep_snapshots || 3} 份。`;
  if (opt && opt.dataset.last) note += ` 最新数据截至 ${opt.dataset.last}。`;
  if (opt && opt.dataset.meta) note += `\n接口：${opt.dataset.meta}`;
  if (precCategory === 'etf' && opt && opt.dataset.listing) {
    note += `\n上市日 ${opt.dataset.listing}，早于上市日的范围自动忽略。`;
  }
  $('precNote').textContent = note;
}

// 类别切换（两级联动：类别下拉 → 该类别下的具体口径下拉）
$('precCategory').addEventListener('change', () => {
  precCategory = $('precCategory').value;
  applyDefaultRange('precStart', 'precEnd');   // 时间回到最近一个交易日
  renderPrecItems();                            // 重建口径下拉，默认「全部」
});
$('precCode').addEventListener('change', updatePrecNote);

// 精确采集提交
$('precRun').addEventListener('click', async () => {
  const payload = {
    category: precCategory,
    codes: $('precCode').value ? [$('precCode').value] : [],
    start: $('precStart').value.trim(),
    end: $('precEnd').value.trim(),
  };
  try {
    const res = await api('/api/collect/interface', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    flashLog(`精确采集任务已提交（${precCategory} / ${payload.codes[0] || '全部'}）`, 'INFO');
    startJob(res.job_id);
  } catch (e) { alert('精确采集启动失败：' + e.message); }
});

/* ---------- 数据清理（按口径 / 时间范围，危险操作） ---------- */
let cleanCategory = 'macro';

function renderCleanItems() {
  if (!catalogData) return;
  const items = catalogData[cleanCategory] || [];
  $('cleanCode').innerHTML = '<option value="">全部（该类别整体）</option>' + items.map(it => {
    const label = `${it.name || it.code}（${it.code}）`;
    return `<option value="${esc(it.code)}">${esc(label)}</option>`;
  }).join('');
  $('cleanItemCount').textContent = `共 ${items.length} 项`;
  setCleanResult('—');
}

function cleanPayload() {
  return {
    category: cleanCategory,
    code: $('cleanCode').value.trim(),
    start: $('cleanStart').value.trim(),
    end: $('cleanEnd').value.trim(),
  };
}

function setCleanResult(text, cls = '') {
  const el = $('cleanResult');
  el.textContent = text;
  el.className = 'clean-result' + (cls ? ' ' + cls : '');
}

function scopeText(p) {
  const cat = {macro: '宏观指标', etf: 'ETF 基金', index: '指数'}[p.category] || p.category;
  return `${cat} / ${p.code || '全部口径'} / ${p.start || '不限'} ~ ${p.end || '不限'}`;
}

document.querySelectorAll('#cleanCategory .seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#cleanCategory .seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    cleanCategory = btn.dataset.cat;
    renderCleanItems();
  });
});

$('cleanPreviewBtn').addEventListener('click', async () => {
  const p = cleanPayload();
  try {
    const res = await api('/api/clean/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p),
    });
    const scope = scopeText(p);
    if (!res.matched) {
      setCleanResult(`预览 ${scope}：匹配 0 行，无可清理数据。`, 'warn');
    } else {
      setCleanResult(`预览 ${scope}：将删除 ${res.matched} 行（该类别当前共 ${res.total} 行）。`, 'warn');
    }
  } catch (e) { setCleanResult('预览失败：' + e.message, 'warn'); }
});

$('cleanExecBtn').addEventListener('click', async () => {
  const p = cleanPayload();
  try {
    const pre = await api('/api/clean/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p),
    });
    if (!pre.matched) {
      setCleanResult(`无匹配数据，无需清理：${scopeText(p)}`, 'warn');
      return;
    }
    const ok = window.confirm(
      `即将删除 ${pre.matched} 行数据：\n${scopeText(p)}\n\n` +
      `此操作不可撤销，不影响配置、监控与导出记录。确认继续？`
    );
    if (!ok) { setCleanResult('已取消清理。', 'warn'); return; }
    const res = await api('/api/clean', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...p, confirm: true }),
    });
    setCleanResult(`已清理 ${scopeText(p)}：删除 ${res.deleted} 行，剩余 ${res.after} 行。`, 'done');
    flashLog(`数据清理完成：删除 ${res.deleted} 行（${p.category}/${p.code || '全部'}）`, 'INFO');
    await Promise.all([loadCatalog(), loadStatus(), loadData()]);
  } catch (e) { setCleanResult('清理失败：' + e.message, 'warn'); }
});

$('cleanResetBtn').addEventListener('click', () => {
  $('cleanCode').value = '';
  applyDefaultRange('cleanStart', 'cleanEnd');
  setCleanResult('—');
});

/* ---------- ETF 标的配置 ---------- */
let etfCfg = null;

async function loadEtfConfig() {
  try {
    etfCfg = await api('/api/etf/config');
    renderEtfChips();
    $('etfStart').value = etfCfg.start_date || '';
    $('etfEnd').value = etfCfg.end_date || '';
    $('etfDays').value = etfCfg.default_trade_days || 1;
  } catch (e) {
    $('etfChips').innerHTML = `<div class="empty">配置加载失败：${esc(e.message)}</div>`;
    console.error(e);
  }
}

function etfTagOptions() {
  return (etfCfg && etfCfg.tag_options) ? etfCfg.tag_options : ['宽基', '汇金', '黄金'];
}

function renderEtfChips() {
  const wrap = $('etfChips');
  if (!etfCfg || !etfCfg.items.length) {
    wrap.innerHTML = '<div class="empty">尚未配置 ETF 标的，请在上方输入基金代码添加。</div>';
    return;
  }
  const options = etfTagOptions();
  wrap.innerHTML = etfCfg.items.map(it => {
    const listing = it.listing_date
      ? `<span class="chip-listing">上市 ${esc(it.listing_date)}</span>`
      : '<span class="chip-listing pending">上市日待采集</span>';
    const tags = it.tags || [];
    return `<div class="etf-chip">
      <div class="chip-top">
        <span class="chip-code">${esc(it.code)}</span>
        <button class="chip-remove" data-code="${esc(it.code)}" title="移除该标的">×</button>
      </div>
      <span class="chip-name">${esc(it.name || '—')}</span>
      ${listing}
      <div class="chip-tags">
        ${options.map(t => `<label class="tag-check ${tags.includes(t) ? 'on' : ''}">
          <input type="checkbox" data-tag="${esc(t)}" data-code="${esc(it.code)}" ${tags.includes(t) ? 'checked' : ''}>${esc(t)}
        </label>`).join('')}
      </div>
    </div>`;
  }).join('');
  wrap.querySelectorAll('.chip-remove').forEach(btn => {
    btn.addEventListener('click', () => removeEtfCode(btn.dataset.code));
  });
  wrap.querySelectorAll('.tag-check input').forEach(cb => {
    cb.addEventListener('change', async () => {
      const item = etfCfg.items.find(x => x.code === cb.dataset.code);
      if (!item) return;
      const codes = etfCfg.items.map(x => x.code);
      const checked = Array.from(wrap.querySelectorAll(`.tag-check input[data-code="${cb.dataset.code}"]:checked`))
        .map(x => x.dataset.tag);
      item.tags = etfTagOptions().filter(t => checked.includes(t));
      await saveEtfConfig(true);
      flashLog(`ETF ${item.code} 标签已更新：${item.tags.join('/') || '无'}`, 'INFO');
    });
  });
}

function etfPayload() {
  const tags = {};
  if (etfCfg) {
    etfCfg.items.forEach(x => { if (x.tags && x.tags.length) tags[x.code] = x.tags; });
  }
  return {
    codes: etfCfg ? etfCfg.items.map(x => x.code) : [],
    tags,
    start_date: $('etfStart').value.trim(),
    end_date: $('etfEnd').value.trim(),
    default_trade_days: $('etfDays').value.trim(),
  };
}

async function saveEtfConfig(quiet = false) {
  const payload = etfPayload();
  payload.default_trade_days = payload.default_trade_days ? parseInt(payload.default_trade_days, 10) : 1;
  try {
    const res = await api('/api/etf/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    etfCfg = res.config;
    renderEtfChips();
    await loadCatalog();  // 同步精确采集下拉
    if (!quiet) flashLog('ETF 配置已保存', 'INFO');
    return true;
  } catch (e) {
    if (!quiet) alert('保存失败：' + e.message);
    return false;
  }
}

async function addEtfCode() {
  const input = $('etfNewCode');
  const code = input.value.trim();
  if (!/^\d{1,6}$/.test(code)) { alert('请输入 1~6 位数字基金代码'); return; }
  const full = code.padStart(6, '0');
  if (!etfCfg) etfCfg = { items: [] };
  if (etfCfg.items.some(x => x.code === full)) { alert('该标的已在列表中'); return; }
  etfCfg.items.push({ code: full, name: '', listing_date: '', tags: [] });
  renderEtfChips();
  input.value = '';
  await saveEtfConfig(true);
  flashLog(`已添加 ETF ${full}，上市日将在下次采集时自动解析`, 'INFO');
}

async function removeEtfCode(code) {
  if (!etfCfg) return;
  etfCfg.items = etfCfg.items.filter(x => x.code !== code);
  renderEtfChips();
  await saveEtfConfig(true);
  flashLog(`已移除 ETF ${code}`, 'INFO');
}

$('etfAddBtn').addEventListener('click', addEtfCode);
$('etfNewCode').addEventListener('keydown', (e) => { if (e.key === 'Enter') addEtfCode(); });
$('etfSaveBtn').addEventListener('click', () => saveEtfConfig(false));

/* ---------- 指数标的配置 ---------- */
let indexCfg = null;
let indexCatalog = [];

async function loadIndexConfig() {
  try {
    const [cfg, cat] = await Promise.all([
      api('/api/index/config'),
      Promise.resolve(catalogData ? null : api('/api/catalog')),
    ]);
    indexCfg = cfg;
    if (!catalogData && cat) catalogData = cat;
    indexCatalog = (catalogData && catalogData.index) ? catalogData.index : (cat ? cat.index || [] : []);
    // 下拉列出全部支持的指数，已添加的排除
    const selected = new Set(indexCfg.items.map(x => x.code));
    const available = indexCatalog.filter(it => !selected.has(it.code));
    $('indexNewCode').innerHTML = available.length
      ? available.map(it => `<option value="${esc(it.code)}">${esc(it.name || it.code)}（${esc(it.code)}）</option>`).join('')
      : '<option value="">已添加全部指数</option>';
    renderIndexChips();
    $('indexStart').value = indexCfg.start_date || '';
    $('indexEnd').value = indexCfg.end_date || '';
    $('indexDays').value = indexCfg.lookback_days || 1;
  } catch (e) {
    $('indexChips').innerHTML = `<div class="empty">配置加载失败：${esc(e.message)}</div>`;
    console.error(e);
  }
}

function renderIndexChips() {
  const wrap = $('indexChips');
  if (!indexCfg || !indexCfg.items.length) {
    wrap.innerHTML = '<div class="empty">尚未配置指数标的，请在上方选择并添加。</div>';
    return;
  }
  wrap.innerHTML = indexCfg.items.map(it => `<div class="etf-chip">
    <div class="chip-top">
      <span class="chip-code">${esc(it.code)}</span>
      <button class="chip-remove" data-code="${esc(it.code)}" title="移除该指数">×</button>
    </div>
    <span class="chip-name">${esc(it.name || '—')}</span>
  </div>`).join('');
  wrap.querySelectorAll('.chip-remove').forEach(btn => {
    btn.addEventListener('click', () => removeIndexCode(btn.dataset.code));
  });
}

async function saveIndexConfig() {
  const payload = {
    codes: indexCfg ? indexCfg.items.map(x => x.code) : [],
    start_date: $('indexStart').value.trim(),
    end_date: $('indexEnd').value.trim(),
    lookback_days: $('indexDays').value.trim() ? parseInt($('indexDays').value.trim(), 10) : 1,
  };
  try {
    const res = await api('/api/index/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    indexCfg = res.config;
    renderIndexChips();
    await loadCatalog();
    flashLog('指数配置已保存', 'INFO');
  } catch (e) { alert('保存失败：' + e.message); }
}

async function addIndexCode() {
  const code = $('indexNewCode').value.trim();
  if (!code) { alert('请先选择一个指数'); return; }
  if (!indexCfg) indexCfg = { items: [] };
  if (indexCfg.items.some(x => x.code === code)) { alert('该指数已在列表中'); return; }
  const meta = indexCatalog.find(x => x.code === code);
  indexCfg.items.push({ code, name: meta ? meta.name : '' });
  renderIndexChips();
  await saveIndexConfig();
}

async function removeIndexCode(code) {
  if (!indexCfg) return;
  indexCfg.items = indexCfg.items.filter(x => x.code !== code);
  renderIndexChips();
  await saveIndexConfig();
  flashLog(`已移除指数 ${code}`, 'INFO');
}

$('indexAddBtn').addEventListener('click', addIndexCode);
$('indexSaveBtn').addEventListener('click', () => saveIndexConfig());

/* ---------- 监控看板 ---------- */
async function loadMonitor() {
  try {
    monitorData = await api('/api/monitor/report');
    const fails = monitorData['失败明细'] || [];
    const count = fails.length && fails[0]['提示'] ? 0 : fails.length;
    $('failCount').textContent = count;
    $('failCount').className = 'badge ' + (count ? '' : 'zero');
    buildMonitorOptions();
    renderMonitor();
  } catch (e) { console.error(e); }
}

// 用「接口健康度」的接口名填充关键词 datalist（去重、无日期后缀，可选可输入）
function buildMonitorOptions() {
  const names = new Set();
  (monitorData?.['接口健康度'] || []).forEach(r => {
    const n = r['接口名称'] || r['接口键'];
    if (n) names.add(String(n));
  });
  $('monKeywordOptions').innerHTML = [...names].sort().map(n => `<option value="${esc(n)}"></option>`).join('');
}

document.querySelectorAll('#monitorTabs .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#monitorTabs .tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    monitorTab = tab.dataset.tab;
    monitorPage = 1;  // 切换 Tab 重置到第一页
    const hint = $('monRetryHint');
    if (hint) hint.hidden = monitorTab !== 'failures';
    renderMonitor();
  });
});
$('monFilterBtn').addEventListener('click', () => { monitorPage = 1; renderMonitor(); });
$('monResetBtn').addEventListener('click', () => {
  $('monKeyword').value = '';
  $('monRunType').value = ''; $('monCategory').value = ''; $('monStatus').value = '';
  monitorDefaultRange();
  monitorPage = 1;
  renderMonitor();
});
$('monPageSize').addEventListener('change', () => { monitorPage = 1; renderMonitor(); });

/* ---------- 接口监控清理（按口径，需确认） ---------- */
function mcPayload() {
  return {
    start: $('mcStart').value.trim(), end: $('mcEnd').value.trim(),
    status: $('mcStatus').value, run_type: $('mcRunType').value,
    category: $('mcCategory').value, keyword: $('mcKeyword').value.trim(),
  };
}
function mcScopeText(p) {
  const label = [p.start || '不限', p.end || '不限'].join('~');
  return `${label} / ${p.status || '全部状态'} / ${p.run_type || '全部类型'} / ${p.category || '全部类别'}${p.keyword ? ' / ' + p.keyword : ''}`;
}
function setMcResult(text, cls = '') {
  const el = $('mcResult');
  el.textContent = text;
  el.className = 'clean-result' + (cls ? ' ' + cls : '');
}
$('monitorCleanToggle').addEventListener('click', () => {
  const box = $('monitorCleanBox');
  box.hidden = !box.hidden;
  if (!box.hidden) applyDefaultRange('mcStart', 'mcEnd');
});
$('mcPreviewBtn').addEventListener('click', async () => {
  const p = mcPayload();
  try {
    const res = await api('/api/monitor/clean/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p),
    });
    setMcResult(res.matched
      ? `预览 ${mcScopeText(p)}：将删除 ${res.matched} 条（监控记录共 ${res.total} 条）。`
      : `预览 ${mcScopeText(p)}：匹配 0 条，无可清理记录。`, 'warn');
  } catch (e) { setMcResult('预览失败：' + e.message, 'warn'); }
});
$('mcExecBtn').addEventListener('click', async () => {
  const p = mcPayload();
  try {
    const pre = await api('/api/monitor/clean/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p),
    });
    if (!pre.matched) { setMcResult('无匹配记录，无需清理。', 'warn'); return; }
    const ok = window.confirm(
      `即将删除 ${pre.matched} 条接口监控记录：\n${mcScopeText(p)}\n\n` +
      `此操作不可撤销，不影响业务数据、ETF/指数配置与导出文件。确认继续？`
    );
    if (!ok) { setMcResult('已取消清理。', 'warn'); return; }
    const res = await api('/api/monitor/clean', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...p, confirm: true }),
    });
    setMcResult(`已清理：删除 ${res.deleted} 条，剩余 ${res.after} 条。`, 'done');
    flashLog(`接口监控清理完成：删除 ${res.deleted} 条`, 'INFO');
    await Promise.all([loadMonitor(), loadStatus()]);
  } catch (e) { setMcResult('清理失败：' + e.message, 'warn'); }
});
$('mcResetBtn').addEventListener('click', () => {
  $('mcStart').value = ''; $('mcEnd').value = '';
  $('mcStatus').value = ''; $('mcRunType').value = '';
  $('mcCategory').value = ''; $('mcKeyword').value = '';
  setMcResult('—');
});

// 监控看板默认日期：当天（监控看的是运行记录，与行情交易日无关）
function monitorDefaultRange() {
  $('monStart').value = todayText();
  $('monEnd').value = todayText();
}

// 从一行里按字段名取值（各 sheet 字段名不同，做兼容映射）
function monField(row, candidates) {
  for (const c of candidates) if (row[c] != null && row[c] !== '') return String(row[c]);
  return '';
}

function filterMonitorRows(rows) {
  const kw = $('monKeyword').value.trim().toLowerCase();
  const runType = $('monRunType').value;
  const category = $('monCategory').value;
  const status = $('monStatus').value;
  const start = $('monStart').value;
  const end = $('monEnd').value;
  return rows.filter(r => {
    // 关键词：匹配整行任意单元格
    if (kw && !Object.values(r).some(v => String(v ?? '').toLowerCase().includes(kw))) return false;
    // 运行类型（各 sheet 字段名不同）
    if (runType && monField(r, ['运行类型']) && monField(r, ['运行类型']) !== runType) return false;
    // 类别（采集量统计用「类别口径」）
    if (category && monField(r, ['类别', '类别口径']) !== category) return false;
    // 状态：成功/失败；失败明细无状态列，按"失败原因"存在视为失败
    if (status) {
      const st = monField(r, ['状态']);
      if (st) {
        if (st !== status) return false;
      } else if (sheetIsFailureView()) {
        if (status !== 'failed') return false;
      }
    }
    // 日期范围：取行内第一个日期字段
    const date = monField(r, ['运行日期', '失败日期', '最近失败日期', '运行时间']).slice(0, 10);
    if (start && date && date < start) return false;
    if (end && date && date > end) return false;
    return true;
  });
}

function sheetIsFailureView() { return monitorTab === 'failures'; }

function renderMonitor() {
  const map = { overview: '运行总览', failures: '失败明细', health: '接口健康度', detail: '调用明细', stats: '采集量统计' };
  let rows = (monitorData && monitorData[map[monitorTab]]) || [];
  const hasNotice = !!(rows.length && rows[0]['提示']);
  if (!hasNotice) rows = filterMonitorRows(rows);

  const el = $('monitorTable');
  const filterActive = ['monKeyword','monStart','monEnd'].some(id => $(id).value.trim()) ||
    !!$('monRunType').value || !!$('monCategory').value || !!$('monStatus').value;
  monitorPageSize = Math.min(Math.max(parseInt($('monPageSize')?.value, 10) || 25, 1), MAX_PAGE_SIZE);
  monitorTotalRows = rows.length;
  monitorPages = Math.max(1, Math.ceil(monitorTotalRows / monitorPageSize));
  if (monitorPage > monitorPages) monitorPage = monitorPages;
  const start = (monitorPage - 1) * monitorPageSize;
  const pageRows = hasNotice ? rows : rows.slice(start, start + monitorPageSize);

  const prefix = filterActive ? '筛选后' : '共';
  $('monFilterHint').textContent = `${prefix} ${monitorTotalRows} 行 · 第 ${monitorPage}/${monitorPages} 页`;
  if (!pageRows.length) {
    el.innerHTML = `<div class="empty">${rows[0]?.['提示'] || '无匹配记录'}</div>`;
    renderMonitorPager();
    return;
  }
  // 失败明细：额外加「操作」列，每条失败可单独重跑自己的任务
  const showRetry = sheetIsFailureView() && !hasNotice;
  // 运行总览：额外加「展开」列，点开可看该批次每次接口调用的明细
  const isOverview = monitorTab === 'overview' && !hasNotice;
  const cols = Object.keys(pageRows[0]);
  const headCols = isOverview ? ['', ...cols] : (showRetry ? [...cols, '操作'] : cols);
  const head = '<thead><tr>' + headCols.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead>';
  const body = '<tbody>' + pageRows.map(r => {
    const isFail = (r['状态'] === 'failed') || (r['失败原因'] && !r['重跑结果']);
    const cls = isFail ? 'row-fail' : '';
    let cells = cols.map(c => {
      let v = r[c] == null ? '' : String(r[c]);
      let cell = `<div class="cell-clip" title="${esc(v)}">${esc(v)}</div>`;
      if (c === '状态') {
        const ok = v === 'success';
        cell = `<span class="tag ${ok ? 'success' : 'failed'}">${ok ? '成功' : '失败'}</span>`;
      }
      if (c === '重跑结果' && v) {
        cell = `<span class="tag ${v.startsWith('已') ? 'success' : 'failed'}">${esc(v)}</span>`;
      }
      return `<td>${cell}</td>`;
    }).join('');
    if (showRetry) {
      const tid = esc(r['任务ID'] || '');
      const done = String(r['重跑结果'] || '').startsWith('已重跑成功');
      const label = done ? '再次重跑' : '重跑此条';
      cells += `<td><button class="btn tiny retry-task" data-task-id="${tid}" title="仅重跑这一条失败任务">${label}</button></td>`;
    }
    if (isOverview) {
      const runId = esc(r['运行ID'] || '');
      cells = `<td><button class="btn tiny ov-expand" data-run-id="${runId}" title="展开本次运行的接口调用明细">▸</button></td>` + cells;
      return `<tr class="${cls}">${cells}</tr>` +
             `<tr class="ov-detail-row" hidden><td colspan="${headCols.length}"></td></tr>`;
    }
    return `<tr class="${cls}">` + cells + '</tr>';
  }).join('') + '</tbody>';
  el.innerHTML = '<table>' + head + body + '</table>';
  if (isOverview) {
    el.querySelectorAll('button.ov-expand').forEach(btn => {
      btn.addEventListener('click', () => {
        const detailRow = btn.closest('tr').nextElementSibling;
        if (detailRow.hidden) {
          const runId = String(btn.dataset.runId || '');
          const details = (monitorData['调用明细'] || []).filter(
            d => String(d['运行ID'] || '') === runId);
          detailRow.querySelector('td').innerHTML = renderOverviewDetail(runId, details);
          detailRow.hidden = false;
          btn.textContent = '▾';
        } else {
          detailRow.hidden = true;
          btn.textContent = '▸';
        }
      });
    });
  }
  if (showRetry) {
    el.querySelectorAll('button.retry-task').forEach(btn => {
      btn.addEventListener('click', () => retryFailureTask(btn.dataset.taskId, btn));
    });
  }
  renderMonitorPager();
}

// 运行总览展开：展示该批次每次接口调用的明细
function renderOverviewDetail(runId, details) {
  if (!details || !details.length) {
    return `<div class="ov-detail-empty">运行 ${esc(runId)} 无接口调用明细</div>`;
  }
  const cols = ['运行时间', '接口名称', '类别', '状态', '数据行数', '耗时(秒)', '失败原因'];
  const head = `<div class="ov-detail-title">运行 ${esc(runId)} · 接口调用明细（${details.length} 条）</div>` +
    '<table class="ov-detail-table"><thead><tr>' + cols.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead><tbody>';
  const body = details.map(d => {
    return '<tr>' + cols.map(c => {
      const v = d[c] == null ? '' : String(d[c]);
      if (c === '状态') {
        const ok = v === 'success';
        return `<td><span class="tag ${ok ? 'success' : 'failed'}">${ok ? '成功' : '失败'}</span></td>`;
      }
      if (c === '失败原因' && v) {
        return `<td><div class="cell-clip" title="${esc(v)}">${esc(v)}</div></td>`;
      }
      return `<td>${esc(v)}</td>`;
    }).join('') + '</tr>';
  }).join('');
  return head + body + '</tbody></table>';
}

// 监控失败明细：按任务ID重跑单条失败任务
async function retryFailureTask(taskId, btn) {
  if (!taskId) { alert('该记录缺少任务ID，无法单独重跑'); return; }
  if (!window.confirm('确认只重跑这一条失败任务？')) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '提交中…';
  try {
    const res = await api('/api/retry/task', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId }),
    });
    startJob(res.job_id);
  } catch (e) {
    alert('单条重跑启动失败：' + e.message);
    btn.disabled = false;
    btn.textContent = old;
  }
}

function renderMonitorPager() {
  const el = $('monitorPager');
  if (!monitorTotalRows || monitorPages <= 1) { el.innerHTML = ''; return; }
  el.innerHTML = pagerButtons(monitorPage, monitorPages, monitorTotalRows);
  bindPager(el, monitorPages, (p) => { monitorPage = p; renderMonitor(); });
}

/* ---------- 数据浏览 ---------- */
// 数据预览日期统一为完整日期控件；后端按月指标自动截取到 YYYY-MM
function applyDateControl() {
  $('filterStart').type = 'date';
  $('filterEnd').type = 'date';
}

function fillCodeOptions() {
  if (!catalogData) return;
  const items = catalogData[dataTab] || [];
  $('filterCodeOptions').innerHTML = items.map(it => {
    const label = it.name ? `${it.code} - ${it.name}` : it.code;
    return `<option value="${esc(it.code)}" label="${esc(label)}"></option>`;
  }).join('');
}

document.querySelectorAll('#dataTabs .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#dataTabs .tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    dataTab = tab.dataset.tab;
    $('dataFilters').style.display = dataTab === 'files' ? 'none' : 'flex';
    $('dataPager').style.display = dataTab === 'files' ? 'none' : 'flex';
    if (dataTab !== 'files') {
      applyDateControl();
      fillCodeOptions();
      resetToDefaultRange();  // 切换类别重置为默认区间
    }
    dataPage = 1;
    loadData();
  });
});

// 采集数据默认区间：近 1 个月（快捷按钮近1月/近1年/近5年）
const DATA_DEFAULT_MONTHS = 1;
function applyRecentRange(months, btn = null) {
  $('filterStart').value = monthsAgoText(months);
  $('filterEnd').value = todayText();
  document.querySelectorAll('#dataRangeBtns button').forEach(b => {
    b.classList.toggle('active', b === btn ||
      (!btn && Number(b.dataset.months) === months));
  });
  dataRangeMonths = months;
}
let dataRangeMonths = DATA_DEFAULT_MONTHS;

function resetToDefaultRange() {
  applyRecentRange(DATA_DEFAULT_MONTHS);
  $('filterCode').value = '';
}

$('filterBtn').addEventListener('click', () => { dataPage = 1; loadData(); });
$('filterResetBtn').addEventListener('click', () => { resetToDefaultRange(); dataPage = 1; loadData(); });
$('filterPageSize').addEventListener('change', () => { dataPage = 1; loadData(); });
document.querySelectorAll('#dataRangeBtns button').forEach(b => {
  b.addEventListener('click', () => {
    applyRecentRange(Number(b.dataset.months), b);
    dataPage = 1;
    loadData();
  });
});

async function loadData() {
  const el = $('dataTable');
  if (dataTab === 'files') { $('dataPager').innerHTML = ''; return loadFiles(el); }
  const code = $('filterCode').value.trim();
  const start = $('filterStart').value.trim();
  const end = $('filterEnd').value.trim();
  const pageSize = Math.min(Math.max(parseInt($('filterPageSize').value, 10) || 25, 1), MAX_PAGE_SIZE);
  const url = `/api/data/${dataTab}?code=${encodeURIComponent(code)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&page=${dataPage}&page_size=${pageSize}&order=${dataDateOrder}`;
  try {
    const res = await api(url);
    dataPages = res.pages || 1;
    dataTotalRows = res.total || 0;
    if (dataPage > dataPages) { dataPage = dataPages; return loadData(); }
    $('dataTotal').textContent = `共 ${res.total} 行 · 第 ${res.page}/${res.pages} 页`;
    renderTable(el, res.rows, false, { dateCol: dataTab === 'macro' ? '日期' : '交易日期', dateOrder: dataDateOrder, onSortDate: toggleDateOrder });
    renderPager();
  } catch (e) { el.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

// 通用分页控件：监控看板与采集数据预览共用，默认 25 行/页、单页上限 100 行
function pagerButtons(page, pages, total) {
  const btn = (label, target, opts = {}) => {
    const dis = opts.disabled ? 'disabled' : '';
    const act = opts.active ? 'active' : '';
    return `<button ${dis} class="${act}" data-page="${target}">${label}</button>`;
  };
  let html = btn('«', 1, { disabled: page === 1 });
  html += btn('‹', page - 1, { disabled: page === 1 });
  const from = Math.max(1, page - 2);
  const to = Math.min(pages, page + 2);
  for (let p = from; p <= to; p++) html += btn(String(p), p, { active: p === page });
  html += btn('›', page + 1, { disabled: page === pages });
  html += btn('»', pages, { disabled: page === pages });
  html += `<span class="page-info">共 ${pages} 页 / ${total} 行</span>`;
  return html;
}

function bindPager(el, pages, onPage) {
  el.querySelectorAll('button[data-page]').forEach(b => {
    b.addEventListener('click', () => {
      const p = parseInt(b.dataset.page, 10);
      if (p >= 1 && p <= pages) onPage(p);
    });
  });
}

function renderPager() {
  const el = $('dataPager');
  if (dataPages <= 1) { el.innerHTML = ''; return; }
  el.innerHTML = pagerButtons(dataPage, dataPages, dataTotalRows);
  bindPager(el, dataPages, (p) => {
    if (p !== dataPage) { dataPage = p; loadData(); }
  });
}

async function loadFiles(el) {
  $('dataTotal').textContent = '';
  try {
    const res = await api('/api/files');
    const rows = res.files.map(f => ({
      ...f, 下载: `<a class="link" href="/api/download?name=${encodeURIComponent(f.name)}">下载</a>`
    }));
    renderTable(el, rows, true);
  } catch (e) { el.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

function renderTable(el, rows, allowHtml = false, opts = null) {
  if (!rows.length) { el.innerHTML = '<div class="empty">无数据</div>'; return; }
  const cols = Object.keys(rows[0]);
  const sortable = opts && opts.dateCol && cols.includes(opts.dateCol);
  const head = '<thead><tr>' + cols.map(c => {
    if (sortable && c === opts.dateCol) {
      const arrow = opts.dateOrder === 'asc' ? '▲' : '▼';
      const tip = opts.dateOrder === 'asc' ? '点击改为降序' : '点击改为升序';
      return `<th><button type="button" class="th-sort" data-sort-date title="${tip}">${esc(c)} <span class="sort-arrow">${arrow}</span></button></th>`;
    }
    return `<th>${esc(c)}</th>`;
  }).join('') + '</tr></thead>';
  const body = '<tbody>' + rows.map(r => '<tr>' + cols.map(c => {
    const v = r[c] == null ? '' : String(r[c]);
    const html = allowHtml && c === '下载' ? v : esc(v);
    return `<td title="${esc(v)}"><div class="cell-clip">${html}</div></td>`;
  }).join('') + '</tr>').join('') + '</tbody>';
  el.innerHTML = '<table>' + head + body + '</table>';
  if (sortable) {
    const btn = el.querySelector('[data-sort-date]');
    if (btn) btn.addEventListener('click', () => opts.onSortDate && opts.onSortDate());
  }
}

function toggleDateOrder() {
  dataDateOrder = dataDateOrder === 'asc' ? 'desc' : 'asc';
  dataPage = 1;
  loadData();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[m]);
}

/* ---------- SQLite 分类导出 ---------- */
function downloadFile(name) {
  if (!name) return;
  const a = document.createElement('a');
  a.href = '/api/download?name=' + encodeURIComponent(name);
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function runExport(url, label) {
  try {
    const res = await api(url, { method: 'POST' });
    const files = res.files || [];
    flashLog(`${label}完成，生成 ${files.length} 个文件：${files.join('、')}`, 'INFO');
    // 后端最后一个文件始终是 ZIP 包，点击按钮后直接触发浏览器下载
    downloadFile(files[files.length - 1]);
    if (dataTab === 'files') loadData();
  } catch (e) {
    alert(`${label}失败：${e.message}`);
  }
}

$('exportMacroBtn').addEventListener('click', () => runExport('/api/export/macro', '宏观数据导出'));
$('exportEtfBtn').addEventListener('click', () => runExport('/api/export/etf', 'ETF 数据导出'));
$('exportIndexBtn').addEventListener('click', () => runExport('/api/export/index', '指数数据导出'));
$('exportFullBtn').addEventListener('click', () => runExport('/api/export/full', '全量导出'));
$('exportMonitorBtn').addEventListener('click', () => runExport('/api/export/monitor', '接口监控导出'));

/* ---------- 数据处理模块 ---------- */
function derivedTag(code) {
  if (['RETAIL_SALES_YOY', 'CPI_YOY', 'PPI_YOY', 'M1_YOY', 'M2_YOY'].includes(code)) return '直接取数';
  if (code === 'TRADE_YOY') return '官方累计';
  if (code === 'SFR_STOCK_YOY') return '央行官方';
  if (code.includes('NEW') || code === 'GOLD_NEW_TON') return '差分';
  return '派生';
}

function switchModule(name) {
  document.querySelectorAll('.module-tab').forEach(b => b.classList.toggle('active', b.dataset.module === name));
  $('collectModule').hidden = name !== 'collect';
  $('derivedModule').hidden = name !== 'derived';
  $('dashboardModule').hidden = name !== 'dashboard';
  if (name === 'derived') { loadDerived(); loadDerivedScheduler(); }
  if (name === 'dashboard') {
    loadDashboard().then(() => setTimeout(resizeDashboardCharts, 60));
  }
}

async function loadDerived() {
  try {
    const month = $('derivedMonth').value || '';
    const res = await api('/api/derived' + (month ? '?month=' + encodeURIComponent(month) : ''));
    $('derivedMonth').value = res.month || '';
    $('derivedMonthList').innerHTML = (res.months || []).map(m => `<option value="${esc(m)}"></option>`).join('');
    $('derivedMeta').textContent = `${res.month || '—'} · ${res.count} 项指标 · 来源 macro_observation`;
    const groups = {};
    (res.rows || []).forEach(r => { (groups[r.grp] = groups[r.grp] || []).push(r); });
    const keys = Object.keys(groups).sort();
    if (!keys.length) {
      $('derivedGroups').innerHTML = '<div class="empty">该月暂无派生指标，请先执行定时调度或在采集完成后手动重算</div>';
      return;
    }
    $('derivedGroups').innerHTML = keys.map(g => `
      <div class="derived-group">
        <h3>${esc(g)}</h3>
        <div class="derived-grid">
          ${groups[g].map(r => {
            const v = (r.value === null || r.value === undefined) ? '—'
              : Number(r.value).toLocaleString('zh-CN', { maximumFractionDigits: 4 });
            return `<div class="derived-item" title="${esc(r.formula || '')}">
              <span class="d-tag">${esc(derivedTag(r.code))}</span>
              <div class="d-name">${esc(r.name)}</div>
              <div class="d-value">${v}<span class="d-unit">${esc(r.unit || '')}</span></div>
              <div class="d-formula">${esc(r.formula || '')}</div>
            </div>`;
          }).join('')}
        </div>
      </div>`).join('');
  } catch (e) {
    $('derivedGroups').innerHTML = `<div class="empty">派生指标加载失败：${esc(e.message)}</div>`;
    flashLog(`派生指标加载失败：${e.message}`, 'ERROR');
  }
}

document.querySelectorAll('.module-tab').forEach(btn =>
  btn.addEventListener('click', () => switchModule(btn.dataset.module)));
$('derivedMonth').addEventListener('change', loadDerived);

// 派生指标代码与名称（与后端 INDICATORS 保持一致，用于「按指标重算」下拉）
const DERIVED_CODES = [
  ['TRADE_YOY', '进出口同比'], ['RETAIL_SALES_YOY', '社零同比'], ['CPI_YOY', 'CPI同比'],
  ['PPI_YOY', 'PPI同比'], ['CPI_PPI_GAP', 'CPI-PPI剪刀差'], ['SFR_STOCK_YOY', '社融存量同比'],
  ['M1_YOY', 'M1同比增速'], ['M2_YOY', 'M2同比增速'], ['M1_M2_GAP', 'M1-M2剪刀差'],
  ['A_SHARE_MC_M2', 'A股总市值/M2'], ['HH_DEPOSIT_NEW', '居民存款新增'],
  ['HH_DEPOSIT_NEW_YOY', '居民存款同比多增'], ['RMB_LOAN_NEW', '人民币贷款当月新增'],
  ['GOLD_NEW_TON', '黄金当月新增'],
];
(function initDerivedCodes() {
  const sel = $('dpRecalcCode');
  if (sel) sel.innerHTML = DERIVED_CODES.map(([c, n]) => `<option value="${c}">${n} (${c})</option>`).join('');
})();

async function recomputeDerived(payload, label) {
  try {
    const res = await api('/api/derived/recompute', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    flashLog(`派生指标${label}重算完成：${res.rows} 行`, 'INFO');
    if (payload.month) $('derivedMonth').value = payload.month;
    await loadDerived();
  } catch (e) {
    alert(`派生指标${label}重算失败：${e.message}`);
  }
}

$('derivedRecomputeBtn').addEventListener('click', () => recomputeDerived({}, '全量'));
$('dpRecalcMonthBtn').addEventListener('click', () => {
  const month = $('dpRecalcMonth').value;
  if (!month) { alert('请先选择要重算的月份'); return; }
  recomputeDerived({ month }, `月份 ${month}`);
});
$('dpRecalcCodeBtn').addEventListener('click', () => {
  const code = $('dpRecalcCode').value;
  if (!code) { alert('请先选择要重算的指标'); return; }
  recomputeDerived({ code }, `指标 ${code}`);
});
$('derivedExportBtn').addEventListener('click', () => {
  const month = $('derivedMonth').value || '';
  window.location.href = '/api/derived/export' + (month ? '?month=' + encodeURIComponent(month) : '');
});

async function loadDerivedScheduler() {
  try {
    const res = await api('/api/scheduler');
    const badge = $('derivedSchedBadge');
    badge.textContent = res.enabled ? '已启用' : '未启用';
    badge.className = 'pill ' + (res.enabled ? 'ok' : 'ghost');
    const runTypeMap = { full: '全量', macro: '仅宏观', etf: '仅ETF', index: '仅指数' };
    $('derivedSchedGrid').textContent =
      `${res.enabled ? '每日 ' + (res.time || '08:30') : '未启用'} · ${runTypeMap[res.run_type] || res.run_type || '全量'} · 下次 ${res.next_run || '—'}`;
  } catch (e) {
    $('derivedSchedGrid').innerHTML = `<div class="empty">调度配置加载失败：${esc(e.message)}</div>`;
  }
}

/* ---------- 数据看板模块 ---------- */
const dashboardInstances = {};  // containerId -> [{ id, inst }]
let dashboardTab = 'macro';
let dashboardData = null;
const etfDetailState = { code: '', page: 1, start: '' };  // 当前展开的 ETF 每日明细
const DASH_PALETTE = ['#3b5bdb', '#16855b', '#c2410c', '#7048b6', '#0b7285', '#b5235b'];

function disposeDashboardCharts(containerId) {
  (dashboardInstances[containerId] || []).forEach(x => {
    try { x.inst.dispose(); } catch (_) {}
  });
  dashboardInstances[containerId] = [];
}

function resizeDashboardCharts() {
  Object.values(dashboardInstances).forEach(list =>
    list.forEach(x => { try { x.inst.resize(); } catch (_) {} }));
}

function dashboardChartOption(chart) {
  // etf_rank 布局：X 轴为 ETF 简称，顺序由后端按净流入降序给出，不能再排序
  const isRank = chart.layout === 'etf_rank';
  const rawCategories = [...new Set((chart.series || []).flatMap(s => (s.data || []).map(p => p[0])))];
  const categories = isRank ? rawCategories : rawCategories.sort();
  const axisCount = (chart.y_axes || []).length || 1;
  const hasBar = (chart.series || []).some(s => s.type === 'bar');
  return {
    animation: false,
    grid: isRank
      ? { left: 62, right: 24, top: 30, bottom: 82 }
      : { left: 62, right: 34 + (axisCount - 1) * 62, top: 54, bottom: 62 },
    legend: isRank ? { show: false } : { top: 6, type: 'scroll', textStyle: { fontSize: 11 } },
    tooltip: {
      trigger: 'axis', axisPointer: { type: isRank ? 'shadow' : 'cross' }, confine: true,
      valueFormatter: isRank ? (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + ' 亿元') : undefined,
    },
    xAxis: {
      type: 'category', data: categories, boundaryGap: hasBar,
      axisLabel: {
        fontSize: isRank ? 10 : 10, hideOverlap: !isRank,
        rotate: isRank ? 42 : (categories.length > 36 ? 30 : 0),
        interval: isRank ? 0 : 'auto',
      },
    },
    yAxis: (chart.y_axes || [{ name: '', position: 'left' }]).map((a, i) => ({
      type: 'value', name: a.name || '', position: a.position || 'left',
      offset: a.offset || 0,
      nameTextStyle: { fontSize: 10, padding: [0, 0, 0, a.offset ? 32 : 0] },
      axisLabel: { fontSize: 10 },
      splitLine: { show: i === 0 },
    })),
    dataZoom: [
      { type: 'inside' },
      { type: 'slider', height: 14, bottom: 8, brushSelect: false },
    ],
    series: (chart.series || []).map((s, idx) => {
      const lookup = new Map(s.data || []);
      const values = categories.map(c => lookup.has(c) ? lookup.get(c) : null);
      const base = {
        name: s.name, type: s.type || 'line', yAxisIndex: s.y_axis_index || 0,
        data: values,
        smooth: s.type !== 'bar',
        // 稀疏序列（如季度GDP）点少且被空值隔开，需显示圆点+连线才能看见
        showSymbol: s.type !== 'bar' && (s.data || []).length <= 24,
        symbolSize: 6, connectNulls: s.type !== 'bar',
        lineStyle: { width: 2 }, barMaxWidth: 20, barGap: '20%',
        itemStyle: { color: DASH_PALETTE[idx % DASH_PALETTE.length] },
      };
      if (isRank) {
        // 净流入：正为流入(红)、负为流出(绿)，并显示数值标签
        base.barMaxWidth = 26;
        base.itemStyle = { color: (p) => (p.value >= 0 ? '#c0392b' : '#16855b') };
        base.label = {
          show: true, position: 'top', fontSize: 9,
          formatter: (p) => (p.value == null ? '' : Number(p.value).toFixed(1)),
        };
      }
      return base;
    }),
  };
}

function renderDashboardCharts(containerId, charts) {
  const el = $(containerId);
  disposeDashboardCharts(containerId);
  etfDetailState.code = '';  // 重绘后旧明细面板已销毁
  if (!charts || !charts.length) {
    el.innerHTML = '<div class="empty">暂无图表数据</div>';
    return;
  }
  el.innerHTML = charts.map(c => {
    // 汇金净流入图：独立合计数字卡片（合计流通规模变化 / 份额变化）
    const totalCard = (c.layout === 'etf_rank' && c.total_cap_change != null) ? `
      <div class="etf-total-cards">
        <div class="etf-total-card">
          <div class="etc-k">合计流通规模变化</div>
          <div class="etc-v ${c.total_cap_change >= 0 ? 'up' : 'down'}">${c.total_cap_change >= 0 ? '+' : ''}${Number(c.total_cap_change).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} <small>亿元</small></div>
        </div>
        <div class="etf-total-card">
          <div class="etc-k">合计流通份额变化</div>
          <div class="etc-v ${c.total_share_change >= 0 ? 'up' : 'down'}">${c.total_share_change >= 0 ? '+' : ''}${Number(c.total_share_change).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} <small>亿份</small></div>
        </div>
        <div class="etf-total-card">
          <div class="etc-k">统计范围</div>
          <div class="etc-v neutral">${c.etf_count} 只 · ${esc(c.range_label || '')}</div>
        </div>
      </div>` : '';
    const cardClass = 'chart-card' + (c.group === 'huijin_detail' ? ' chart-card-wide' : '');
    return `
    <div class="${cardClass}">
      <div class="chart-head">
        <h4>${esc(c.title)}</h4>
        <span>${esc(c.note || '')}</span>
      </div>
      ${totalCard}
      <div class="chart-box ${c.layout === 'etf_rank' ? 'chart-box-tall' : ''}" data-chart-id="${esc(c.id)}"></div>
      ${c.layout === 'etf_rank' ? `<div class="etf-daily-detail" data-detail-for="${esc(c.id)}" hidden></div>` : ''}
    </div>`;
  }).join('');
  charts.forEach(c => {
    const box = el.querySelector(`[data-chart-id="${c.id}"]`);
    if (!box) return;
    if (!window.echarts) {
      box.innerHTML = '<div class="empty">图表库未加载</div>';
      return;
    }
    const inst = echarts.init(box);
    inst.setOption(dashboardChartOption(c));
    dashboardInstances[containerId].push({ id: c.id, inst });
    if (c.layout === 'etf_rank') {
      box.style.cursor = 'pointer';
      box.title = '点击任意 ETF 柱子，展开该 ETF 每日明细';
      inst.on('click', (p) => {
        if (p.componentType !== 'series' || p.dataIndex == null) return;
        const meta = (c.rank_meta || [])[p.dataIndex];
        if (!meta || !meta.code) return;
        const net = ((c.series || [])[0]?.data || [])[p.dataIndex]?.[1];
        openEtfDailyDetail(c, meta, net);
      });
    }
  });
}

/* 点击汇金细分图的柱子：展开/收起该 ETF 在所选区间内的每日明细 */
async function openEtfDailyDetail(chart, meta, netValue) {
  const panel = document.querySelector(`[data-detail-for="${chart.id}"]`);
  if (!panel) return;
  if (!panel.hidden && etfDetailState.code === meta.code) {
    panel.hidden = true;
    etfDetailState.code = '';
    return;
  }
  etfDetailState.code = meta.code;
  etfDetailState.page = 1;
  etfDetailState.start = chart.detail_start || '';
  panel.hidden = false;
  panel.innerHTML = '<div class="empty">加载每日明细…</div>';
  await renderEtfDetailPage(panel, chart, meta, netValue);
}

async function renderEtfDetailPage(panel, chart, meta, netValue) {
  const qs = new URLSearchParams({
    code: meta.code, page: String(etfDetailState.page), page_size: '100', order: 'desc',
  });
  if (etfDetailState.start) qs.set('start', etfDetailState.start);
  try {
    const res = await api('/api/data/etf?' + qs.toString());
    if (etfDetailState.code !== meta.code) return;  // 已切到其他 ETF，丢弃过期响应
    const rows = res.rows || [];
    const fmt = (v, digits = 2) => (v == null || v === '') ? '—'
      : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: digits });
    const signed = (v, unit) => {
      const n = Number(v || 0);
      return `<span class="${n >= 0 ? 'up' : 'down'}">${n >= 0 ? '+' : ''}${fmt(n)} <small>${unit}</small></span>`;
    };
    const body = rows.map(r => `<tr>
      <td>${esc(r['交易日期'])}</td>
      <td class="num">${fmt(r['份额(亿份)'])}</td>
      <td class="num">${signed(r['份额变化(亿份)'], '亿份')}</td>
      <td class="num">${fmt(r['份额变化率(%)'])}%</td>
      <td class="num">${fmt(r['规模(亿元)'])}</td>
      <td class="num">${signed(r['规模变化(亿元)'], '亿元')}</td>
      <td class="num">${fmt(r['规模变化率(%)'])}%</td>
    </tr>`).join('');
    const pages = res.pages || 1;
    const pager = `<div class="pager">
      <button type="button" data-detail-page="prev" ${etfDetailState.page <= 1 ? 'disabled' : ''}>上一页</button>
      <span class="page-info">第 ${etfDetailState.page} / ${pages} 页 · 共 ${res.total} 条</span>
      <button type="button" data-detail-page="next" ${etfDetailState.page >= pages ? 'disabled' : ''}>下一页</button>
    </div>`;
    panel.innerHTML = `
      <div class="etf-detail-head">
        <div><b>${esc(meta.name)}</b> <span class="muted-inline">${esc(meta.code)}</span>
          <span class="muted-inline">区间净流入（规模变化累计）：${Number(netValue) >= 0 ? '+' : ''}${fmt(netValue)} 亿元</span></div>
        <button type="button" class="btn tiny" data-detail-close>收起明细</button>
      </div>
      <div class="table-wrap tall"><table>
        <thead><tr><th>交易日期</th><th>份额(亿份)</th><th>份额变化(亿份)</th><th>变化率</th><th>规模(亿元)</th><th>规模变化(亿元)</th><th>变化率</th></tr></thead>
        <tbody>${body || '<tr><td colspan="7" class="empty">该区间无数据</td></tr>'}</tbody>
      </table></div>
      ${pager}`;
    panel.querySelector('[data-detail-close]').addEventListener('click', () => {
      panel.hidden = true;
      etfDetailState.code = '';
    });
    panel.querySelectorAll('[data-detail-page]').forEach(b => b.addEventListener('click', () => {
      etfDetailState.page += (b.dataset.detailPage === 'next' ? 1 : -1);
      renderEtfDetailPage(panel, chart, meta, netValue);
    }));
  } catch (e) {
    panel.innerHTML = `<div class="empty">明细加载失败：${esc(e.message || e)}</div>`;
  }
}

async function loadDashboard() {
  try {
    const months = $('dashboardMonths').value || '12';
    const etfMonths = $('dashboardEtfMonths').value || '12';
    const res = await api(`/api/dashboard?months=${encodeURIComponent(months)}&etf_months=${encodeURIComponent(etfMonths)}`);
    const macro = (res.macro && res.macro.charts) || [];
    const etf = (res.etf && res.etf.charts) || [];
    dashboardData = res;
    // 只渲染当前 Tab：ECharts 在 hidden 容器中取不到宽度，切换时再渲染另一组
    renderDashboardTab();
    const etfNote = etf.filter(c => c.group !== 'huijin_detail')
      .map(c => `${c.id === 'etf_gold' ? '黄金' : '汇金'} ${c.etf_count}只`).join(' · ');
    $('dashboardMeta').textContent = `${res.generated_at || '—'} · 宏观 ${macro.length} 图 · ETF ${etf.length} 图${etfNote ? '（' + etfNote + '）' : ''}`;
  } catch (e) {
    $('dashboardMeta').textContent = `看板加载失败：${e.message}`;
    flashLog(`数据看板加载失败：${e.message}`, 'ERROR');
  }
}

function renderDashboardTab() {
  if (!dashboardData) return;
  if (dashboardTab === 'macro') {
    renderDashboardCharts('macroCharts', (dashboardData.macro && dashboardData.macro.charts) || []);
  } else {
    renderDashboardCharts('etfCharts', (dashboardData.etf && dashboardData.etf.charts) || []);
  }
  setTimeout(resizeDashboardCharts, 40);
}

document.querySelectorAll('#dashboardTabs .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#dashboardTabs .tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    dashboardTab = tab.dataset.tab;
    $('dashboardMacroPanel').hidden = dashboardTab !== 'macro';
    $('dashboardEtfPanel').hidden = dashboardTab !== 'etf';
    renderDashboardTab();
  });
});

window.addEventListener('resize', resizeDashboardCharts);
$('dashboardMonths').addEventListener('change', loadDashboard);
$('dashboardEtfMonths').addEventListener('change', loadDashboard);
$('dashboardRefreshBtn').addEventListener('click', () => loadDashboard().then(resizeDashboardCharts));

/* ---------- 定时刷新 ---------- */
async function refresh() {
  await Promise.all([loadStatus(), loadMonitor()]);
}

async function init() {
  applyDateControl();
  // 先取 status 注入「最近一个已结束交易日」，保证日期默认值取最近交易日而非当天
  await loadStatus();
  // 并行加载目录/配置，再填充下拉并套用默认日期区间
  await Promise.all([loadCatalog(), loadEtfConfig(), loadIndexConfig()]);
  fillCodeOptions();
  applyDefaultRange('fullStart', 'fullEnd');
  applyDefaultRange('precStart', 'precEnd');
  applyDefaultRange('cleanStart', 'cleanEnd');
  resetToDefaultRange();
  monitorDefaultRange();
  await loadData();
  await loadDerived();
  await refresh();
  startLiveLogPolling();  // 常驻实时日志流（含定时调度），保留最近 5000 行
}
init();
setInterval(refresh, 30000);
