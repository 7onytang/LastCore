'use strict';
const $ = id => document.getElementById(id);
let admin = '', loading = false, generation = 0, connected = false;
const desktop = document.documentElement.dataset.surface === 'desktop';
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
  if (response.status === 401) { const error = new Error('凭据无效或已失效，请重新连接。'); error.code = 'unauthorized'; throw error; }
  if (!response.ok) throw new Error(`查询失败（HTTP ${response.status}），请稍后重试。`);
  return response.json();
}
function clearData() {
  for (const id of ['node-count','online-count','active-count','worker-count']) $(id).textContent = '—';
  $('node-list').replaceChildren(text('p','连接后查看设备、可用内存与节点状态。','empty'));
  const row = document.createElement('tr'), cell = text('td','尚未获取任务。','empty'); cell.colSpan = 6; row.append(cell); $('task-list').replaceChildren(row);
  $('updated').textContent = '尚未获取数据'; $('worker-note').textContent = '等待查询可用 worker';
}
function accessControls() {
  $('connect-title').textContent = connected ? 'Controller' : '连接 Controller';
  $('credential-entry').hidden = connected;
  $('connected-actions').hidden = !connected;
  $('connect-submit').disabled = loading;
  $('admin-token').disabled = loading;
  $('refresh').disabled = loading || !connected;
  $('connect-submit').firstChild.textContent = loading ? '连接中… ' : '连接 ';
}
function disconnect() {
  admin = ''; connected = false; generation++;
  $('admin-token').value = ''; clearData(); accessControls();
  $('connection').textContent = '尚未连接'; $('connection').className = 'badge';
  message('已断开。页面凭据和数据已清除。');
}
async function refresh() {
  if (loading || !admin) return;
  loading = true; accessControls(); const ticket = generation, token = admin;
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
    if (!tasks.length) { const r = document.createElement('tr'), c = text('td','暂无运算任务。','empty'); c.colSpan = 6; r.append(c); rows.append(r); }
    for (const t of tasks) { const r = document.createElement('tr'), state = document.createElement('td'); state.append(badge(t.status)); const model = t.request?.model_id; const kind = model === 'lastcore/linear-demo-v1' ? '线性运算' : model === 'lastcore/tiny-cpu' ? 'tiny 前向' : '模型运算'; const saved = t.result === null || t.result === undefined ? '未返回' : typeof t.result === 'string' ? t.result : JSON.stringify(t.result); r.append(text('td',t.id || t.task_id || '—'),text('td',model ? `${kind} · ${model}` : '未声明'),state,text('td',Array.isArray(t.assignments) ? t.assignments.length : '—'),text('td',saved),text('td',date(t.created_at))); rows.append(r); }
    $('updated').textContent = `更新于 ${date(Date.now()/1000)}（北京）`;
    connected = true; $('connection').textContent = '已连接 · 只读'; $('connection').className = 'badge good';
    message('真实数据已更新。手动刷新可获取最新状态。');
  } catch (error) {
    if (ticket === generation) {
      if (error.code === 'unauthorized') { admin = ''; connected = false; clearData(); }
      message(error instanceof TypeError || error.name === 'TimeoutError' ? '连接超时或网络不可达，请重试。' : error.message, true);
      $('connection').textContent = connected ? '数据可能过期' : '连接失败'; $('connection').className = 'badge bad';
    }
  } finally { loading = false; accessControls(); }
}
if (!desktop) {
$('connect-form').addEventListener('submit', event => {
  event.preventDefault(); if (loading || connected) return;
  if (location.protocol !== 'https:' && !['127.0.0.1','localhost','[::1]'].includes(location.hostname)) {
    message('请通过 HTTPS 打开 Controller，再输入管理员凭据。', true); return;
  }
  admin = $('admin-token').value.trim(); $('admin-token').value = '';
  generation++; clearData(); refresh();
});
$('refresh').addEventListener('click',refresh); $('disconnect').addEventListener('click',disconnect);
}
window.addEventListener('pagehide', () => { if (!desktop) disconnect(); });
for (const a of document.querySelectorAll('nav a')) a.addEventListener('click', () => { for (const b of document.querySelectorAll('nav a')) b.removeAttribute('aria-current'); a.setAttribute('aria-current','page'); });
async function localRequest(path, method = 'GET') {
  const response = await fetch(path,{method,cache:'no-store',headers:{'X-LastCore-Session':localKey},signal:AbortSignal.timeout(12000)});
  if (!response.ok) throw new Error('本地操作未完成，请检查启动面板的终端。');
  return response.json();
}
let localLoading = false;
const taskStates = {'preparing':'准备中','running':'执行中','completed':'本机完成','failed':'执行失败',
  'stopping':'正在停止','stopped-local':'本机已停止','stop-unconfirmed':'停止未确认'};
const ownErrors = {'shutdown':'用户停止本机执行','deadline':'任务超时','heartbeat-stall':'心跳中断',
  'task-terminal':'Controller 任务已结束','local-cancel':'本机取消','execution_failed':'本机执行失败',
  'executor_unavailable':'执行器不可用','cleanup_timeout':'清理尚未确认'};
const ownConnection = {'connected':'心跳已确认','unreachable':'连接不可达','heartbeat-failed':'心跳失败',
  'heartbeat-stale':'心跳已过期','starting':'正在连接','stopped':'进程未运行','rejected':'凭据被拒绝','unavailable':'Unavailable'};
