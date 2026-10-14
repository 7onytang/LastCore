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
    return {'id':'task','status':'completed','request':REQUEST,'assignments':[dict(id='a',task_id='task',node_id='node',
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


@pytest.mark.parametrize('change',['task','node','assignment','range','request','result','bool-result','invalid-status','bool-layer','404','409'])
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
    elif change=='invalid-status':reply['status']='unknown'
    elif change=='bool-layer':reply['assignments'][0]['layer_end']=True
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
    frozen_request=json.loads((Path(__file__).resolve().parents[2]/
        'edge-client/tests/fixtures/linear-request-63d409a8.json').read_text())
    current_request=dict(frozen_request)  # Wire fixture also checked by the isolated Controller suite.
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


@pytest.mark.parametrize('marker',['binding','workload_result','result'])
def test_incomplete_typed_marker_preserves_bytes_and_blocks_all_http_and_execution(tmp_path,marker):
    entry={'status':'completed','error':None,marker:binding() if marker=='binding' else VALUES}
    path=tmp_path/'pending-reports.json'
    path.write_text(json.dumps({'a':entry}))
    original=path.read_bytes()
    calls=[]
    def handler(request):
        calls.append(request)
        pytest.fail('corrupt journal must be rejected before any HTTP')
    agent,client=fixture(tmp_path,handler)
    agent.executor_factory=lambda:pytest.fail('must not execute new work')
    with pytest.raises(StateError):
        agent.run_forever()
    assert agent.stop_event.is_set() and not calls and not agent.stats.started
    assert path.read_bytes()==original
    with pytest.raises(StateError):
        record_pending_report(tmp_path,'a','completed',None)
    assert path.read_bytes()==original
    client.close()


def test_incomplete_marker_detected_during_poll_never_posts_or_starts_new_work(tmp_path):
    path=tmp_path/'pending-reports.json'
    path.write_text(json.dumps({'a':{'status':'completed','error':None,'binding':binding()}}))
    original=path.read_bytes()
    calls=[]
    row={'id':'new','task_id':'new-task','status':'pending','task_status':'queued','deadline':9999999999,
         'layer_start':0,'layer_end':1,'request':{'model_id':'legacy-demo'}}
    def handler(request):
        calls.append(request)
        assert request.method=='GET'
        return httpx.Response(200,json={'assignments':[row]})
    agent,client=fixture(tmp_path,handler)
    agent._pending={'a':('completed',None)}  # A running process held a valid legacy snapshot before corruption.
    agent.executor_factory=lambda:pytest.fail('must not execute new work')
    with pytest.raises(StateError):agent._poll_once()
    assert calls and all(request.method=='GET' for request in calls)
    assert agent.stop_event.is_set() and not agent.stats.started
    assert path.read_bytes()==original
    client.close()


def test_disappeared_known_typed_journal_cannot_be_sent_as_legacy_status_only(tmp_path):
    calls=[]
    row={'id':'new','task_id':'new-task','status':'pending','task_status':'queued','deadline':9999999999,
         'layer_start':0,'layer_end':1,'request':{'model_id':'legacy-demo'}}
    def handler(request):
        calls.append(request)
        assert request.method=='GET'
        return httpx.Response(200,json={'assignments':[row]})
    agent,client=fixture(tmp_path,handler)
    assert agent._record_pending('a','completed',None,VALUES,binding())
    path=tmp_path/'pending-reports.json'
    path.unlink()
    agent.executor_factory=lambda:pytest.fail('must not execute after outcome disappearance')
    with pytest.raises(StateError):agent._poll_once()
    assert agent.stop_event.is_set() and not agent.stats.started
    assert all(request.method=='GET' for request in calls)
    assert agent._pending_results['a'][0]==VALUES and not path.exists()
    client.close()


@pytest.mark.parametrize('invalid',[[],0,False])
def test_falsy_invalid_journal_is_not_overwritten(tmp_path,invalid):
    path=tmp_path/'pending-reports.json';path.write_text(json.dumps(invalid));before=path.read_bytes()
    with pytest.raises(StateError):record_pending_report(tmp_path,'a','completed',None,VALUES,binding())
    assert path.read_bytes()==before



def test_legacy_executor_return_value_cannot_change_the_five_argument_report_contract(tmp_path):
    from types import SimpleNamespace
    agent,client=fixture(tmp_path,lambda request:pytest.fail('no HTTP in execution fixture'))
    run=context({'model_id':'legacy-demo','num_layers':1})
    active=SimpleNamespace(context=run,result=None,result_payload=None)
    agent._active=active
    class LegacyExecutor:
        def run(self,context):return {'legacy-local-summary':1}
    agent._execute(run,LegacyExecutor())
    assert active.result==('completed',None) and active.result_payload is None
    client.close()


@pytest.mark.parametrize("cancelled",[False,True])
def test_typed_executor_cannot_complete_without_its_result(tmp_path,cancelled):
    from types import SimpleNamespace
    agent,client=fixture(tmp_path,lambda request:pytest.fail('no HTTP in execution fixture'))
    run=context();active=SimpleNamespace(context=run,result=None,result_payload=None)
    if cancelled:run.request_cancel("shutdown")
    agent._active=active
    class MissingResultExecutor:
        def run(self,context):return None
    agent._execute(run,MissingResultExecutor())
    assert active.result[0]==('cancelled-local' if cancelled else 'failed') and active.result_payload is None
    client.close()


@pytest.mark.parametrize('invalid',[None,False,0,[],"",1,[1],"not-an-object"])
@pytest.mark.parametrize('where',['top','entry'])
def test_present_invalid_json_grammar_is_not_missing_and_cannot_be_overwritten(tmp_path,invalid,where):
    from lastcore_node_agent.credentials import clear_pending_report
    data=invalid if where=='top' else {'a':invalid}
    path=tmp_path/'pending-reports.json';path.write_text(json.dumps(data));before=path.read_bytes()
    calls=[]
    agent,client=fixture(tmp_path,lambda request:(calls.append(request) or pytest.fail('zero HTTP required')))
    agent.executor_factory=lambda:pytest.fail('zero execution required')
    for loader in (load_pending_reports,load_pending_report_results):
        with pytest.raises(StateError):loader(tmp_path)
    with pytest.raises(StateError):agent.run_forever()
    assert agent.stop_event.is_set() and not calls and not agent.stats.started
    with pytest.raises(StateError):record_pending_report(tmp_path,'a','completed',None)
    with pytest.raises(StateError):record_pending_report(tmp_path,'new','completed',None,VALUES,binding())
    with pytest.raises(StateError):clear_pending_report(tmp_path,'a')
    assert path.read_bytes()==before
    client.close()


@pytest.mark.parametrize('entry',[{}, {'status':False}, {'status':'completed','error':False},
                                   {'status':'completed','future':1}])
def test_invalid_object_entry_grammar_is_preserved(tmp_path,entry):
    path=tmp_path/'pending-reports.json';path.write_text(json.dumps({'a':entry}));before=path.read_bytes()
    for loader in (load_pending_reports,load_pending_report_results):
        with pytest.raises(StateError):loader(tmp_path)
    with pytest.raises(StateError):record_pending_report(tmp_path,'a','completed',None)
    assert path.read_bytes()==before


def test_valid_empty_object_missing_file_and_legacy_string_statuses_are_preserved(tmp_path):
    assert load_pending_reports(tmp_path)=={} and load_pending_report_results(tmp_path)=={}
    path=tmp_path/'pending-reports.json';path.write_text('{}');before=path.read_bytes()
    assert load_pending_reports(tmp_path)=={} and load_pending_report_results(tmp_path)=={}
    assert path.read_bytes()==before
    path.write_text(json.dumps({'a':{'status':'legacy-string'}}))
    assert load_pending_reports(tmp_path)=={'a':('legacy-string',None)}
    assert load_pending_report_results(tmp_path)=={}


@pytest.mark.parametrize('raw',[ '{"a":{"status":"completed"},"a":null}',
                                  '{"a":{"status":"completed","status":"failed"}}' ])
def test_duplicate_json_keys_are_ambiguous_and_preserved(tmp_path,raw):
    path=tmp_path/'pending-reports.json';path.write_text(raw);before=path.read_bytes()
    with pytest.raises(StateError):load_pending_reports(tmp_path)
    with pytest.raises(StateError):record_pending_report(tmp_path,'a','completed',None)
    assert path.read_bytes()==before



def test_invalid_utf8_state_is_a_controlled_error_before_any_http(tmp_path):
    path=tmp_path/'pending-reports.json';path.write_bytes(b'\xff');before=path.read_bytes()
    agent,client=fixture(tmp_path,lambda request:pytest.fail('zero HTTP required'))
    with pytest.raises(StateError):agent.run_forever()
    assert agent.stop_event.is_set() and not agent.stats.started
    with pytest.raises(StateError):record_pending_report(tmp_path,'a','completed',None)
    assert path.read_bytes()==before
    client.close()
