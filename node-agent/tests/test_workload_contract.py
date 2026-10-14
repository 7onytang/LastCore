"""Pure Fake/Mock workload compatibility; no native/model worker, service or actual task."""
import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.config import AgentConfig
from lastcore_node_agent.controller import ControllerClient
from lastcore_node_agent.credentials import (NodeCredentials, StateError, load_pending_report_results,
    load_pending_reports, record_pending_report)
from lastcore_node_agent.executor import ExecutionContext, ExecutionCancelled, SliceDemoExecutor

PI = 'lastcore/pi-monte-carlo-v1'
REQUEST = {'model_id':PI, 'workload':'pi', 'num_layers':1, 'total_samples':1000, 'weight_seed':7}
VALUES = {'inside':500, 'samples':1000}


def context(request=None, deadline=1000):
    request = request or REQUEST
    return ExecutionContext(task_id='task',assignment_id='a',model_id=request['model_id'],position=0,
        layer_start=0,layer_end=request['num_layers'],previous_node=None,next_node=None,deadline=deadline,
        raw={'request':request})


def binding():
    return {'origin':'https://fixture.invalid','assignment_id':'a','node_id':'node','task_id':'task','model_id':PI,
            'layer_start':0,'layer_end':1,'request_sha256':hashlib.sha256(json.dumps(REQUEST,sort_keys=True,
                separators=(',',':')).encode()).hexdigest()}


def fixture(tmp_path,handler):
    client=ControllerClient('https://fixture.invalid',transport=httpx.MockTransport(handler))
    agent=NodeAgent(AgentConfig('https://fixture.invalid','http://127.0.0.1:1',128,state_dir=tmp_path,
                              report_retries=1,retry_backoff=0),client)
    agent.credentials=NodeCredentials('node','fixture-private',30)
    return agent,client


def ack():
    return {'id':'task','request':REQUEST,'assignments':[dict(id='a',task_id='task',node_id='node',
        layer_start=0,layer_end=1,status='completed',result=None,workload_result=VALUES)]}


def test_builtin_capability_is_explicit_and_custom_factory_is_safe_default(tmp_path):
    agent,client=fixture(tmp_path,lambda r:httpx.Response(200,json={}))
    from lastcore_node_agent.__main__ import _resolve_executor_factory
    assert _resolve_executor_factory(agent.config) is None
    assert agent.supported_workloads==('pi','log_stats')
    custom=NodeAgent(agent.config,client,executor_factory=lambda:pytest.fail('must not instantiate'))
    assert custom.supported_workloads==()
    requests=[]
    client._client.close()
    client._client=httpx.Client(base_url=client.origin,transport=httpx.MockTransport(
        lambda r:(requests.append(r) or httpx.Response(200,json={}))))
    agent._send_heartbeat()
    assert json.loads(requests[-1].content)['supported_workloads']==['pi','log_stats']
    client.close()


def test_lost_ack_restart_replays_exact_durable_typed_payload(tmp_path):
    calls=[]
    def outage(request):
        calls.append(request)
        raise httpx.ReadError('fixture lost ACK',request=request)
    agent,client=fixture(tmp_path,outage)
    assert agent._record_pending('a','completed',None,VALUES,binding())
    original=(tmp_path/'pending-reports.json').read_bytes()
    agent._flush_pending_reports()
    assert (tmp_path/'pending-reports.json').read_bytes()==original
    client.close()
    second,client=fixture(tmp_path,lambda r:(calls.append(r) or httpx.Response(200,json=ack())))
    second._pending=load_pending_reports(tmp_path)
    second._pending_results=load_pending_report_results(tmp_path)
    second._flush_pending_reports()
    assert calls[0].content==calls[1].content
    assert json.loads(calls[-1].content)=={'status':'completed','error':None,'workload_result':VALUES}
    assert not load_pending_reports(tmp_path)
    assert all(r.url.path=='/v1/nodes/node/assignments/a/report' for r in calls)
    client.close()


@pytest.mark.parametrize('change',['task','node','assignment','range','request','result','bool-result','404','409'])
def test_unknown_ack_or_terminal_numeric_conflict_preserves_immutable_pending(tmp_path,change):
    reply=json.loads(json.dumps(ack()))
    status=200
    if change=='task':reply['id']='other'
    elif change=='node':reply['assignments'][0]['node_id']='other'
    elif change=='assignment':reply['assignments'][0]['id']='other'
    elif change=='range':reply['assignments'][0]['layer_end']=2
    elif change=='request':reply['request']['weight_seed']=8
    elif change=='result':reply['assignments'][0]['workload_result']['inside']=1
    elif change=='bool-result':reply['assignments'][0]['workload_result']['inside']=True
    else:status=int(change)
    agent,client=fixture(tmp_path,lambda r:httpx.Response(status,json=reply))
    assert agent._record_pending('a','completed',None,VALUES,binding())
    before=(tmp_path/'pending-reports.json').read_bytes()
    agent._flush_pending_reports()
    assert (tmp_path/'pending-reports.json').read_bytes()==before
    assert load_pending_reports(tmp_path)=={'a':('completed',None)}
    if status!=200:assert agent.stop_event.is_set()
    client.close()


