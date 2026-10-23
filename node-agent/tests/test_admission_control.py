"""Pure Fake controller/executor/time tests; no socket or model execution."""
import json
import os
from pathlib import Path
import threading
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.controller import ControllerClient
from lastcore_node_agent.local_control import ControlError, LocalControlReader, write_control_command
from tests.helpers import make_config


def test_stale_heartbeat_cannot_confirm_new_request(fake, client, clock, tmp_path):
    observations = []
    agent = NodeAgent(make_config(tmp_path), client, clock=lambda: clock[0], observer=observations.append)
    agent.ensure_credentials()
    agent._send_heartbeat()
    assert agent.admission_state()['status'] == 'confirmed'
    heartbeat = client.heartbeat
    def delayed_ack(*args, **kwargs):
        response = heartbeat(*args, **kwargs)
        agent.set_accepting_tasks(False, 'pause')
        agent.set_accepting_tasks(True, 'resume')
        return response
    client.heartbeat = delayed_ack
    agent._send_heartbeat()
    assert agent.admission_state() == {'desired': True, 'confirmed': True, 'request_id': 'resume',
        'confirmed_request_id': None, 'status': 'pending'}
    assert not agent._accepting_new_work()
    client.heartbeat = heartbeat
    agent._send_heartbeat()
    assert agent.admission_state()['confirmed_request_id'] == 'resume'
    assert agent.admission_state()['status'] == 'confirmed'
    agent.set_accepting_tasks(True, 'resume')
    assert agent.admission_state()['status'] == 'confirmed'
    with pytest.raises(ValueError): agent.set_accepting_tasks(False, 'resume')
    with pytest.raises(ValueError): agent.set_accepting_tasks(1)
    assert all('admission' in value for value in observations)


class RecordedStop:
    def __init__(self): self.stopped = False; self.waits = []
    def is_set(self): return self.stopped
    def set(self): self.stopped = True
    def wait(self, delay):
        self.waits.append(delay)
        if len(self.waits) == 8: self.stopped = True
        return self.stopped


def test_poll_backoff_caps_and_success_resets_without_changing_heartbeat(fake, client, tmp_path):
    agent = NodeAgent(make_config(tmp_path, poll_interval=1), client)
    stop = RecordedStop(); agent.stop_event = stop
    agent._join_controller = lambda: True
    agent._heartbeat_loop = lambda: None
    agent._watchdog_loop = lambda: None
    calls = []
    def poll():
        calls.append(True)
        if len(calls) != 7: raise httpx.ConnectError('fake outage')
    agent._poll_once = poll
    agent.run_forever()
    assert stop.waits == [2, 4, 8, 16, 30, 30, 1, 2]


def test_backoff_wait_is_interruptible_and_controls_have_independent_watchdog(fake, client, clock, tmp_path):
    path = tmp_path / 'command.json'
    tmp_path.chmod(0o700)
    agent = NodeAgent(make_config(tmp_path / 'state', poll_interval=30, watchdog_interval=.01),
        client, clock=lambda: clock[0], control_reader=LocalControlReader(path))
    agent.ensure_credentials(); agent._send_heartbeat()
    thread = threading.Thread(target=agent._watchdog_loop)
    thread.start()
    try:
        write_control_command(path, 'pause', False)
        from tests.helpers import wait_until
        assert wait_until(lambda: agent.admission_state()['request_id'] == 'pause', timeout=1)
        assert agent.admission_state()['desired'] is False
        # An actual 30-second Event.wait exits as soon as shutdown signals it.
        waiting = threading.Event(); done = threading.Event()
        def backoff():
            waiting.set(); agent.stop_event.wait(30); done.set()
        sleeper = threading.Thread(target=backoff); sleeper.start()
        assert waiting.wait(1)
        agent.shutdown()
        assert done.wait(1)
        sleeper.join(1)
    finally:
        agent.shutdown(); thread.join(1)
    assert not thread.is_alive()