function renderOwnState(state) {
  const observation = state.observation || {}, task = observation.task, device = state.device || {};
  $('own-node-id').textContent = observation.node_id || '—';
  $('own-connection').textContent = ownConnection[observation.connection] || 'Unavailable';
  $('own-heartbeat').textContent = observation.last_heartbeat ? date(observation.last_heartbeat) : '—';
  $('own-cpu').textContent = Number.isFinite(device.cpu_percent) ? `${device.cpu_percent}% · ${device.cpu_logical ?? '—'} 逻辑核心` : 'Unavailable · 尚无有效采样';
  const gib = bytes => (bytes / 1073741824).toFixed(1) + ' GiB';
  $('own-memory').textContent = Number.isFinite(device.memory_available_bytes) && Number.isFinite(device.memory_total_bytes) ? `${gib(device.memory_available_bytes)} / ${gib(device.memory_total_bytes)}` : 'Unavailable';
  $('own-accelerator').textContent = state.mode === 'stage' ? 'CPU 执行 · GPU/NPU 信息 Unavailable' : 'Demo 无模型计算 · GPU/NPU 信息 Unavailable';
  $('own-sampled').textContent = device.sampled_at ? `本机 OS 采样 ${date(device.sampled_at)}（北京）` : '资源指标 Unavailable';
  $('own-task-id').textContent = task?.task_id || '—';
  $('own-task-type').textContent = task ? `${task.executor === 'stage' ? 'tiny 分层前向' : 'Demo 模拟'} · ${task.model_id || '—'}` : '—';
  $('own-task-stage').textContent = task && Number.isInteger(task.layer_start) && Number.isInteger(task.layer_end) ? `层 [${task.layer_start}, ${task.layer_end})` : '—';
  const stale = task && !state.running && ['preparing','running','stopping'].includes(task.status);
  $('task-status').textContent = stale ? '最后状态未确认' : task ? taskStates[task.status] || '状态未确认' : observation.poll_status === 'ok' ? '等待任务' : '尚未获取';
  $('task-status').className = task && task.status === 'completed' ? 'badge good' : 'badge';
  const result = task?.result;
  $('own-task-result').textContent = result?.output_shape ? `输出形状 ${result.output_shape.join(' × ')}${Array.isArray(result.output_preview) && result.output_preview.length ? ` · 数值摘要 [${result.output_preview.map(n => Number(n).toPrecision(5)).join(', ')}]` : ''}${Number.isFinite(result.local_seconds) ? ` · 本机计算 ${result.local_seconds.toFixed(4)}s` : ''}` : task?.error_code ? `状态原因：${ownErrors[task.error_code] || 'Unavailable'}` : task?.executor === 'demo' && task.status === 'completed' ? '模拟执行已结束，未产生模型结果' : task ? '暂无模型结果' : '尚未执行任务';
  const report = {'acknowledged':'本机终态已由 Controller 接收','pending':'本机结果已保存，等待重新上报',
    'terminal-conflict':'Controller 已返回终态冲突，不能据此证明结果已接收','not-sent':'只确认本机执行状态；云端任务由 Controller 收敛','running-reported':'本机已上报开始执行'};
  $('own-task-report').textContent = stale ? '进程已退出，此任务的清理与云端结果尚未确认。' : task ? report[task.report_status] || '云端接收状态未确认' : '只展示本机分配与执行状态，不查询其他节点。';
}
async function localState(action) {
  if (localLoading) return; localLoading = true;
  $('start-agent').disabled = true; $('stop-agent').disabled = true; $('refresh-local').disabled = true;
  if (action) { $('process-status').textContent = action === 'start' ? '正在启动…' : '正在停止…'; }
  try {
    if (!localKey) throw new Error('本地会话已失效，请从启动终端重新打开原始面板链接。');
    const state = await localRequest(action ? `/desktop/${action}` : '/desktop/state', action ? 'POST' : 'GET');
    renderOwnState(state);
    $('process-status').textContent = state.stopping ? '正在停止 · 等待确认' : state.running ? '进程运行中' : state.exit_code !== null && state.exit_code !== 0 ? '进程已退出' : '已停止';
    $('process-status').className = state.running && !state.stopping ? 'badge good' : 'badge';
    const mode = state.mode === 'demo' ? 'Demo 模拟' : 'tiny 分层节点';
    $('desktop-result').textContent = `${mode} · ${state.stopping ? '停止已请求，原进程退出后才可重新启动。' : state.running ? '本机任务与连接状态显示在上方。' : state.exit_code === null ? '点击启动节点。' : state.exit_code === 0 ? '已正常停止，恢复状态已保留。' : `退出码 ${state.exit_code}，检查私有配置后重试。`}`;
    $('start-agent').hidden = state.running; $('stop-agent').hidden = !state.running;
    $('start-agent').textContent = state.exit_code !== null && state.exit_code !== 0 ? '重试启动' : '连接并启动';
    $('stop-agent').textContent = state.stopping ? '正在停止…' : '停止本机执行';
    $('start-agent').disabled = state.running; $('stop-agent').disabled = !state.running || state.stopping;
    $('refresh-local').hidden = true;
  } catch (error) {
    $('desktop-result').textContent = error.message; $('process-status').textContent = '状态未确认';
    $('own-connection').textContent = '状态未确认';
    $('own-sampled').textContent = '查询失败，先前资源指标可能已过期';
    // A failed request can have taken effect: re-read state before allowing another action.
    $('refresh-local').hidden = !localKey; $('refresh-local').disabled = false;
  } finally { localLoading = false; }
}
if (desktop) {
  $('refresh-local').addEventListener('click',() => localState());
  $('start-agent').addEventListener('click',() => localState('start'));
  $('stop-agent').addEventListener('click',() => localState('stop'));
  localState();
  setInterval(() => {if (!document.hidden) localState();},5000);
}
