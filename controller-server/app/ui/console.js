'use strict';
const $ = id => document.getElementById(id);
let admin = '', loading = false, generation = 0;
const desktop = new URLSearchParams(location.search).has('desktop');
const localKey = desktop ? location.hash.slice(1) : '';
if (desktop) history.replaceState(null, '', location.pathname + location.search);
const date = value => {
  const d = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(d.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(d);
};
const statuses = {available:'可接单',online:'在线',idle:'空闲',busy:'忙碌',offline:'离线',draining:'暂停接单',queued:'排队中',running:'运行中',completed:'已完成',failed:'失败',cancelled:'已取消',succeeded:'成功'};
function badge(status) { const el = document.createElement('span'); el.className = 'badge'; el.textContent = statuses[status] || status || '未知'; if (['online','available','idle','completed','succeeded'].includes(status)) el.classList.add('good'); if (status === 'failed') el.classList.add('bad'); return el; }
function text(tag, value, cls) { const el = document.createElement(tag); el.textContent = value; if (cls) el.className = cls; return el; }
function message(value, error = false) { $('feedback').textContent = value; $('feedback').classList.toggle('error', error); }
async function request(path, token = admin, options = {}) {
  const response = await fetch(path, {...options, cache:'no-store', credentials:'omit', redirect:'error', headers:{...options.headers, ...(token ? {Authorization:`Bearer ${token}`} : {})}, signal:AbortSignal.timeout(12000)});
  if (response.status === 401) throw new Error('凭据无效或已失效，请重新连接。');
  if (!response.ok) throw new Error(`查询失败（HTTP ${response.status}），请稍后重试。`);
  return response.json();
}
function clearData() {
  for (const id of ['node-count','online-count','active-count','worker-count']) $(id).textContent = '—';
  $('node-list').replaceChildren(text('p','连接后查看设备、可用内存与节点状态。','empty'));
  const row = document.createElement('tr'), cell = text('td','尚未获取任务。','empty'); cell.colSpan = 4; row.append(cell); $('task-list').replaceChildren(row);
  $('updated').textContent = '尚未获取数据'; $('worker-note').textContent = '等待查询可用 worker';
}
function disconnect() { admin = ''; generation++; $('admin-token').value = ''; $('disconnect').hidden = true; $('refresh').disabled = true; clearData(); $('connection').textContent = '尚未连接'; $('connection').className = 'badge'; message('已断开，页面凭据和数据已清除。'); }
async function refresh() {
  if (loading || !admin) return;
  loading = true; $('refresh').disabled = true; const ticket = generation, token = admin;
  message('正在查询 Controller…');
  try {
    const [nodesData, tasksData, workersResponse] = await Promise.all([
      request('/v1/nodes',token),request('/v1/tasks',token),
      fetch('/v1/inference/workers',{headers:{Authorization:`Bearer ${token}`},cache:'no-store',credentials:'omit',redirect:'error',signal:AbortSignal.timeout(12000)})
    ]);
    const workers = workersResponse.status === 404 ? null : workersResponse.ok ? (await workersResponse.json()).workers : undefined;
    if (ticket !== generation) return;
    const nodes = nodesData.nodes, tasks = tasksData.tasks;
    if (!Array.isArray(nodes) || !Array.isArray(tasks) || (workers !== null && !Array.isArray(workers))) throw new Error('Controller 返回了无法读取的数据，请检查服务状态。');
    $('node-count').textContent = nodes.length; $('online-count').textContent = nodes.filter(n => ['available','busy','draining'].includes(n.status)).length;
    $('active-count').textContent = tasks.filter(t => ['queued','running'].includes(t.status)).length;
    $('worker-count').textContent = workers === null ? '未启用' : workers.length;
    $('worker-note').textContent = workers === null ? '整模型 API 当前不可用' : '已声明整模型能力的 worker';
    const list = $('node-list'); list.replaceChildren();
    if (!nodes.length) list.append(text('p','还没有注册节点。通过下方终端入口连接第一台设备。','empty'));
    for (const n of nodes) {
      const card = text('article','','node'), head = text('div','','node-head');
      head.append(text('h3',n.name || '未命名节点'),badge(n.status)); card.append(head,text('p',n.platform || '未知平台'),text('p',`${n.available_memory_mb ?? '—'} MiB 可用内存 · 权重 ${n.compute_weight ?? '—'}`),text('small',`最近心跳 ${date(n.last_seen)}（北京）`)); list.append(card);
    }
    const rows = $('task-list'); rows.replaceChildren();
    if (!tasks.length) { const r = document.createElement('tr'), c = text('td','暂无 tiny 任务。','empty'); c.colSpan = 4; r.append(c); rows.append(r); }
    for (const t of tasks) { const r = document.createElement('tr'), state = document.createElement('td'); state.append(badge(t.status)); r.append(text('td',t.id || t.task_id || '—'),state,text('td',Array.isArray(t.assignments) ? t.assignments.length : '—'),text('td',date(t.created_at))); rows.append(r); }
    $('updated').textContent = `更新于 ${date(Date.now()/1000)}（北京）`;
    $('connection').textContent = '已连接 · 只读'; $('connection').className = 'badge good';
    message('真实数据已更新。手动刷新可获取最新状态。');
  } catch (error) { if (ticket === generation) { message(error instanceof TypeError || error.name === 'TimeoutError' ? '连接超时或网络不可达，请检查 Controller。' : error.message, true); $('connection').textContent = '查询失败 · 数据可能过期'; $('connection').className = 'badge bad'; } }
  finally { loading = false; $('refresh').disabled = !admin; }
}
$('connect-form').addEventListener('submit', event => { event.preventDefault(); if (loading) return; if (location.protocol !== 'https:' && !['127.0.0.1','localhost','[::1]'].includes(location.hostname)) { message('请通过 HTTPS 打开 Controller，再输入管理员凭据。', true); return; } admin = $('admin-token').value.trim(); $('admin-token').value = ''; generation++; clearData(); $('disconnect').hidden = false; refresh(); });
$('refresh').addEventListener('click',refresh); $('disconnect').addEventListener('click',disconnect);
window.addEventListener('pagehide', () => {admin = ''; generation++;});
for (const a of document.querySelectorAll('nav a')) a.addEventListener('click', () => { for (const b of document.querySelectorAll('nav a')) b.removeAttribute('aria-current'); a.setAttribute('aria-current','page'); });
async function localRequest(path, method = 'GET') {
  const response = await fetch(path,{method,cache:'no-store',headers:{'X-LastCore-Session':localKey},signal:AbortSignal.timeout(12000)});
  if (!response.ok) throw new Error('本地操作未完成，请检查启动面板的终端。');
  return response.json();
}
let localLoading = false;
async function localState(action) {
  if (localLoading) return; localLoading = true;
  $('start-agent').disabled = true; $('stop-agent').disabled = true;
  try { const state = await localRequest(action ? `/desktop/${action}` : '/desktop/state',action ? 'POST' : 'GET'); $('process-status').textContent = state.running ? '进程运行中' : '进程已停止'; $('process-status').className = state.running ? 'badge good' : 'badge'; $('desktop-result').textContent = `${state.mode === 'demo' ? 'Demo 模拟' : 'tiny 分层节点'} · ${state.running ? '请在 Controller 查看实际注册、心跳与任务状态。' : state.exit_code === null ? '等待启动。' : `退出码 ${state.exit_code}；恢复信息保留在私有状态目录。`}`; $('start-agent').disabled = state.running; $('stop-agent').disabled = !state.running; }
  catch (error) { $('desktop-result').textContent = error.message; $('process-status').textContent = '状态未知'; $('start-agent').disabled = false; }
  finally { localLoading = false; }
}
if (desktop) {
  $('desktop-panel').hidden = false;
  for (const selector of ['.connect-card','#feedback','.metrics-title','.metrics','#nodes','#tasks','#api-docs','nav a[href="#nodes"]','nav a[href="#tasks"]']) document.querySelector(selector).hidden = true;
  $('connection').textContent = '本地节点面板';
  $('start-agent').addEventListener('click',() => localState('start')); $('stop-agent').addEventListener('click',() => localState('stop')); localState();
  // Poll only the local process while the page is visible; never infer cloud health.
  setInterval(() => {if (!document.hidden) localState();},5000);
}
