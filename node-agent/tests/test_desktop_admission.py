"""Production desktop state/control methods with a Fake child; no server or worker."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
import ui
from lastcore_node_agent.local_status import LocalStatusWriter, safe_admission


def ack(desired=True, request_id=None, confirmed_id=None, status='confirmed'):
    return dict(desired=desired, confirmed=desired, request_id=request_id,
                confirmed_request_id=confirmed_id, status=status)


@pytest.fixture
def node(tmp_path, monkeypatch):
    env = tmp_path / 'node.env'
    env.write_text('NODE_AGENT_EXECUTOR=demo\nNODE_AGENT_CONTROLLER_URL=https://controller.invalid:8443/\n'
                   'NODE_AGENT_AVAILABLE_MEMORY_MB=512\nNODE_AGENT_COMPUTE_WEIGHT=2\n'
                   'NODE_AGENT_REGISTRATION_TOKEN=private-registration\n')
    env.chmod(0o600)
    monkeypatch.setattr(ui, 'ResourceSampler', lambda: SimpleNamespace(sample=lambda: {'cpu_percent':None}))
    value = ui.NodeProcess(env)
    value.process = SimpleNamespace(poll=lambda:None)
    try:
        yield value
    finally:
        value.private_status.cleanup()


def observe(node, admission, **extra):
    node.status_path.write_text(json.dumps({'admission':admission, 'connection':'connected', **extra}))


def test_metadata_is_from_loaded_private_config_without_credentials(node):
    state = node.state()
    assert state['controller_origin'] == 'https://controller.invalid:8443'
    assert state['configured_resources'] == {'available_memory_mb':512,'compute_weight':2}
    assert 'private-registration' not in json.dumps(state)
    assert state['device']['cpu_percent'] is None


def test_pause_only_writes_private_command_until_matching_ack(node):
    observe(node, ack())
    initial_process = node.process
    state = node.set_accepting(False)
    command = json.loads(node.control_path.read_text())
    assert command['version'] == 1 and command['accepting_tasks'] is False
    identity = command['request_id']
    assert state['running'] and state['observation']['admission']['status'] == 'pending'
    assert state['observation']['admission']['request_id'] == identity
    assert state['observation']['admission']['desired'] is False
    assert node.process is initial_process and not node.stop_requested
    observe(node, ack(False,identity,identity))
    assert node.state()['observation']['admission']['status'] == 'confirmed'


def test_old_ack_and_mismatched_generation_cannot_confirm_resume(node):
    first = node.set_accepting(False)['observation']['admission']['request_id']
    second = node.set_accepting(True)['observation']['admission']['request_id']
    observe(node, ack(False, first, first))
    pending = node.state()['observation']['admission']
    assert pending['desired'] is True and pending['request_id'] == second and pending['status']=='pending'
    observe(node, ack(True,second,first))
    assert node.state()['observation']['admission']['status']=='pending'
    observe(node, ack(True,second,second))
    assert node.state()['observation']['admission']['status']=='confirmed'


def test_stopped_or_stopping_child_refuses_admission_commands(node):
    node.stop_requested = True
    with pytest.raises(ValueError):node.set_accepting(False)
    node.stop_requested = False
    node.process = None
    with pytest.raises(ValueError):node.set_accepting(True)
    assert not node.control_path.exists()


def test_writer_failure_does_not_lie_about_a_new_request(node, monkeypatch):
    from lastcore_node_agent import local_control
    def failed(*args):raise OSError('injected storage failure')
    monkeypatch.setattr(local_control,'write_control_command',failed)
    with pytest.raises(OSError):node.set_accepting(False)
    assert node.pending_admission is None


def test_restart_forwards_only_explicit_private_paths_and_discards_old_command(node, monkeypatch):
    node.process = None
    node.control_path.write_text('{}')
    node.status_path.write_text('{}')
    captured = {}
    def popen(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return SimpleNamespace(poll=lambda:None)
    monkeypatch.setattr(ui.subprocess,'Popen',popen)
    state = node.start()
    assert state['running'] and not node.control_path.exists() and not node.status_path.exists()
    assert captured['argv'][-4:] == ['--ui-status-file',str(node.status_path),'--ui-control-file',str(node.control_path)]
    assert 'private-registration' not in repr(captured)


def test_safe_admission_and_control_errors_do_not_leak_untrusted_fields(tmp_path):
    writer = LocalStatusWriter(tmp_path/'status.json')
    writer({'admission':{**ack(False,'pause','pause'),'token':'private-token'}, 'control_error':'invalid-command'})
    value = json.loads(writer.path.read_text())
    assert value['admission']['status']=='confirmed' and value['control_error']=='invalid-command'
    assert 'private-token' not in writer.path.read_text()
    writer({'admission':{**ack(),'desired':'private-token'}, 'control_error':'private-token'})
    assert json.loads(writer.path.read_text())['admission'] is None
    assert 'private-token' not in writer.path.read_text()
    assert safe_admission(ack(True,'next','previous'))['status']=='pending'


def test_recent_errors_are_bounded_safe_codes(node):
    for code in ['rejected','heartbeat-failed','rejected','unreachable']:
        observe(node,ack(),connection=code)
        node.state()
    state = node.state()
    assert state['recent_errors']==['rejected','heartbeat-failed','rejected','unreachable']
    observe(node,ack(),connection='private-provider-error',control_error='private-token')
    assert 'private-' not in json.dumps(node.state()['recent_errors'])
    observe(node,ack(),poll_status='unavailable')
    assert node.state()['recent_errors'][-1]=='poll-unavailable'


def test_real_http_dispatch_binds_session_and_origin_to_pause_route_without_a_listener(node, monkeypatch):
    captured={}
    def server(address,handler):
        captured['handler']=handler
        return SimpleNamespace(server_port=9123)
    monkeypatch.setattr(ui,'ThreadingHTTPServer',server)
    server,key=ui.make_server(node)
    handler=captured['handler'].__new__(captured['handler'])
    handler.server=server
    handler.path='/desktop/pause';handler.command='POST'
    handler.headers={'Host':'127.0.0.1:9123','Origin':'http://127.0.0.1:9123','X-LastCore-Session':key}
    responses=[]
    handler.send=lambda code,data,*args:responses.append((code,data))
    handler.dispatch()
    assert responses[-1][0]==200
    assert json.loads(responses[-1][1])['observation']['admission']['desired'] is False
    first=node.pending_admission['request_id']
    handler.headers['Origin']='https://other.invalid'
    handler.path='/desktop/resume';handler.dispatch()
    assert responses[-1][0]==403 and node.pending_admission['request_id']==first
    handler.headers['Origin']='http://127.0.0.1:9123'
    handler.headers['X-LastCore-Session']='invalid';handler.dispatch()
    assert responses[-1][0]==403 and node.pending_admission['request_id']==first


def test_actual_launcher_forwards_control_path_without_starting_a_child(node, monkeypatch):
    import os
    import run_agent
    monkeypatch.setattr(run_agent.os,'environ',dict(os.environ))
    monkeypatch.setattr(run_agent.sys,'argv',['run_agent.py'])
    captured={}
    def run_module(name,run_name):captured.update(name=name,argv=list(run_agent.sys.argv))
    monkeypatch.setattr(run_agent.runpy,'run_module',run_module)
    assert run_agent.main(['--env-file',str(node.env_file),'--ui-status-file',str(node.status_path),
        '--ui-control-file',str(node.control_path)])==0
    assert captured['name']=='lastcore_node_agent'
    assert captured['argv'][-4:]==['--ui-status-file',str(node.status_path),'--ui-control-file',str(node.control_path)]


def test_actual_core_reader_observer_and_parent_confirm_only_after_real_heartbeat_ack(node,tmp_path):
    import httpx
    from lastcore_node_agent.agent import NodeAgent
    from lastcore_node_agent.config import AgentConfig
    from lastcore_node_agent.controller import ControllerClient
    from lastcore_node_agent.local_control import LocalControlReader
    calls=[]
    def transport(request):
        if request.url.path.endswith('/register'):
            return httpx.Response(201,json={'node_id':'fixture-node','node_token':'fixture-token','heartbeat_ttl_seconds':5})
        payload=json.loads(request.content)
        calls.append(payload['accepting_tasks'])
        return httpx.Response(200,json={'accepting_tasks':payload['accepting_tasks']})
    client=ControllerClient('https://fixture.invalid',transport=httpx.MockTransport(transport))
    config=AgentConfig('https://fixture.invalid','http://127.0.0.1:9',512,
        state_dir=tmp_path/'state',registration_token='fixture-registration')
    agent=NodeAgent(config,client,clock=lambda:1000.0,observer=LocalStatusWriter(node.status_path),
        control_reader=LocalControlReader(node.control_path))
    try:
        agent.ensure_credentials();agent._send_heartbeat()
        assert node.state()['observation']['admission']['status']=='confirmed'
        requested=node.set_accepting(False)['observation']['admission']['request_id']
        agent._consume_controls()
        assert node.state()['observation']['admission']['status']=='pending'
        agent._send_heartbeat()
        value=node.state()['observation']['admission']
        assert value['status']=='confirmed' and value['confirmed'] is False
        assert value['confirmed_request_id']==requested and calls==[True,False]
        assert not node.stop_requested and not agent.stop_event.is_set()
    finally:
        client.close()
