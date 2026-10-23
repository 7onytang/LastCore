"""Real core + blocking Fake executor: no Tk, socket, model or real task compute."""
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from linear_edge import Session, LinearController, LinearNodeAgent
from lastcore_node_agent.config import AgentConfig
from lastcore_node_agent.credentials import acquire_state_lock
from lastcore_node_agent.executor import ExecutionCancelled
from test_gui_view import callback, fixture as view_fixture, Variable


@pytest.fixture
def stopped_session(tmp_path):
    entered=threading.Event();release=threading.Event();reports=[]
    row={'id':'assignment','task_id':'task','node_id':'fixture-node','position':0,
         'layer_start':0,'layer_end':1,'previous_node':None,'next_node':None,
         'status':'pending','task_status':'queued','deadline':1060.0,
         'request':{'model_id':'fake-blocking','num_layers':1,'layer_memory_mb':1}}
    def transport(request):
        if request.url.path.endswith('/register'):
            return httpx.Response(201,json={'node_id':'fixture-node','node_token':'fixture-token','heartbeat_ttl_seconds':30})
        if request.url.path.endswith('/heartbeat'):
            return httpx.Response(200,json={'accepting_tasks':json.loads(request.content)['accepting_tasks']})
        if request.url.path.endswith('/assignments'):
            return httpx.Response(200,json={'assignments':[row]})
        reports.append(json.loads(request.content)['status'])
        return httpx.Response(200,json={})
    class BlockingExecutor:
        def run(self,context):
            entered.set()
            assert release.wait(10),'test must release its blocking Fake executor'
            raise ExecutionCancelled(context.cancel_reason)
    session=Session.__new__(Session)
    session.config=AgentConfig('https://fixture.invalid','http://127.0.0.1:9',512,
        state_dir=tmp_path/'state',registration_token='fixture-registration',stop_grace=0,
        heartbeat_interval=.02,poll_interval=.01,watchdog_interval=.01)
    session.lock=acquire_state_lock(session.config.state_dir)
    assert session.lock is not None
    session.client=LinearController(session.config.controller_url,session.config.state_dir,
        transport=httpx.MockTransport(transport),publish=lambda value:None)
    session.close_lock=threading.Lock();session.closed=False
    session.observation_lock=threading.Lock()
    session.observation={'connection':'starting','admission':None,'recent_errors':[]}
    session.admission_request_id=None
    session.agent=LinearNodeAgent(session.config,session.client,clock=lambda:1000.0,
        executor_factory=BlockingExecutor,observer=session._observe)
    view=view_fixture();view['state']['session']=session
    view['work']=callback('work',view)
    thread=threading.Thread(target=view['work'],args=(session,))
    view['state']['thread']=thread
    thread.start()
    try:
        assert entered.wait(2),'core did not enter its Fake executor'
        run=session.agent._current_run()
        session.agent.shutdown();thread.join(3)
        assert not thread.is_alive() and session.has_live_execution()
        assert session.observation['connection']=='stopped'
        assert 'cleanup_timeout' in session.observation['recent_errors']
        yield SimpleNamespace(session=session,view=view,run=run,release=release,reports=reports)
    finally:
        release.set()
        session.agent.shutdown();thread.join(3)
        if session.agent._current_run() is not None:
            session.agent._current_run().worker.join(3)
        assert not session.has_live_execution()
        session.close()


def test_public_liveness_is_read_only_and_session_keeps_real_state_lock_and_client(stopped_session):
    value=stopped_session;session=value.session
    before=list(value.reports)
    assert session.agent.has_live_execution() and session.agent.has_live_execution()
    assert session.agent._current_run() is value.run and value.reports==before
    assert session.close() is False
    assert not session.client._client.is_closed and not session.lock.closed
    assert acquire_state_lock(session.config.state_dir) is None
    value.release.set();value.run.worker.join(3)
    assert not session.agent.has_live_execution()
    assert session.agent._current_run() is value.run  # Getter never reaps or clears references.
    assert session.close() is True and session.close() is True
    assert session.client._client.is_closed and session.lock.closed
    replacement=acquire_state_lock(session.config.state_dir)
    assert replacement is not None
    replacement.close()


def test_actual_start_refresh_and_stop_callbacks_keep_session_until_executor_exits(stopped_session):
    value=stopped_session;view=value.view;session=value.session
    attempts=[]
    def create(*args):attempts.append(args);raise AssertionError('must not create a second session')
    view['Session']=create
    callback('start',view)()
    assert attempts==[] and view['state']['session'] is session
    assert view['status'].value=='停止未确认 · 工作线程仍在运行'
    callback('refresh',view)()
    assert view['state']['stopping'] and view['task'].value=='task'
    assert view['status'].value=='停止未确认 · 工作线程仍在运行'
    assert view['admission'].value=='停止中，接单状态未确认'
    callback('stop',view)()
    assert not session.lock.closed and view['state']['session'] is session
    value.release.set();value.run.worker.join(3)
    callback('refresh',view)()
    assert view['status'].value=='已停止' and not view['state']['stopping']
    assert session.lock.closed
    new_session=view_fixture()['state']['session'];new_session.node_id='next-node'
    class Thread:
        def __init__(self,**kwargs):self.started=False
        def start(self):self.started=True
    view.update(Session=lambda *args:new_session,threading=SimpleNamespace(Thread=Thread),
                file=Variable('fake-config'),verified_origin=Variable('https://fixture.invalid'),node=Variable())
    callback('start',view)()
    assert view['state']['session'] is new_session and view['state']['thread'].started


def test_window_close_waits_for_actual_executor_not_only_the_finished_main_loop(stopped_session):
    value=stopped_session;view=value.view;scheduled=[];destroyed=[]
    view['root']=SimpleNamespace(after=lambda *args:scheduled.append(args),destroy=lambda:destroyed.append(True))
    view['stop']=callback('stop',view)
    close=callback('close',view);view['close']=close
    close()
    assert destroyed==[] and scheduled and view['status'].value=='等待本机停止确认'
    assert not value.session.lock.closed
    value.release.set();value.run.worker.join(3)
    close()
    assert destroyed==[True] and value.session.lock.closed
