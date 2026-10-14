/* Admin-only workload view. No storage, framework, API dispatch retry or edge authority. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else { root.LastCoreWorkloads = api; api.mount(root.document, {fetch:root.fetch.bind(root), location:root.location}); }
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  class UiError extends Error { constructor(code, message) { super(message); this.code=code; } }
  const allowedOrigin = location => location.protocol === 'https:' ||
    (location.protocol === 'http:' && ['localhost','127.0.0.1','[::1]','::1'].includes(location.hostname));
  const beijingFormat = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  function beijing(epoch) { const date=new Date(epoch*1000);return Number.isFinite(date.getTime()) ? beijingFormat.format(date)+'（北京时间）' : '时间未返回'; }
  function createTransport({fetch,location,onUnauthorized=()=>{},timeoutMs=12000,setTimer=setTimeout,clearTimer=clearTimeout}) {
    let token='',generation=0;const pending=new Set();
    function clear() { generation++;token='';for(const abort of pending)abort.abort();pending.clear(); }
    function replace(value) { clear();if(!allowedOrigin(location))throw new UiError('unsafe-origin','请通过 HTTPS 或本机 loopback 打开本页；不要输入凭据。');token=String(value).trim(); }
    const current = id => id===generation;
    async function request(path,{method='GET',body}={}) {
      if(!allowedOrigin(location))throw new UiError('unsafe-origin','公开 HTTP 页面不能发送管理员凭据，请改用 HTTPS。');
      if(!/^\/v1\/(nodes|tasks(?:\/[A-Za-z0-9_-]{1,128})?)$/.test(path))throw new UiError('path','只允许当前 Controller 的已知接口。');
      if(!token)throw new UiError('credential','请先连接管理员凭据。');
      const ticket=generation,secret=token,controller=new AbortController();pending.add(controller);
      let expired=false,listener;
      const aborted=new Promise((_,reject)=>{listener=()=>reject(new UiError(current(ticket)&&expired?'timeout':'stale',current(ticket)&&expired?'请求超时，结果尚未确认。':'旧页面请求已停止。'));controller.signal.addEventListener('abort',listener,{once:true});});
      const timer=setTimer(()=>{expired=true;controller.abort();},timeoutMs);
      try {
        const response=await Promise.race([fetch(path,{method,credentials:'omit',mode:'same-origin',redirect:'error',cache:'no-store',signal:controller.signal,
          headers:{Authorization:`Bearer ${secret}`,...(body!==undefined?{'Content-Type':'application/json'}:{})},...(body!==undefined?{body:JSON.stringify(body)}:{})}),aborted]);
        if(!current(ticket))throw new UiError('stale','旧凭据响应已忽略。');
        if(response.status===401){clear();onUnauthorized();throw new UiError('unauthorized','凭据无效或已失效，请重新连接。');}
        if(response.redirected)throw new UiError('network','接口重定向未接受，结果尚未确认。');
        if(!response.ok)throw new UiError(response.status>=500?'server':response.status===409?'conflict':'rejected',response.status===409?'当前可用节点或任务状态已变化，请刷新后核对。':`请求被拒绝（HTTP ${response.status}），请核对权限和输入。`);
        const value=response.status===204?null:await Promise.race([response.json(),aborted]);
        if(!current(ticket))throw new UiError('stale','旧凭据响应已忽略。');
        return value;
      } catch(error) {
        if(!current(ticket)&&error.code!=='unauthorized')throw new UiError('stale','旧页面请求已停止。');
        if(error instanceof UiError)throw error;
        if(expired)throw new UiError('timeout','请求超时，结果尚未确认。');
        throw new UiError('network','网络或响应异常，结果尚未确认。');
      } finally {clearTimer(timer);controller.signal.removeEventListener('abort',listener);pending.delete(controller);}
    }
    return {request,replace,clear,current,get generation(){return generation;},get authenticated(){return Boolean(token);}};
  }
  const MODEL={pi:'lastcore/pi-monte-carlo-v1',log_stats:'lastcore/log-statistics-v1'};
  const eligibleNodes=(nodes,workload)=>nodes.filter(node=>node&&typeof node==='object'&&node.status==='available'&&Array.isArray(node.supported_workloads)&&node.supported_workloads.includes(workload)&&Number.isSafeInteger(node.available_memory_mb)&&node.available_memory_mb>=1);
  function buildRequest(workload,{samples,workers,logs},nodes) {
    if(!['pi','log_stats'].includes(workload))throw new UiError('input','请选择已支持的运算类型。');
    if(!Number.isSafeInteger(workers)||workers<1||workers>8)throw new UiError('input','设备上限须为1–8的整数。');
    const pool=eligibleNodes(nodes,workload);if(!pool.length)throw new UiError('capability','没有声明支持此运算的可用节点，不会派发。');
    const maxNodes=Math.min(workers,pool.length);
    // Scheduler can fall back to its largest-capacity candidates within this limit.
    const capacity=pool.map(n=>n.available_memory_mb).sort((a,b)=>b-a).slice(0,maxNodes).reduce((a,b)=>a+b,0);
    const base={model_id:MODEL[workload],workload,layer_memory_mb:1,max_nodes:maxNodes};
    if(workload==='pi'){
      if(!Number.isSafeInteger(samples)||samples<1000||samples>100000000)throw new UiError('input','采样点须为1000–100000000的整数。');
      const layers=Math.min(24,samples,capacity);
      return {...base,max_nodes:Math.min(maxNodes,layers),num_layers:layers,timeout_seconds:180,total_samples:samples,weight_seed:20261013};
    }
    if(typeof logs!=='string'||logs.length===0)throw new UiError('input','请输入至少一行日志。');
    const lines=logs.split(/\r?\n/);
    // Python backend counts Unicode code points, rather than JavaScript UTF-16 units.
    if(lines.length<1||lines.length>10000||lines.reduce((n,line)=>n+Array.from(line).length,0)>1000000)throw new UiError('input','日志须为1–10000行、总字符不超过1000000。');
    if(lines.length>capacity)throw new UiError('capacity','支持此运算的可用层容量不足，请增加设备上限或等待节点释放。');
    return {...base,max_nodes:Math.min(maxNodes,lines.length),num_layers:lines.length,timeout_seconds:120,log_lines:lines};
  }
  function typedSummary(task) {
    const value=task.workload_result,workload=task.request?.workload;
    if(!Object.prototype.hasOwnProperty.call(MODEL,workload))return {code:'other-workload',message:'此任务不属于π/日志运算，请在原任务接口核对其结果。'};
    if(task.request.model_id!==MODEL[workload])return {code:'invalid-result',message:'任务模型与运算类型不一致，请核对服务端记录。'};
    if(value===null||value===undefined)return {code:'missing-result',message:'任务已完成，但服务端尚未返回此运算的结果，请刷新任务记录核对。'};
    if(!value||typeof value!=='object'||Array.isArray(value))return {code:'invalid-result',message:'任务已完成，但返回结果格式不符合运算合同。'};
    if(workload==='pi'){
      if(!Number.isSafeInteger(value.samples)||value.samples<=0||(Number.isSafeInteger(task.request.total_samples)&&value.samples!==task.request.total_samples)||!Number.isSafeInteger(value.inside)||value.inside<0||value.inside>value.samples||!Number.isFinite(value.pi_estimate)||Math.abs(value.pi_estimate-4*value.inside/value.samples)>1e-9)
        return {code:'invalid-result',message:'任务已完成，但π采样统计不一致，请核对服务端记录。'};
      return {code:'result',label:'圆周率估算',value:value.pi_estimate.toFixed(6),detail:`${value.samples.toLocaleString()} 个采样点 · ${value.inside.toLocaleString()} 个圆内点`};
    }
    if(workload==='log_stats'){
      const levels=value.levels,keys=['ERROR','WARN','INFO','OTHER'];
      if(!levels||typeof levels!=='object'||Array.isArray(levels)||!Number.isSafeInteger(value.total_lines)||value.total_lines<1||Object.keys(levels).length!==keys.length||!keys.every(key=>Object.prototype.hasOwnProperty.call(levels,key)&&Number.isSafeInteger(levels[key])&&levels[key]>=0)||(Array.isArray(task.request.log_lines)&&value.total_lines!==task.request.log_lines.length)||Object.values(levels).reduce((a,b)=>a+b,0)!==value.total_lines)
        return {code:'invalid-result',message:'任务已完成，但日志计数不一致，请核对服务端记录。'};
      return {code:'result',label:'日志级别统计',value:`ERROR ${levels.ERROR??0} · WARN ${levels.WARN??0}`,detail:`共 ${value.total_lines} 行 · INFO ${levels.INFO??0} · OTHER ${levels.OTHER??0}`};
    }
    return {code:'other-workload',message:'此任务不属于π/日志运算，结果请在原任务接口核对。'};
  }
  function taskOutcome(task) {
    if(task.status==='cancelled')return {terminal:true,code:'cancelled',message:'任务已取消；没有新的运算结果。'};
    if(task.status==='failed')return {terminal:true,code:'failed',message:'任务执行失败，请核对任务分片与节点状态。'};
    if(task.status==='completed')return {terminal:true,...typedSummary(task)};
    return {terminal:false,code:'waiting',message:'任务尚未结束。'};
  }
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const statusName={available:'可用',busy:'计算中',draining:'暂停接单',offline:'离线',queued:'排队中',pending:'待领取',running:'执行中',completed:'已完成',failed:'失败',cancelled:'已取消'};
  function mount(document,options) {
    const $=id=>document.getElementById(id),now=options.now||Date.now,setTimer=options.setTimer||setTimeout,clearTimer=options.clearTimer||clearTimeout;
    let mode='pi',running=false,unknownPost=false,nodes=[],freshAt=null,refreshPending=null;
    const transport=createTransport({...options,setTimer,clearTimer,onUnauthorized:()=>resetPrivate('凭据无效或已失效，页面私有数据已清除。')});
    function clearResult(){ $('result').classList.remove('show');for(const id of ['resultLabel','resultValue','resultDetail'])$(id).textContent=''; }
    function controls(){
      const allowed=allowedOrigin(options.location),connected=transport.authenticated;
      $('token').disabled=!allowed||connected;for(const id of ['logLines','samples','workers'])$(id).disabled=!allowed||!connected||running;$('connect').disabled=!allowed||connected;$('disconnect').hidden=!connected;$('refresh').disabled=!connected||Boolean(refreshPending);
      const pool=eligibleNodes(nodes,mode);
      $('capabilityNote').textContent=connected?`${pool.length} 台明确支持当前运算且可用；仅以实际声明为准。`:'连接后查询可用节点的运算声明。';
      $('run').disabled=!allowed||!connected||running||unknownPost||!pool.length;
      $('run').textContent=running?'等待任务状态…':unknownPost?'派发结果待核对':mode==='pi'?'运行圆周率估算':'运行日志统计';
      document.querySelectorAll('.mode button').forEach(button=>{button.disabled=running;button.classList.toggle('selected',(button.dataset.mode==='logs'?'log_stats':'pi')===mode);button.setAttribute('aria-selected',String((button.dataset.mode==='logs'?'log_stats':'pi')===mode));});
    }
    function resetPrivate(message){
      nodes=[];freshAt=null;refreshPending=null;running=false;unknownPost=false;$('token').value='';$('logLines').value='';
      $('health').classList.remove('ok');$('healthText').textContent='未连接';
      for(const id of ['onlineCount','busyCount','activeCount','doneCount'])$(id).textContent='—';
      $('nodes').innerHTML='<div class="empty">连接管理员凭据后查看节点</div>';$('tasks').innerHTML='<div class="empty">尚未获取任务</div>';
      $('nodeRefresh').textContent='未连接';$('taskRefresh').textContent='—';clearResult();$('error').textContent=message;controls();
    }
    function disconnect(){transport.clear();resetPrivate('已断开并清除页面凭据与数据。已派发的任务仍由服务端管理。');}
    function renderNodes(values){
      $('nodes').innerHTML=values.length?values.map(node=>{
        const state=Object.prototype.hasOwnProperty.call(statusName,node.status)?node.status:'offline';
        const memory=Number.isFinite(node.available_memory_mb)?`${node.available_memory_mb.toLocaleString()} MB`:'未返回';
        const claims=Array.isArray(node.supported_workloads)?node.supported_workloads.filter(v=>['pi','log_stats'].includes(v)).join(' / '):'';
        return `<article class="node"><div class="node-head"><span class="node-name">${esc(node.name)}</span><span class="badge ${state}">${statusName[state]}</span></div><div class="node-meta"><span>${esc(node.platform)}</span><span>${memory}</span></div><div class="quiet">运算声明：${esc(claims||'未声明π/日志支持')}</div><div class="quiet">心跳 ${Number.isFinite(node.last_seen)?esc(beijing(node.last_seen)):'未返回'}</div><div class="node-id">${esc(node.id)}</div></article>`;
      }).join(''):'<div class="empty">没有已注册节点</div>';
    }
    function renderTasks(tasks){
      $('tasks').innerHTML=tasks.length?tasks.map(task=>{
        const request=task.request||{},kind=request.workload==='pi'?'圆周率估算':request.workload==='log_stats'?'日志统计':request.model_id||'其他任务';
        const assignments=Array.isArray(task.assignments)?task.assignments:[],outcome=task.status==='completed'?typedSummary(task):null;
        const summary=outcome?(outcome.code==='result'?`${outcome.label} ${outcome.value} · ${outcome.detail}`:outcome.message):'';
        return `<article class="task"><div class="task-summary"><span class="task-id">${esc(task.id)}</span><span class="task-kind">${esc(kind)}</span><span>${esc(statusName[task.status]||task.status)}</span><span>${assignments.length} 个分片</span><button class="detail-toggle" type="button">查看分片</button></div><div class="assignments">${assignments.map((a,index)=>`<div class="assignment-grid"><span>Worker ${index+1}<br>${esc(a.node_id)}</span><span>区间<br>[${esc(a.layer_start)}, ${esc(a.layer_end)})</span><span>状态<br>${esc(statusName[a.status]||a.status)}</span><span>派发 ID<br>${esc(a.id)}</span><span>${a.error?'分片有错误，请核对记录':'—'}</span></div>`).join('')}${summary?`<div class="result-line">${esc(summary)}</div>`:''}</div></article>`;
      }).join(''):'<div class="empty">暂无任务</div>';
      document.querySelectorAll('.detail-toggle').forEach(button=>button.addEventListener('click',()=>{const task=button.closest('.task');task.classList.toggle('open');button.textContent=task.classList.contains('open')?'收起分片':'查看分片';}));
    }
    function refresh(){
      if(!transport.authenticated)return Promise.resolve(false);if(refreshPending)return refreshPending;
      const ticket=transport.generation;
      const work=(async()=>{try {
        const [n,t]=await Promise.all([transport.request('/v1/nodes'),transport.request('/v1/tasks')]);
        if(!transport.current(ticket))return false;
        if(!Array.isArray(n.nodes)||!Array.isArray(t.tasks))throw new UiError('shape','节点或任务响应格式无效。');
        nodes=n.nodes;freshAt=now();$('health').classList.add('ok');$('healthText').textContent='Controller 已连接 · 管理员';
        $('onlineCount').textContent=nodes.filter(n=>['available','busy','draining'].includes(n.status)).length;
        $('busyCount').textContent=nodes.filter(n=>n.status==='busy').length;$('activeCount').textContent=t.tasks.filter(t=>['queued','running'].includes(t.status)).length;$('doneCount').textContent=t.tasks.filter(t=>t.status==='completed').length;
        const time=beijing(now()/1000);$('nodeRefresh').textContent=`${nodes.length} 台已注册 · ${time}`;$('taskRefresh').textContent=`最近 ${t.tasks.length} 条 · ${time}`;
        renderNodes(nodes);renderTasks(t.tasks);return true;
      }catch(error){if(transport.current(ticket)){nodes=[];freshAt=null;$('health').classList.remove('ok');$('healthText').textContent='查询未确认 · 旧数据可能过期';$('error').textContent=error.message;}return false;}
      finally {if(transport.current(ticket)){refreshPending=null;controls();}}})();
      refreshPending=work;controls();return work;
    }
    async function connect(){
      const value=$('token').value;transport.clear();resetPrivate('');
      if(!value.trim()){resetPrivate('请输入管理员令牌。');return;}
      try{transport.replace(value);$('token').value='';controls();await refresh();}
      catch(error){transport.clear();resetPrivate(error.message);}
    }
    function showOutcome(task){
      clearResult();const outcome=taskOutcome(task);
      if(outcome.code!=='result'){$('error').textContent=outcome.message;return;}
      $('error').textContent='';$('result').classList.add('show');$('resultLabel').textContent=`${outcome.label} · 任务 ${task.id}`;$('resultValue').textContent=outcome.value;$('resultDetail').textContent=outcome.detail;
    }
    function pause(ms,ticket){return new Promise(resolve=>setTimer(()=>resolve(transport.current(ticket)),ms));}
    async function runTask(){
      if(running||unknownPost)return;const ticket=transport.generation;
      running=true;clearResult();$('error').textContent='';controls();let sent=false,createdId=null;
      try {
        if(!transport.authenticated)throw new UiError('credential','请先连接管理员凭据。');
        if(freshAt===null||now()-freshAt>5000){if(!await refresh())throw new UiError('capability','节点声明尚未确认，请刷新后重试。');}
        if(!transport.current(ticket))return;
        const body=buildRequest(mode,{samples:Number($('samples').value),workers:Number($('workers').value),logs:$('logLines').value},nodes);
        sent=true;const created=await transport.request('/v1/tasks',{method:'POST',body});
        if(!created||typeof created!=='object'||Array.isArray(created)||typeof created.id!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(created.id))throw new UiError('network','派发响应缺少有效任务 ID，结果尚未确认。');
        createdId=created.id;const deadline=now()+body.timeout_seconds*1000;
        while(now()<deadline){
          if(!await pause(1500,ticket))return;
          const task=await transport.request(`/v1/tasks/${encodeURIComponent(createdId)}`);
          if(!transport.current(ticket))return;
          if(!task||typeof task!=='object'||task.id!==createdId)throw new UiError('shape','任务身份不一致，请在任务列表核对。');
          if(taskOutcome(task).terminal){showOutcome(task);return;}
          await refresh();
        }
        $('error').textContent=`页面等待结束，任务 ${createdId} 的服务端状态尚未确认，请刷新核对。`;
      }catch(error){if(transport.current(ticket)){unknownPost=sent&&!createdId&&['network','timeout','server'].includes(error.code);$('error').textContent=unknownPost?'派发结果尚未确认；请先刷新任务列表核对，确认后断开重连才能再次派发。':createdId?`${error.message} · 任务 ${createdId}，请刷新记录核对。`:error.message;}}
      finally {if(transport.current(ticket)){running=false;controls();await refresh();}}
    }
    $('connect').addEventListener('click',connect);$('disconnect').addEventListener('click',disconnect);$('refresh').addEventListener('click',refresh);
    $('token').addEventListener('keydown',event=>{if(event.key==='Enter')connect();});$('run').addEventListener('click',runTask);
    document.querySelectorAll('.mode button').forEach(button=>button.addEventListener('click',()=>{if(running)return;mode=button.dataset.mode==='logs'?'log_stats':'pi';clearResult();$('error').textContent='';$('piFields').hidden=mode!=='pi';$('logFields').hidden=mode!=='log_stats';controls();}));
    $('samples').addEventListener('input',()=>$('sampleLabel').textContent=Number($('samples').value).toLocaleString());$('workers').addEventListener('input',()=>$('workerLabel').textContent=$('workers').value);
    const pagehide=()=>disconnect();if(options.addEventListener)options.addEventListener('pagehide',pagehide);else if(typeof window==='object')window.addEventListener('pagehide',pagehide);
    const interval=(options.setInterval||setInterval)(()=>{if(!document.hidden)refresh();},5000);
    resetPrivate(allowedOrigin(options.location)?'':'请通过 HTTPS 或本机 loopback 打开本页；不要输入凭据。');
    return {connect,disconnect,refresh,runTask,transport,showOutcome,destroy(){transport.clear();(options.clearInterval||clearInterval)(interval);},get mode(){return mode;}};
  }
  return {allowedOrigin,createTransport,eligibleNodes,buildRequest,typedSummary,taskOutcome,beijing,mount};
});
