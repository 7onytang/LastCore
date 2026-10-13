import json
import time
from pathlib import Path
from lastcore_node_agent.local_status import LocalStatusWriter
from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.executor import SliceDemoExecutor
from tests.helpers import make_config,run_in_thread,wait_until


def test_status_file_allowlist_drops_secrets_payloads_and_provider_fields(tmp_path):
    path=tmp_path/'status.json'; writer=LocalStatusWriter(path)
    writer({'node_token':'secret-credential','origin':'private-origin','task':{
        'task_id':'own-task','assignment_id':'own-stage','model_id':'tiny',
        'node_token':'secret-credential','prompt':'private-prompt','error':'private-provider-path',
        'status':'completed','result':{'output_shape':[1,1,64],'local_seconds':.01,
            'parameter_bytes':200,'tokens':'private-input','generated_text':'private-output'}}})
    saved=path.read_text()
    assert 'secret-credential' not in saved and 'private-' not in saved
    assert json.loads(saved)['task']['result']['output_shape']==[1,1,64]
    assert path.stat().st_mode & 0o077 == 0
    writer({'task_update':{'assignment_id':'another-stage','status':'failed'}})
    assert json.loads(path.read_text())['task']['status']=='completed'


def test_observer_reports_local_completion_and_ack_without_touching_protocol(fake,client,clock,tmp_path):
    path=tmp_path/'ui-status.json'; writer=LocalStatusWriter(path)
    config=make_config(tmp_path/'private-state')
    agent=NodeAgent(config,client,clock=lambda:clock[0],
        executor_factory=lambda:SliceDemoExecutor(slices=1,slice_seconds=.01),observer=writer)
    thread,captured=run_in_thread(agent)
    try:
        assert wait_until(lambda:fake.register_calls==1)
        task_id,assignment_id=fake.add_task('node-1')
        assert wait_until(lambda:agent.stats.completed==[assignment_id])
        value=json.loads(path.read_text())
        assert value['last_heartbeat']==clock[0] and value['node_id']=='node-1'
        assert value['task']['task_id']==task_id and value['task']['status']=='completed'
        assert value['task']['cleanup_confirmed'] is True
        assert value['task']['report_status']=='acknowledged'
        assert 'node_token' not in path.read_text()
        assert [r[1] for r in fake.reports if r[0]==assignment_id]==['running','completed']
    finally:
        agent.shutdown();thread.join(5)
    assert not thread.is_alive() and 'error' not in captured


def test_observation_failure_cannot_stop_heartbeat_or_execution(fake,client,clock,tmp_path):
    def broken(updates): raise OSError('simulated observation storage failure')
    config=make_config(tmp_path/'state')
    agent=NodeAgent(config,client,clock=lambda:clock[0],
        executor_factory=lambda:SliceDemoExecutor(slices=1,slice_seconds=.01),observer=broken)
    thread,captured=run_in_thread(agent)
    try:
        assert wait_until(lambda:fake.register_calls==1)
        _,assignment=fake.add_task('node-1')
        assert wait_until(lambda:agent.stats.completed==[assignment])
    finally: agent.shutdown();thread.join(5)
    assert agent.stats.heartbeats_ok>0 and 'error' not in captured
