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

/* ---------- 概览 ---------- */
async function loadStatus() {
  try {
    const s = await api('/api/status');
    $('sysTime').textContent = s.server_time;
    const sched = s.scheduler;
    const sb = $('schedBadge');
    sb.textContent = sched.enabled ? `定时 ${sched.time} 已启用` : '定时未启用';
    sb.className = 'pill ' + (sched.enabled ? 'ok' : 'ghost');
    $('schedEnabled').checked = sched.enabled;
    $('schedTime').value = sched.time;
    $('schedType').value = sched.run_type;
    $('schedNext').textContent = '下次运行：' + sched.next_run;
    const rb = $('runBadge');
    rb.textContent = s.running ? '采集中…' : '空闲';
    rb.className = 'pill ' + (s.running ? 'warn' : 'ghost');
    setButtons(!s.running);

    const last = s.last_run;
    const stats = [
      { label: '最近采集', value: last.date || '—', sub: last.id ? `${last.time.slice(11)} · ${last.type}` : '尚无运行记录', alert: false },
      { label: '宏观数据', value: s.macro_rows, sub: `${s.macro_indicators} 个指标 · 月度`, alert: false },
      { label: 'ETF 日频', value: s.etf_rows, sub: `${s.etf_codes} 只 ETF · 逐交易日`, alert: false },
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
}

/* ---------- 调度设置 ---------- */
$('schedSave').addEventListener('click', async () => {
  const payload = {
    enabled: $('schedEnabled').checked,
    time: $('schedTime').value,
    run_type: $('schedType').value,
  };
  try {
    await api('/api/scheduler', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    await loadStatus();
    flashLog('调度配置已保存', 'INFO');
  } catch (e) { alert('保存失败：' + e.message); }
});

/* ---------- 采集 / 重跑 ---------- */
document.querySelectorAll('button[data-action]').forEach(btn => {
  btn.addEventListener('click', async () => {
    const action = btn.dataset.action;
    let url, payload;
    if (action === 'collect') {
      url = '/api/collect';
      const kind = btn.dataset.kind;
      payload = { macro_only: kind === 'macro', etf_only: kind === 'etf' };
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

function startJob(jobId) {
  currentJobId = jobId;
  $('logBody').innerHTML = '';
  $('logDot').classList.add('live');
  setButtons(false);
  $('jobHint').textContent = `任务 ${jobId} 运行中…`;
  if (jobPollTimer) clearInterval(jobPollTimer);
  jobPollTimer = setInterval(() => pollJob(jobId), 900);
  pollJob(jobId);
}

async function pollJob(jobId) {
  try {
    const job = await api('/api/jobs/' + jobId);
    const body = $('logBody');
    body.innerHTML = job.logs.map(l => `
      <div class="log-line ${l.level}"><span class="t">[${l.time}]</span> ${esc(l.message)}</div>
    `).join('') || '<div class="log-empty">等待日志…</div>';
    body.scrollTop = body.scrollHeight;
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

function flashLog(msg, level) {
  const body = $('logBody');
  const div = document.createElement('div');
  div.className = 'log-line ' + level;
  div.textContent = `[${new Date().toLocaleTimeString('zh-CN', {hour12:false})}] ${msg}`;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

/* ---------- 监控看板 ---------- */
async function loadMonitor() {
  try {
    monitorData = await api('/api/monitor/report');
    const fails = monitorData['失败明细'] || [];
    const count = fails.length && fails[0]['提示'] ? 0 : fails.length;
    $('failCount').textContent = count;
    $('failCount').className = 'badge ' + (count ? '' : 'zero');
    renderMonitor();
  } catch (e) { console.error(e); }
}

document.querySelectorAll('#monitorTabs .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#monitorTabs .tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    monitorTab = tab.dataset.tab;
    renderMonitor();
  });
});

function renderMonitor() {
  const map = { overview: '运行总览', failures: '失败明细', health: '接口健康度', detail: '调用明细' };
  const rows = (monitorData && monitorData[map[monitorTab]]) || [];
  const el = $('monitorTable');
  if (!rows.length || (rows[0]['提示'])) {
    el.innerHTML = `<div class="empty">${rows[0]?.['提示'] || '暂无数据'}</div>`;
    return;
  }
  const cols = Object.keys(rows[0]);
  const head = '<thead><tr>' + cols.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead>';
  const body = '<tbody>' + rows.map(r => {
    const isFail = (r['状态'] === 'failed') || (r['失败原因'] && !r['重跑结果']);
    const cls = isFail ? 'row-fail' : '';
    return `<tr class="${cls}">` + cols.map(c => {
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
    }).join('') + '</tr>';
  }).join('') + '</tbody>';
  el.innerHTML = '<table>' + head + body + '</table>';
}

/* ---------- 数据浏览 ---------- */
document.querySelectorAll('#dataTabs .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#dataTabs .tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    dataTab = tab.dataset.tab;
    $('dataFilters').style.display = dataTab === 'files' ? 'none' : 'flex';
    loadData();
  });
});
$('filterBtn').addEventListener('click', loadData);

async function loadData() {
  const el = $('dataTable');
  if (dataTab === 'files') return loadFiles(el);
  const code = $('filterCode').value.trim();
  const start = $('filterStart').value.trim();
  const end = $('filterEnd').value.trim();
  const url = `/api/data/${dataTab}?code=${encodeURIComponent(code)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
  try {
    const res = await api(url);
    $('dataTotal').textContent = `共 ${res.total} 行（最多显示 5000 行）`;
    renderTable(el, res.rows);
  } catch (e) { el.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
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

function renderTable(el, rows, allowHtml = false) {
  if (!rows.length) { el.innerHTML = '<div class="empty">无数据</div>'; return; }
  const cols = Object.keys(rows[0]);
  const head = '<thead><tr>' + cols.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead>';
  const body = '<tbody>' + rows.map(r => '<tr>' + cols.map(c => {
    const v = r[c] == null ? '' : String(r[c]);
    const html = allowHtml && c === '下载' ? v : esc(v);
    return `<td title="${esc(v)}"><div class="cell-clip">${html}</div></td>`;
  }).join('') + '</tr>').join('') + '</tbody>';
  el.innerHTML = '<table>' + head + body + '</table>';
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[m]);
}

/* ---------- 定时刷新 ---------- */
async function refresh() {
  await Promise.all([loadStatus(), loadMonitor()]);
}
refresh();
loadData();
setInterval(refresh, 30000);
