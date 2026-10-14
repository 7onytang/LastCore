import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from linear_edge import LinearController, LinearNodeAgent, MODEL, result_file
from view_state import device_status
from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.config import AgentConfig
from lastcore_node_agent.credentials import NodeCredentials,load_pending_reports,record_pending_report


def fixture(tmp_path,rows,reply=None):
    requests=[]
    def handler(request):
        requests.append(request)
        if request.method=='GET':return httpx.Response(200,json={'assignments':rows})
        return httpx.Response(200,json=reply or {'id':'task','assignments':[{'id':'old','status':'completed'}]})
    client=LinearController('https://example.test',tmp_path,transport=httpx.MockTransport(handler),publish=lambda *_:None)
    agent=LinearNodeAgent(AgentConfig('https://example.test','http://127.0.0.1:9000',128,state_dir=tmp_path,report_retries=1,retry_backoff=0),client)
    agent.credentials=NodeCredentials('node','fixture-private',30)
    client.on_recovery_required=agent.shutdown
    record_pending_report(tmp_path,'old','completed',None)
    agent._pending=load_pending_reports(tmp_path)
    return agent,client,requests


def test_old_stage_completion_without_cache_remains_status_only(tmp_path):
    agent,client,requests=fixture(tmp_path,[{'id':'old','request':{'model_id':'lastcore/tiny-cpu'}}])
    agent._flush_pending_reports()
    assert [r.method for r in requests]==['GET','POST']
    assert json.loads(requests[-1].content)=={'status':'completed','error':None}
    assert not load_pending_reports(tmp_path) and not client.recovery_required
    client.close()


@pytest.mark.parametrize('rows',[
    [{'id':'old','status':'running','request':{'model_id':MODEL}}],[],[{'id':'old','request':{}}],
    [{'id':'old','task_id':'task','status':'completed','request':{'model_id':MODEL},'layer_start':0,'layer_end':0,'result':[]}],
])
def test_missing_linear_or_unknown_outcome_stops_and_preserves_pending(tmp_path,rows):
    agent,client,requests=fixture(tmp_path,rows)
    before=(tmp_path/'pending-reports.json').read_bytes()
    agent._flush_pending_reports()
    assert all(r.method=='GET' for r in requests)
    assert client.recovery_required and agent.stop_event.is_set()
    assert (tmp_path/'pending-reports.json').read_bytes()==before
    assert load_pending_reports(tmp_path)=={'old':('completed',None)}
    client.close()


def test_completed_linear_can_replay_positive_server_saved_values(tmp_path):
    row={'id':'old','task_id':'task','status':'completed','request':{'model_id':MODEL},'layer_start':0,'layer_end':2,'result':[.1,.2]}
    agent,client,requests=fixture(tmp_path,[row],{'id':'task','assignments':[row]})
    agent._flush_pending_reports()
    assert json.loads(requests[-1].content)['result']==[.1,.2]
    assert not load_pending_reports(tmp_path) and client.last_result==[.1,.2]
    assert client.acknowledged_result == ('task',(.1,.2))
    client.last_result[0] = .9  # Compatibility list mutation cannot alter the view snapshot.
    assert client.acknowledged_result == ('task',(.1,.2))
    client.close()


def test_corrupt_cache_is_controlled_without_post_or_pending_loss(tmp_path):
    agent,client,requests=fixture(tmp_path,[])
    path=result_file(tmp_path,'old');path.parent.mkdir();path.write_text('{bad json')
    agent._flush_pending_reports()
    assert not requests and client.recovery_required and load_pending_reports(tmp_path)
    client.close()


def test_same_poll_missing_result_cannot_claim_or_launch_queued_work(tmp_path):
    import time
    rows=[{'id':'old','task_id':'old-task','task_status':'running','status':'running','request':{'model_id':MODEL}},
          {'id':'new','task_id':'new-task','task_status':'queued','status':'pending',
           'deadline':time.time()+100,'request':{'model_id':MODEL}}]
    agent,client,requests=fixture(tmp_path,rows)
    agent.executor_factory=lambda:pytest.fail('Recovery must stop before any executor is instantiated')
    before=(tmp_path/'pending-reports.json').read_bytes()
    agent._poll_once()
    assert agent.stop_event.is_set() and client.recovery_required
    assert not agent.stats.started and agent._current_run() is None
    assert all(request.method=='GET' for request in requests)
    assert (tmp_path/'pending-reports.json').read_bytes()==before
    client.close()


@pytest.mark.parametrize('kwargs,expected',[
    ({'stopping':True},'正在停止本机任务'),
    ({'accepting_tasks':False},'已连接 · 暂停接单'),
    ({'alive':False},'已停止'),
    ({'heartbeat_age':30},'连接中/心跳中断'),
    ({'recovery':True},'结果恢复待确认 · 待回报记录已保留'),
])
def test_device_labels_preserve_actual_lifecycle(kwargs,expected):
    options=dict(alive=True,heartbeat_age=1,accepting_tasks=True,stopping=False)
    options.update(kwargs)
    assert device_status(**options)==expected


def test_rejected_result_ack_does_not_publish_view_snapshot(tmp_path):
    row={'id':'old','task_id':'task','status':'completed','request':{'model_id':MODEL},
         'layer_start':0,'layer_end':2,'result':[.1,.2]}
    reply={'id':'task','assignments':[dict(row,result=[.3,.4])]}
    agent,client,requests=fixture(tmp_path,[row],reply)
    try:
        assert client.acknowledged_result is None
        with pytest.raises(RuntimeError,match='has not acknowledged'):
            client.report('node','fixture-private','old','completed')
        assert client.acknowledged_result is None
    finally:
        client.close()
