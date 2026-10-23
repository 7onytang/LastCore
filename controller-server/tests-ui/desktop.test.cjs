'use strict';
// Run production desktop JS against IDs parsed from production HTML. No browser/server.
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname,'..');
const code = fs.readFileSync(path.join(root,'app/ui/console.js'),'utf8');
const html = fs.readFileSync(path.join(root,'app/ui/desktop.html'),'utf8');
function admission(desired=true,status='confirmed',request=null,confirmed=request) {
  return {desired,confirmed:desired,request_id:request,confirmed_request_id:confirmed,status};
}
function state(admit=admission()) {
  return {running:true,stopping:false,exit_code:null,mode:'stage',
    controller_origin:'https://actual.invalid:8443',configured_resources:{available_memory_mb:512,compute_weight:2},
    recent_errors:[],device:{cpu_percent:null},
    observation:{node_id:'own-node',connection:'connected',admission:admit,poll_status:'ok',task:{
      task_id:'active-task',assignment_id:'assignment',executor:'stage',model_id:'tiny',status:'running',layer_start:0,layer_end:2}}};
}
async function fixture(initial=state()) {
  const elements={};
  for (const [,id] of html.matchAll(/\bid="([^"]+)"/g)) {
    elements[id]={textContent:'',hidden:false,disabled:false,className:'',listeners:{},
      classList:{toggle(){},add(){}},addEventListener(event,handler){this.listeners[event]=handler;}};
  }
  let next=initial,error=null;
  const requests=[];
  const context={console,Date,Intl,Number,Array,Object,Error,TypeError,
    location:{hash:'#private-local-session',pathname:'/',search:''},history:{replaceState(){}},
    window:{addEventListener(){}},AbortSignal:{timeout(){return {}; }},setInterval(){},
    document:{documentElement:{dataset:{surface:'desktop'}},hidden:false,querySelectorAll(){return [];},
      getElementById(id){assert.ok(elements[id],`production ID missing from HTML: ${id}`);return elements[id];}},
    async fetch(url,options){requests.push({url,options});if(error)throw error;return {ok:true,json:async()=>next};}};
  vm.createContext(context);vm.runInContext(code,context);
  await new Promise(resolve=>setImmediate(resolve));
  return {elements,requests,setState(value){next=value;error=null;},setError(value){error=value;},
    async click(id){await elements[id].listeners.click();}};
}

test('production bindings show actual origin/config resources, never global controls',async()=>{
  const f=await fixture();
  assert.equal(f.elements['own-controller-origin'].textContent,'https://actual.invalid:8443');
  assert.match(f.elements['own-configured-resources'].textContent,/配置值，非占用测量/);
  assert.equal(f.elements['own-node-id'].textContent,'own-node');
  assert.equal(f.elements['own-admission'].textContent,'接收新任务 · 心跳已确认');
  assert.equal(f.requests[0].options.headers['X-LastCore-Session'],'private-local-session');
  assert.ok(!f.requests[0].options.headers.Authorization);
  assert.ok(!f.elements['node-list'] && !f.elements['task-list']);
});

test('pause/resume callbacks request actual routes and await matching ACK without stopping task',async()=>{
  const f=await fixture();
  f.setState(state({...admission(false,'pending','pause',null),confirmed:true}));
  await f.click('pause-agent');
  assert.equal(f.requests.at(-1).url,'/desktop/pause');assert.equal(f.requests.at(-1).options.method,'POST');
  assert.equal(f.elements['own-admission'].textContent,'暂停接单待确认');
  assert.equal(f.elements['own-task-id'].textContent,'active-task');
  assert.equal(f.elements['task-status'].textContent,'执行中');
  assert.equal(f.elements['stop-agent'].disabled,false);
  f.setState(state(admission(false,'confirmed','pause','old')));
  await f.click('refresh-local');
  assert.equal(f.elements['own-admission'].textContent,'暂停接单待确认');
  f.setState(state(admission(false,'confirmed','pause','pause')));
  await f.click('refresh-local');
  assert.equal(f.elements['own-admission'].textContent,'暂停接单 · 心跳已确认');
  f.setState(state(admission(true,'pending','resume','pause')));
  await f.click('resume-agent');
  assert.equal(f.requests.at(-1).url,'/desktop/resume');
  assert.equal(f.elements['own-admission'].textContent,'恢复接单待确认');
});

test('ambiguous request failure disables repeat controls until real state read succeeds',async()=>{
  const f=await fixture();f.setError(new Error('injected request failure'));
  await f.click('pause-agent');
  assert.equal(f.elements['process-status'].textContent,'状态未确认');
  for(const id of ['start-agent','stop-agent','pause-agent','resume-agent'])assert.equal(f.elements[id].disabled,true);
  assert.equal(f.elements['refresh-local'].hidden,false);
  f.setState(state(admission(false,'confirmed','pause','pause')));await f.click('refresh-local');
  assert.equal(f.elements['own-admission'].textContent,'暂停接单 · 心跳已确认');
  assert.equal(f.elements['resume-agent'].disabled,false);
});

test('stopping is retained until child exit, with stale task and safe errors explicit',async()=>{
  const f=await fixture();const stopping={...state(),stopping:true,recent_errors:['cleanup_timeout','private-token']};
  f.setState(stopping);await f.click('stop-agent');
  assert.equal(f.requests.at(-1).url,'/desktop/stop');
  assert.equal(f.elements['process-status'].textContent,'正在停止 · 等待确认');
  assert.equal(f.elements['own-admission'].textContent,'停止中，接单状态未确认');
  assert.equal(f.elements['pause-agent'].disabled,true);assert.equal(f.elements['start-agent'].hidden,true);
  assert.ok(!f.elements['own-recent-errors'].textContent.includes('private-token'));
  f.setState({...stopping,running:false,stopping:false,exit_code:0});await f.click('refresh-local');
  assert.equal(f.elements['process-status'].textContent,'已停止');
  assert.equal(f.elements['own-admission'].textContent,'未运行');
  assert.equal(f.elements['task-status'].textContent,'最后状态未确认');
});