def test_shutdown_timeout_retains_live_run_until_thread_exits(fake, client, clock, tmp_path):
    entered = threading.Event(); release = threading.Event()
    class DelayedExecutor:
        def run(self, context):
            entered.set()
            assert release.wait(3)
            from lastcore_node_agent.executor import ExecutionCancelled
            raise ExecutionCancelled(context.cancel_reason)
    agent = NodeAgent(make_config(tmp_path, stop_grace=0), client, clock=lambda: clock[0],
                      executor_factory=DelayedExecutor)
    agent.ensure_credentials(); agent._send_heartbeat()
    _, assignment = fake.add_task(agent.credentials.node_id)
    agent._poll_once(); assert entered.wait(1)
    run = agent._current_run()
    try:
        agent.shutdown(); agent._shutdown_execution()
        assert run.worker.is_alive() and agent._current_run() is run
        fake.add_task(agent.credentials.node_id)
        agent._poll_once()
        assert agent.stats.started == [assignment]
    finally:
        release.set(); run.worker.join(3)
        agent._reap_finished()
    assert agent._current_run() is None and agent.stats.stopped == [assignment]


def test_private_command_is_idempotent_and_rejects_reused_id(fake, client, tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / 'control.json'
    reader = LocalControlReader(path)
    agent = NodeAgent(make_config(tmp_path / 'state'), client)
    write_control_command(path, 'pause-1', False)
    assert reader.consume(agent) and not reader.consume(agent)
    assert agent.admission_state()['request_id'] == 'pause-1'
    write_control_command(path, 'pause-1', True)
    with pytest.raises(ControlError): reader.consume(agent)
    assert not agent.admission_state()['desired']


@pytest.mark.parametrize('raw', [
    '{}', 'null', '[]', '{"version":true,"request_id":"a","accepting_tasks":true}',
    '{"version":1,"request_id":"a","accepting_tasks":1}',
    '{"version":1,"request_id":"a","accepting_tasks":false,"extra":0}',
    '{"version":1,"request_id":"a","request_id":"b","accepting_tasks":true}',
    json.dumps({'version': 1, 'request_id': 'x' * 65, 'accepting_tasks': True}),
    json.dumps({'version': 1, 'request_id': '../bad', 'accepting_tasks': False}),
    'x' * 1025,
])
def test_bad_command_never_changes_admission(raw, client, tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / 'control.json'; path.write_text(raw); path.chmod(0o600)
    agent = NodeAgent(make_config(tmp_path / 'state'), client)
    before = agent.admission_state()
    with pytest.raises(ControlError): LocalControlReader(path).consume(agent)
    assert agent.admission_state() == before


def test_symlink_and_nonprivate_command_are_rejected(client, tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / 'control.json'
    target = tmp_path / 'target.json'
    write_control_command(target, 'pause', False)
    if os.name != 'nt':
        path.symlink_to(target)
        with pytest.raises(ControlError): LocalControlReader(path).consume(NodeAgent(make_config(tmp_path/'state'),client))
        path.unlink()
        target.chmod(0o644)
        with pytest.raises(ControlError): LocalControlReader(target).consume(NodeAgent(make_config(tmp_path/'state'),client))
        target.chmod(0o600); tmp_path.chmod(0o755)
        with pytest.raises(ControlError): write_control_command(target, 'resume', True)
    else:
        subprocess.run(['icacls', str(target), '/grant', '*S-1-1-0:(R)'], check=True, capture_output=True)
        with pytest.raises(ControlError): LocalControlReader(target).consume(NodeAgent(make_config(tmp_path/'state'),client))


def test_reparse_control_path_is_rejected_on_every_platform(monkeypatch, tmp_path):
    from lastcore_node_agent.local_control import _private_stat
    info = tmp_path.stat()
    monkeypatch.setattr(Path, 'lstat', lambda self: SimpleNamespace(
        st_mode=info.st_mode, st_file_attributes=0x400))
    with pytest.raises(ControlError): _private_stat(tmp_path, directory=True)


def test_cli_control_path_is_explicit_and_no_accept_matches_config(monkeypatch):
    from lastcore_node_agent.__main__ import build_parser
    monkeypatch.setenv('NODE_AGENT_NO_ACCEPT', '1')
    args = build_parser().parse_args(['--ui-control-file', '/private/control.json'])
    assert args.no_accept is True and args.ui_control_file == '/private/control.json'
