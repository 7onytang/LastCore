"""Pure view/Mock callback tests: never import Tk or create a Session/worker."""
import ast
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from view_state import device_status,task_result_labels,NO_CONFIRMED_RESULT,admission_text,recent_error_text

class Variable:
    def __init__(self,value=''):self.value=value
    def set(self,value):self.value=value
    def get(self):return self.value


def callback(name,context):
    source=Path(__file__).resolve().parents[1]/'gui.py'
    main=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='main')
    body=next(n for n in main.body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[body],type_ignores=[]),str(source),'exec'),context)
    return context[name]


def fixture(last_task=None,last_result=None,active=None):
    client=SimpleNamespace(last_heartbeat=100,last_task=last_task,last_result=last_result,recovery_required=False,
        acknowledged_result=(last_task,tuple(last_result)) if last_task and last_result is not None else None)
    session=SimpleNamespace(client=client,config=SimpleNamespace(accepting_tasks=True),
        agent=SimpleNamespace(_current_run=lambda:SimpleNamespace(task_id=active) if active else None))
    session.admission_request_id=None
    session.status_snapshot=lambda:{'controller_origin':'https://actual.invalid','available_memory_mb':512,'compute_weight':1,'cpu_logical':4,'admission':None,'recent_errors':[]}
    return {'state':{'session':session,'thread':SimpleNamespace(is_alive=lambda:True),'error':None,'stopping':False},
        'root':SimpleNamespace(after=lambda *args:None),'time':SimpleNamespace(monotonic=lambda:101),
        'task':Variable(),'result':Variable(),'status':Variable(),
        'actual_origin':Variable(),'resources':Variable(),'admission':Variable(),'recent_errors':Variable(),
        'admission_text':admission_text,'recent_error_text':recent_error_text,
        'device_status':device_status,'task_result_labels':task_result_labels,'NO_CONFIRMED_RESULT':NO_CONFIRMED_RESULT}


def test_running_b_keeps_completed_a_result_explicitly_owned():
    view=fixture('A',[.125,0],active='B')
    callback('refresh',view)()
    assert view['task'].value=='B'
    assert view['result'].value.startswith('已确认任务 A：')
    assert '0.12500000, 0.00000000' in view['result'].value
    assert '任务 B：' not in view['result'].value


def test_refresh_clears_previous_result_for_new_or_missing_session():
    view=fixture('A',[.5]);refresh=callback('refresh',view);refresh()
    assert '任务 A：' in view['result'].value
    view['state']['session']=fixture()['state']['session']
    refresh()
    assert view['task'].value=='空闲' and view['result'].value==NO_CONFIRMED_RESULT
    view['result'].set('stale-text');view['state']['session']=None
    refresh()
    assert view['result'].value==NO_CONFIRMED_RESULT


def test_new_session_clears_before_first_refresh_without_starting_worker():
    view=fixture('A',[.5]);view['task'].set('A');view['result'].set('A result')
    view['state']['thread']=None
    new_session=fixture()['state']['session'];new_session.node_id='mock-node'
    class Thread:
        def __init__(self,**kwargs):self.started=False
        def start(self):self.started=True  # Does not call target or launch a thread.
    view.update(Session=lambda *args:new_session,threading=SimpleNamespace(Thread=Thread),
        file=Variable('mock-config'),verified_origin=Variable('https://fixture.invalid'),node=Variable(),
        work=lambda *args:(_ for _ in ()).throw(AssertionError('must never execute a worker')))
    callback('start',view)()
    assert view['state']['session'] is new_session
    assert view['task'].value=='空闲' and view['result'].value==NO_CONFIRMED_RESULT


def test_missing_owner_or_values_cannot_reuse_acknowledged_text():
    for owner,values in [(None,[1]),('A',None),('A',[])]:
        assert task_result_labels('B',owner,values)==('B',NO_CONFIRMED_RESULT)
    task,result=task_result_labels(None,'A',[0]*17)
    assert task=='A' and result.startswith('已确认任务 A：')
    assert '前16/17项' in result


def test_gui_reads_atomic_binding_despite_mixed_legacy_attributes():
    view=fixture('A',[.125],active='B')
    client=view['state']['session'].client
    # Simulate B's old compatibility fields being published in separate steps.
    client.last_task='A';client.last_result=[.75]
    refresh=callback('refresh',view);refresh()
    assert view['task'].value=='B'
    assert view['result'].value=='已确认任务 A：0.12500000'
    # One replacement publishes both B's identity and immutable values together.
    client.acknowledged_result=('B',(.75,))
    refresh()
    assert view['result'].value=='已确认任务 B：0.75000000'


def test_stop_without_a_session_does_not_remain_stopping():
    view=fixture();view['state']['session']=None;view['state']['thread']=None
    callback('stop',view)()
    assert not view['state']['stopping']
    assert view['status'].value=='已停止' and view['admission'].value=='未运行'


def test_real_pause_resume_callbacks_only_change_admission_not_stop():
    view=fixture();calls=[]
    view['state']['session'].set_accepting_tasks=lambda value:calls.append(value)
    view['set_accepting']=callback('set_accepting',view)
    callback('pause',view)();callback('resume',view)()
    assert calls==[False,True] and not view['state']['stopping']
    assert view['admission'].value=='恢复接单待确认'


def test_refresh_shows_actual_session_origin_and_configured_resources():
    view=fixture();callback('refresh',view)()
    assert view['actual_origin'].value=='https://actual.invalid'
    assert '配置上报内存 512 MiB' in view['resources'].value
    assert '非占用测量' in view['resources'].value
