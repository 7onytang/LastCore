"""Actual portable Session observer and controls with a Fake core, no Tk or sockets."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from linear_edge import Session
from view_state import admission_text,device_status,recent_error_text


def session():
    value=Session.__new__(Session)
    value.config=SimpleNamespace(controller_url='https://actual.invalid:8443/',available_memory_mb=512,compute_weight=3)
    value.observation_lock=threading.Lock()
    value.observation={'connection':'starting','admission':None,'recent_errors':[]}
    value.admission_request_id=None
    value.agent=SimpleNamespace(set_accepting_tasks=lambda desired,request_id:value._observe({'admission':{
        'desired':desired,'confirmed':None,'request_id':request_id,'confirmed_request_id':None,'status':'pending'}}))
    return value


def test_session_control_is_pending_without_stopping_or_using_network():
    value=session()
    identity=value.set_accepting_tasks(False)
    snapshot=value.status_snapshot()
    assert value.admission_request_id==identity
    assert admission_text(snapshot['admission'],identity)=='暂停接单待确认'
    assert snapshot['controller_origin']=='https://actual.invalid:8443'
    assert snapshot['available_memory_mb']==512 and snapshot['compute_weight']==3
    value._observe({'admission':{'desired':False,'confirmed':False,'request_id':identity,
        'confirmed_request_id':identity,'status':'confirmed'}})
    assert admission_text(value.status_snapshot()['admission'],identity)=='暂停接单 · 心跳已确认'


def test_same_boolean_from_old_ack_cannot_confirm_a_new_control():
    value={'desired':False,'confirmed':False,'request_id':'current','confirmed_request_id':'previous','status':'confirmed'}
    assert admission_text(value,'current')=='暂停接单待确认'
    value['confirmed_request_id']='current'
    assert admission_text(value,'different')=='暂停接单待确认'
    assert admission_text(value,'current',stopping=True)=='停止中，接单状态未确认'
    assert admission_text(value,'current',running=False)=='未运行'


def test_connection_and_admission_labels_never_contradict_latest_observation():
    value={'desired':False,'confirmed':False,'request_id':'pause','confirmed_request_id':'pause','status':'confirmed'}
    assert device_status(True,1,True,False,admission=value)=='已连接 · 暂停接单 · 心跳已确认'
    assert '连接未确认' in device_status(True,1,True,False,admission=value,connection='heartbeat-failed')


def test_observer_only_keeps_safe_error_codes_and_sanitized_admission():
    value=session()
    value._observe({'connection':'rejected','node_token':'private-token'})
    value._observe({'task_update':{'error_code':'private-provider-error'}})
    value._observe({'connection':'heartbeat-failed'})
    snapshot=value.status_snapshot()
    assert snapshot['recent_errors']==['rejected','heartbeat-failed']
    assert 'private-' not in str(snapshot)
    assert recent_error_text(snapshot['recent_errors'])=='凭据被拒绝；心跳失败'
    value._observe({'poll_status':'unavailable'})
    assert value.status_snapshot()['recent_errors'][-1]=='poll-unavailable'
    with pytest.raises(ValueError):value.set_accepting_tasks('false')


def test_real_core_setter_and_heartbeat_ack_flow_through_production_session_observer(tmp_path):
    import json
    import httpx
    from lastcore_node_agent.agent import NodeAgent
    from lastcore_node_agent.config import AgentConfig
    from lastcore_node_agent.controller import ControllerClient
    value=session();payloads=[]
    def transport(request):
        if request.url.path.endswith('/register'):
            return httpx.Response(201,json={'node_id':'fixture-node','node_token':'fixture-token','heartbeat_ttl_seconds':5})
        body=json.loads(request.content);payloads.append(body['accepting_tasks'])
        return httpx.Response(200,json={'accepting_tasks':body['accepting_tasks']})
    client=ControllerClient('https://actual.invalid:8443',transport=httpx.MockTransport(transport))
    value.config=AgentConfig('https://actual.invalid:8443','http://127.0.0.1:9',512,
        state_dir=tmp_path/'state',registration_token='fixture-registration')
    value.agent=NodeAgent(value.config,client,clock=lambda:1000.0,observer=value._observe)
    try:
        value.agent.ensure_credentials();value.agent._send_heartbeat()
        active=SimpleNamespace(context=SimpleNamespace(cancel=threading.Event()))
        value.agent._active=active
        pause=value.set_accepting_tasks(False)
        assert admission_text(value.status_snapshot()['admission'],pause)=='暂停接单待确认'
        assert value.agent._active is active and not active.context.cancel.is_set()
        value.agent._send_heartbeat()
        assert admission_text(value.status_snapshot()['admission'],pause)=='暂停接单 · 心跳已确认'
        resume=value.set_accepting_tasks(True)
        assert admission_text(value.status_snapshot()['admission'],resume)=='恢复接单待确认'
        value.agent._send_heartbeat()
        assert admission_text(value.status_snapshot()['admission'],resume)=='接收新任务 · 心跳已确认'
        assert payloads==[True,False,True] and not active.context.cancel.is_set()
    finally:
        value.agent._active=None;client.close()