def test_typed_journal_is_immutable_corruption_and_identity_mismatch_fail_closed(tmp_path):
    record_pending_report(tmp_path,'a','completed',None,VALUES,binding())
    before=(tmp_path/'pending-reports.json').read_bytes()
    with pytest.raises(StateError):record_pending_report(tmp_path,'a','completed',None,{'inside':1,'samples':1000},binding())
    assert (tmp_path/'pending-reports.json').read_bytes()==before
    data=json.loads(before);data['a']['binding']['assignment_id']='different'
    (tmp_path/'pending-reports.json').write_text(json.dumps(data))
    with pytest.raises(StateError):load_pending_report_results(tmp_path)
    (tmp_path/'pending-reports.json').write_text('{bad')
    with pytest.raises(StateError):load_pending_report_results(tmp_path)


def test_fake_pi_cancel_is_observed_with_bounded_checkpoints():
    run=context(dict(REQUEST,total_samples=100_000_000))
    calls=[]
    def random(*args):
        calls.append(args)
        if len(calls)==1:run.request_cancel('shutdown')
        return 0.0
    with pytest.raises(ExecutionCancelled,match='shutdown'):
        SliceDemoExecutor(slices=0,clock=lambda:0,random_unit=random).run(run)
    assert len(calls)<=512


def test_fake_pi_deadline_and_cancel_before_return_do_not_publish_result():
    run=context(deadline=1)
    now=[0]
    def random(*args):
        now[0]=1
        return 0.0
    with pytest.raises(ExecutionCancelled,match='deadline'):
        SliceDemoExecutor(slices=0,clock=lambda:now[0],random_unit=random).run(run)
    run=context(dict(REQUEST,total_samples=1000,num_layers=1000))
    run.layer_end=1
    def last_sample(*args):
        run.request_cancel('task-terminal')
        return 0.0
    with pytest.raises(ExecutionCancelled,match='task-terminal'):
        SliceDemoExecutor(slices=0,clock=lambda:0,random_unit=last_sample).run(run)


def test_fake_log_checks_cancel_after_last_line():
    run=context({'model_id':'lastcore/log-statistics-v1','workload':'log_stats','num_layers':1,'log_lines':['INFO']})
    class FakeLine(str):
        def upper(self):
            run.request_cancel('shutdown')
            return 'INFO'
    run.raw['request']['log_lines']=[FakeLine('INFO')]
    with pytest.raises(ExecutionCancelled,match='shutdown'):
        SliceDemoExecutor(slices=0,clock=lambda:0).run(run)


def test_published_linear_override_and_five_arg_failed_reports_still_replay(tmp_path):
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'edge-client'))
    from linear_edge import LinearController,LinearNodeAgent,MODEL,result_file
    row={'id':'a','task_id':'task','node_id':'node','status':'completed','layer_start':0,'layer_end':1,
         'request':{'model_id':MODEL},'result':[.25]}
    calls=[]
    def handler(request):
        calls.append(request)
        if request.method=='GET':return httpx.Response(200,json={'assignments':[row]})
        return httpx.Response(200,json={'id':'task','assignments':[row]})
    client=LinearController('https://fixture.invalid',tmp_path,transport=httpx.MockTransport(handler),publish=lambda *_:None)
    agent=LinearNodeAgent(AgentConfig('https://fixture.invalid','http://127.0.0.1:1',128,state_dir=tmp_path,
        report_retries=1,retry_backoff=0),client,executor_factory=lambda:pytest.fail('no compute'))
    agent.credentials=NodeCredentials('node','fixture',30)
    path=result_file(tmp_path,'a');path.parent.mkdir();path.write_text(json.dumps({'model_id':MODEL,
        'task_id':'task','assignment_id':'a','result':[.25]}))
    record_pending_report(tmp_path,'a','completed',None)
    agent._pending=load_pending_reports(tmp_path)
    agent._flush_pending_reports()
    assert not load_pending_reports(tmp_path)
    assert json.loads(calls[-1].content)['result']==[.25]
    agent._record_pending('failed','failed','fixture')
    agent._flush_pending_reports()
    assert json.loads(calls[-1].content)=={'status':'failed','error':'fixture'}
    assert not load_pending_reports(tmp_path)
    client.close()


def test_frozen_old_request_cache_replays_without_any_linear_calculation(tmp_path,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'edge-client'))
    import linear_edge
    from app.models import TaskRequest
    frozen_request={'model_id':'lastcore/linear-demo-v1','num_layers':2,'layer_memory_mb':1,'max_nodes':1,
        'timeout_seconds':600,'input_vector':[1.0,2.0],'weight_seed':7,
        'target_node_id':'00000000-0000-0000-0000-000000000001'}
    current_request=json.loads(TaskRequest(**frozen_request).protocol_dump_json())
    old_binding=hashlib.sha256(json.dumps(['task','a',frozen_request,0,2],sort_keys=True).encode()).hexdigest()
    path=linear_edge.result_file(tmp_path,'a');path.parent.mkdir()
    path.write_text(json.dumps({'binding':old_binding,'model_id':linear_edge.MODEL,'task_id':'task',
                               'assignment_id':'a','result':[.1,.2]}))
    original=path.read_bytes()
    monkeypatch.setattr(linear_edge,'weight',lambda *_:pytest.fail('must not calculate'))
    run=ExecutionContext(task_id='task',assignment_id='a',model_id=linear_edge.MODEL,position=0,
        layer_start=0,layer_end=2,previous_node=None,next_node=None,deadline=9999999999,
        raw={'request':current_request})
    linear_edge.LinearExecutor(tmp_path).run(run)
    assert path.read_bytes()==original
