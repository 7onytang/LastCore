'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const {allowedOrigin,createTransport,eligibleNodes,buildRequest,taskOutcome,beijing,mount}=require('../app/ui/workloads.js');
const https={protocol:'https:',hostname:'controller.test'};
const token='mock-admin-credential-not-real';
const reply=(data,status=200)=>({ok:status>=200&&status<300,status,redirected:false,json:async()=>data});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
class Clock {
  constructor(){this.now=Date.UTC(2026,9,13);this.id=0;this.timers=new Map();}
  set=(fn,ms)=>{const id=++this.id;this.timers.set(id,{at:this.now+ms,fn});return id;};
  clear=id=>this.timers.delete(id);
  advance(ms){const end=this.now+ms;for(;;){const next=[...this.timers].filter(([,v])=>v.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;this.now=next[1].at;this.timers.delete(next[0]);next[1].fn();}this.now=end;}
}
function transport(fetch,extra={}){const clock=new Clock();return{clock,api:createTransport({fetch,location:https,setTimer:clock.set,clearTimer:clock.clear,...extra})};}
const node=(id='node',changes={})=>({id,name:'Mock worker',platform:'fixture',status:'available',available_memory_mb:4096,compute_weight:1,last_seen:Date.UTC(2026,9,13)/1000,supported_workloads:['pi','log_stats'],...changes});
function fakeDocument(){
  class Element {
    constructor(){this.textContent='';this.innerHTML='';this.value='';this.hidden=false;this.disabled=false;this.dataset={};this.events={};this.attributes={};const classes=new Set();this.classList={add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,force)=>{if(force===undefined)force=!classes.has(x);force?classes.add(x):classes.delete(x);return force;}};}
    addEventListener(name,fn){this.events[name]=fn;}
    setAttribute(name,value){this.attributes[name]=value;}
  }
  const source=fs.readFileSync(require.resolve('../app/showcase.html'),'utf8');
  const map=Object.fromEntries([...source.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
  map.samples.value='300000';map.workers.value='3';map.logLines.value='Mock INFO';
  const modes=['pi','logs'].map(mode=>{const e=new Element();e.dataset.mode=mode;return e;});
  return{hidden:false,map,modes,getElementById:id=>{assert.ok(map[id],`missing DOM ID ${id}`);return map[id];},querySelectorAll:selector=>selector==='.mode button'?modes:[]};
}
function app(fetch,location=https){const doc=fakeDocument(),clock=new Clock(),events={};const ui=mount(doc,{fetch,location,now:()=>clock.now,setTimer:clock.set,clearTimer:clock.clear,setInterval:()=>1,clearInterval:()=>{},addEventListener:(n,fn)=>{events[n]=fn;}});return{ui,doc,clock,events};}
async function connect(view){view.doc.map.token.value=token;await view.ui.connect();}
const idleFetch=async path=>reply(path==='/v1/nodes'?{nodes:[node()]}:{tasks:[]});

test('public HTTP refuses all Bearer GET/POST before fetch; exact loopback accepted',async()=>{
  let calls=0;const {api}=transport(()=>{calls++;throw Error('must not fetch');},{location:{protocol:'http:',hostname:'8.134.161.123'}});
  assert.throws(()=>api.replace(token),e=>e.code==='unsafe-origin');
  await assert.rejects(api.request('/v1/tasks',{method:'POST',body:{}}),e=>e.code==='unsafe-origin');assert.equal(calls,0);
  for(const hostname of ['localhost','127.0.0.1','[::1]'])assert.equal(allowedOrigin({protocol:'http:',hostname}),true);
  for(const hostname of ['localhost.attacker.test','192.168.1.2'])assert.equal(allowedOrigin({protocol:'http:',hostname}),false);
  assert.equal(allowedOrigin({protocol:'file:',hostname:''}),false);
});
test('same-origin transport does not forward cookies, follow redirects or use external paths',async()=>{
  const calls=[];const {api}=transport(async(path,opts)=>{calls.push({path,opts});return reply({nodes:[]});});api.replace(token);
  await api.request('/v1/nodes');assert.equal(calls[0].opts.credentials,'omit');assert.equal(calls[0].opts.redirect,'error');assert.equal(calls[0].opts.cache,'no-store');assert.equal(calls[0].opts.mode,'same-origin');
  assert.equal(calls[0].opts.headers.Authorization,`Bearer ${token}`);
  await assert.rejects(api.request('//attacker.test/v1/tasks'),e=>e.code==='path');assert.equal(calls.length,1);
});
test('timeout resolves even if a fetch double ignores AbortSignal',async()=>{
  const {api,clock}=transport(()=>new Promise(()=>{}));api.replace(token);
  const request=api.request('/v1/nodes'),expect=assert.rejects(request,e=>e.code==='timeout');clock.advance(12000);await expect;assert.equal(clock.timers.size,0);
});
test('response body read shares the request timeout',async()=>{
  const {api,clock}=transport(async()=>({...reply({}),json:()=>new Promise(()=>{})}));api.replace(token);
  const request=api.request('/v1/tasks'),expect=assert.rejects(request,e=>e.code==='timeout');await flush();clock.advance(12000);await expect;
});
test('a stale401 cannot clear a replacement credential generation',async()=>{
  const late=deferred();let denied=0;const {api}=transport(()=>late.promise,{onUnauthorized:()=>denied++});api.replace('old');
  const old=api.request('/v1/nodes'),expect=assert.rejects(old,e=>e.code==='stale');api.replace('new');await expect;late.resolve(reply({},401));await flush();assert.equal(api.authenticated,true);assert.equal(denied,0);
});
test('401 immediately clears credential, all private views/results/log inputs and rejects late refill',async()=>{
  let phase='ok';const late=deferred();const view=app(path=>phase==='ok'?Promise.resolve(reply(path==='/v1/nodes'?{nodes:[node('private-node')]}:{tasks:[{id:'private-task',request:{},status:'running'}]})):path==='/v1/nodes'?Promise.resolve(reply({},401)):late.promise);
  await connect(view);view.doc.map.logLines.value='private operator logs';view.doc.map.result.classList.add('show');view.doc.map.resultValue.textContent='private result';phase='401';await view.ui.refresh();
  assert.equal(view.ui.transport.authenticated,false);assert.equal(view.doc.map.token.value,'');assert.equal(view.doc.map.logLines.value,'');assert.equal(view.doc.map.onlineCount.textContent,'—');
  assert.equal(view.doc.map.resultValue.textContent,'');assert.equal(view.doc.map.result.classList.contains('show'),false);assert.ok(!view.doc.map.nodes.innerHTML.includes('private-node'));assert.ok(!view.doc.map.tasks.innerHTML.includes('private-task'));
  late.resolve(reply({tasks:[{id:'late-private'}]}));await flush();assert.ok(!view.doc.map.tasks.innerHTML.includes('late-private'));view.ui.destroy();
});
test('old successful refresh cannot refill views after disconnect/reconnect',async()=>{
  const oldNodes=deferred(),oldTasks=deferred();let phase='old';const view=app(path=>phase==='old'?(path==='/v1/nodes'?oldNodes.promise:oldTasks.promise):Promise.resolve(reply(path==='/v1/nodes'?{nodes:[node('new-node')]}:{tasks:[]})));
  view.doc.map.token.value='old';const old=view.ui.connect();view.ui.disconnect();phase='new';await connect(view);await old;
  oldNodes.resolve(reply({nodes:[node('old-private-node')]}));oldTasks.resolve(reply({tasks:[{id:'old-private-task'}]}));await flush();
  assert.ok(view.doc.map.nodes.innerHTML.includes('new-node'));assert.ok(!view.doc.map.nodes.innerHTML.includes('old-private'));assert.ok(!view.doc.map.tasks.innerHTML.includes('old-private'));view.ui.destroy();
});
test('pagehide clears state without cancelling/reposting any server task',async()=>{
  const calls=[];const view=app(async(path,options)=>{calls.push({path,options});return idleFetch(path);});await connect(view);view.events.pagehide();assert.equal(view.ui.transport.authenticated,false);assert.equal(view.doc.map.nodes.innerHTML.includes('Mock worker'),false);assert.ok(calls.every(c=>c.options.method==='GET'));view.ui.destroy();
});
test('only explicit supported workload and available memory-backed nodes form the pool',()=>{
  const nodes=[node('mac',{platform:'macOS',supported_workloads:[]}),node('old',{supported_workloads:undefined}),node('busy',{status:'busy'}),node('offline',{status:'offline'}),node('badmem',{available_memory_mb:0}),node('pi',{supported_workloads:['pi']})];
  assert.deepEqual(eligibleNodes(nodes,'pi').map(n=>n.id),['pi']);assert.deepEqual(eligibleNodes(nodes,'log_stats'),[]);
});
test('no compatible nodes cannot dispatch even if runTask is invoked directly',async()=>{
  const calls=[];const view=app(async(path,opts)=>{calls.push({path,opts});return reply(path==='/v1/nodes'?{nodes:[node('mac',{supported_workloads:[]})]}:{tasks:[]});});await connect(view);await view.ui.runTask();assert.equal(view.doc.map.run.disabled,true);assert.ok(!calls.some(c=>c.opts.method==='POST'));assert.match(view.doc.map.error.textContent,/没有声明支持/);view.ui.destroy();
});
test('PI bounds/capacity match contract; no platform-inferred or custom target IDs',()=>{
  for(const samples of [1000,100000000]){const r=buildRequest('pi',{samples,workers:8},[node('low',{available_memory_mb:1}),node('higher',{available_memory_mb:2})]);assert.equal(r.num_layers,3);assert.equal(r.max_nodes,2);assert.equal(r.workload,'pi');assert.equal(r.model_id,'lastcore/pi-monte-carlo-v1');assert.ok(r.weight_seed>=0&&r.weight_seed<=2147483647);assert.ok(r.num_layers<=r.total_samples);assert.equal('target_node_id'in r,false);assert.equal('input_vector'in r,false);}
  for(const samples of [999,100000001,1.5,NaN])assert.throws(()=>buildRequest('pi',{samples,workers:1},[node()]),e=>e.code==='input');
});
test('log requests preserve blank lines and set exact num_layers without seed/sample fields',()=>{
  const r=buildRequest('log_stats',{logs:'INFO one\n\nWARN two',workers:1},[node()]);assert.deepEqual(r.log_lines,['INFO one','','WARN two']);assert.equal(r.num_layers,3);assert.equal('weight_seed'in r,false);assert.equal('total_samples'in r,false);assert.equal('input_vector'in r,false);
  assert.throws(()=>buildRequest('log_stats',{logs:'a\nb',workers:1},[node('small',{available_memory_mb:1})]),e=>e.code==='capacity');
});
test('log count and Unicode character bounds use Python-style code points',()=>{
  assert.equal(buildRequest('log_stats',{logs:'x'.repeat(1000000),workers:1},[node()]).log_lines.length,1);
  assert.equal(buildRequest('log_stats',{logs:'😀'.repeat(1000000),workers:1},[node()]).num_layers,1);
  assert.equal(buildRequest('log_stats',{logs:Array(10000).fill('x').join('\n'),workers:1},[node('large',{available_memory_mb:10000})]).num_layers,10000);
  for(const logs of ['',undefined,'x'.repeat(1000001),Array(10001).fill('x').join('\n')])assert.throws(()=>buildRequest('log_stats',{logs,workers:1},[node('large',{available_memory_mb:100000})]));
});
test('typed result ignores legacy result and distinguishes cancelled/failed/completed-missing',()=>{
  const request={workload:'pi',model_id:'lastcore/pi-monte-carlo-v1',total_samples:1000};
  assert.equal(taskOutcome({status:'cancelled',request}).code,'cancelled');assert.equal(taskOutcome({status:'failed',request}).code,'failed');
  const missing=taskOutcome({status:'completed',request,result:{inside:785,samples:1000,pi_estimate:3.14}});assert.equal(missing.code,'missing-result');assert.ok(!missing.message.includes('超时'));
  assert.equal(taskOutcome({status:'completed',request:{model_id:'lastcore/linear-demo-v1'},result:[.125]}).code,'other-workload');
});
test('PI statistics must agree; valid zero estimate is not a missing result',()=>{
  const request={workload:'pi',model_id:'lastcore/pi-monte-carlo-v1',total_samples:1000};
  assert.equal(taskOutcome({status:'completed',request,workload_result:{inside:785,samples:1000,pi_estimate:3.14}}).value,'3.140000');
  assert.equal(taskOutcome({status:'completed',request,workload_result:{inside:0,samples:1000,pi_estimate:0}}).value,'0.000000');
  for(const value of [{inside:1001,samples:1000,pi_estimate:4.004},{inside:785,samples:1000,pi_estimate:3},{inside:1,samples:2,pi_estimate:2}])assert.equal(taskOutcome({status:'completed',request,workload_result:value}).code,'invalid-result');
});
test('typed log results require actual counts, not fabricated missing zeros',()=>{
  const request={workload:'log_stats',model_id:'lastcore/log-statistics-v1',log_lines:['INFO x','WARN x']};
  const valid={levels:{ERROR:0,WARN:1,INFO:1,OTHER:0},total_lines:2};assert.equal(taskOutcome({status:'completed',request,workload_result:valid}).code,'result');
  assert.equal(taskOutcome({status:'completed',request,workload_result:{levels:{INFO:2},total_lines:2}}).code,'invalid-result');
});
test('terminal result UI clears prior values and never turns cancellation/missing into waiting timeout',()=>{
  const view=app(idleFetch);const request={workload:'pi',model_id:'lastcore/pi-monte-carlo-v1',total_samples:1000};
  view.ui.showOutcome({id:'own-A',status:'completed',request,workload_result:{inside:785,samples:1000,pi_estimate:3.14}});assert.match(view.doc.map.resultLabel.textContent,/own-A/);
  view.ui.showOutcome({id:'own-B',status:'cancelled',request});assert.match(view.doc.map.error.textContent,/已取消/);assert.equal(view.doc.map.resultValue.textContent,'');
  view.ui.showOutcome({id:'own-C',status:'completed',request,workload_result:null});assert.match(view.doc.map.error.textContent,/已完成.*尚未返回/);assert.ok(!view.doc.map.error.textContent.includes('超时'));view.ui.destroy();
});
test('a POST timeout is not retried and freezes duplicate dispatch until explicit reconnection',async()=>{
  const calls=[];const view=app((path,opts)=>{calls.push({path,opts});return opts.method==='POST'?new Promise(()=>{}):idleFetch(path);});await connect(view);
  const run=view.ui.runTask();await flush();view.clock.advance(12000);await run;
  assert.equal(calls.filter(c=>c.opts.method==='POST').length,1);assert.match(view.doc.map.error.textContent,/派发结果尚未确认/);assert.equal(view.doc.map.run.disabled,true);
  await view.ui.runTask();assert.equal(calls.filter(c=>c.opts.method==='POST').length,1);view.ui.destroy();
});
test('date and refresh timestamps explicitly use Beijing business time',async()=>{
  assert.match(beijing(Date.UTC(2026,9,13,0,0)/1000),/08:00:00.*北京时间/);
  const view=app(idleFetch);await connect(view);assert.match(view.doc.map.nodeRefresh.textContent,/08:00:00.*北京时间/);view.ui.destroy();
});
test('untrusted node/model metadata is rendered as escaped text',async()=>{
  const view=app(async path=>reply(path==='/v1/nodes'?{nodes:[node('<node>',{name:'<img src=x onerror=leak>'})]}:{tasks:[{id:'<task>',request:{model_id:'<script>leak</script>'},status:'queued',assignments:[]}]}));await connect(view);
  assert.ok(view.doc.map.nodes.innerHTML.includes('&lt;img'));assert.ok(!view.doc.map.nodes.innerHTML.includes('<img'));
  assert.ok(view.doc.map.tasks.innerHTML.includes('&lt;script&gt;'));assert.ok(!view.doc.map.tasks.innerHTML.includes('<script>'));view.ui.destroy();
});
test('strict-CSP HTML uses same-origin external assets and explicit administrator-only controls',()=>{
  const source=fs.readFileSync(require.resolve('../app/showcase.html'),'utf8');assert.ok(source.includes('/ui/workloads.js'));assert.ok(source.includes('/ui/workloads.css'));assert.ok(!/<script\s*>|<style>|\bonclick=|\bstyle=/.test(source));assert.ok(source.includes('仅管理员'));
});


test('empty credential gives an actionable message without requesting an endpoint',async()=>{
  let calls=0;const view=app(()=>{calls++;throw Error('must not fetch');});await view.ui.connect();assert.equal(calls,0);assert.match(view.doc.map.error.textContent,/请输入管理员令牌/);view.ui.destroy();
});
test('a malformed successful POST cannot enable a duplicate dispatch',async()=>{
  let posts=0;const view=app(async(path,opts)=>{if(opts.method==='POST'){posts++;return reply(null,201);}return idleFetch(path);});await connect(view);await view.ui.runTask();assert.equal(view.doc.map.run.disabled,true);assert.match(view.doc.map.error.textContent,/派发结果尚未确认/);await view.ui.runTask();assert.equal(posts,1);view.ui.destroy();
});
