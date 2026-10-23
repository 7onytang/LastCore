"""Real FastAPI routes/SQLite plus real NodeAgent client; in-process HTTP.

Ported from PR #27 (7458b15). Only executor work and clocks are controlled fakes.
The transport forwards requests to TestClient; it never manufactures API replies.
"""
import json
import logging
import platform
import socket
import threading
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.store import Store
from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.config import AgentConfig
from lastcore_node_agent.controller import ControllerClient, ControllerError
from lastcore_node_agent.executor import ExecutionCancelled

ADMIN = 'admin-' + 'a' * 32
REGISTRATION = 'registration-' + 'b' * 32
ORIGIN = 'http://testserver'


class AppTransport(httpx.BaseTransport):
    def __init__(self, app_client):
        self.app_client = app_client
        self.requests = []

    def handle_request(self, request):
        self.requests.append((request.method, request.url.path, request.content))
        reply = self.app_client.request(request.method, str(request.url),
                                        headers=request.headers, content=request.content)
        return httpx.Response(reply.status_code, headers=reply.headers,
                              content=reply.content, request=request)


class ControlledExecutor:
    """No model/compute: completion and cancellation use bounded event waits."""
    def __init__(self):
        self.started = threading.Event()
        self.complete = threading.Event()

    def run(self, context):
        self.started.set()
        for _ in range(200):
            if context.cancel.wait(0.01):
                raise ExecutionCancelled(context.cancel_reason)
            if self.complete.is_set():
                return
        raise AssertionError('test did not complete or cancel fake executor')


@pytest.fixture
def contract(tmp_path, monkeypatch):
    def no_bind(*_args, **_kwargs):
        pytest.fail('contract test attempted a socket bind')
    now = [1000.0]
    store = Store(str(tmp_path / 'controller.sqlite3'), 5, clock=lambda: now[0])
    settings = Settings(ADMIN, REGISTRATION, heartbeat_ttl_seconds=5)
    with TestClient(create_app(settings, store)) as admin:
        # Windows asyncio creates an internal loopback socketpair at portal startup.
        # The actual application requests below must never bind a server socket.
        monkeypatch.setattr(socket.socket, 'bind', no_bind)
        transport = AppTransport(admin)
        client = ControllerClient(ORIGIN, transport=transport)
        executor = ControlledExecutor()
        config = AgentConfig(ORIGIN, 'http://127.0.0.1:9', 1024,
                             state_dir=tmp_path / 'node-state', node_name='contract-node',
                             registration_token=REGISTRATION,
                             report_retries=1, retry_backoff=0)
        agent = NodeAgent(config, client, clock=lambda: now[0], executor_factory=lambda: executor)
        try:
            yield SimpleNamespace(admin=admin, transport=transport, client=client,
                                  agent=agent, executor=executor, now=now)
        finally:
            agent.shutdown()
            agent._shutdown_execution()
            client.close()


def admin_request(c, method, path, **kwargs):
    response = c.admin.request(method, path, headers={'Authorization': f'Bearer {ADMIN}'}, **kwargs)
    assert response.is_success, response.text
    return response.json()


def register(c):
    credentials = c.agent.ensure_credentials()
    c.agent._send_heartbeat()
    return credentials


def start(c):
    task = admin_request(c, 'POST', '/v1/tasks', json={
        'model_id': 'contract-model', 'num_layers': 4, 'layer_memory_mb': 1,
        'timeout_seconds': 120})
    assert task['status'] == 'queued' and len(task['assignments']) == 1
    c.agent._poll_once()
    assert c.executor.started.wait(1), 'fake executor did not start'
    current = admin_request(c, 'GET', f"/v1/tasks/{task['id']}")
    assert current['status'] == 'running'
    return current


def finish_worker(c):
    run = c.agent._current_run()
    # The preceding poll may already have reaped a promptly cancelled worker.
    if run is not None:
        run.worker.join(3)
        assert not run.worker.is_alive(), 'fake executor did not exit'
    c.agent._poll_once()


def test_registration_heartbeat_assignment_completion_and_idempotency(contract):
    c = contract
    credentials = register(c)
    nodes = admin_request(c, 'GET', '/v1/nodes')['nodes']
    assert len(nodes) == 1 and nodes[0]['name'] == 'contract-node'
    assert nodes[0]['available_memory_mb'] == 1024 and nodes[0]['status'] == 'available'
    assert credentials.heartbeat_ttl_seconds == 5 and c.agent.stats.heartbeats_ok == 1
    task = start(c)
    assignment = task['assignments'][0]['id']
    c.executor.complete.set()
    finish_worker(c)
    final = admin_request(c, 'GET', f"/v1/tasks/{task['id']}")
    assert final['status'] == final['assignments'][0]['status'] == 'completed'
    assert final['assignments'][0]['error'] is None
    assert c.agent.stats.completed == [assignment] and not c.agent.stats.failed
    replay = c.client.report(credentials.node_id, credentials.node_token, assignment, 'completed')
    assert replay['status'] == 'completed'
    reports = [json.loads(body)['status'] for method, path, body in c.transport.requests
               if method == 'POST' and path.endswith('/report')]
    assert reports == ['running', 'completed', 'completed']


def test_controller_error_status_and_details_reach_node_client(contract):
    c = contract
    payload = c.agent.config.registration_payload()
    with pytest.raises(ControllerError) as rejected:
        c.client.register('wrong-registration-token', payload)
    assert (rejected.value.status_code, rejected.value.detail) == (401, 'invalid registration credential')
    credentials = register(c)
    with pytest.raises(ControllerError) as rejected:
        c.client.heartbeat(credentials.node_id, 'wrong-node-token', 512, True)
    assert (rejected.value.status_code, rejected.value.detail) == (401, 'invalid node credential')
    with pytest.raises(ControllerError) as rejected:
        c.client.assignments('missing-node', credentials.node_token)
    assert (rejected.value.status_code, rejected.value.detail) == (401, 'invalid node credential')
    with pytest.raises(ControllerError) as rejected:
        c.client.report(credentials.node_id, credentials.node_token, 'missing-assignment', 'completed')
    assert (rejected.value.status_code, rejected.value.detail) == (404, 'assignment not found')
    assert c.client.heartbeat(credentials.node_id, credentials.node_token, 512, True)['status'] == 'available'


def test_controller_cancel_reaches_active_executor_via_assignments(contract):
    c = contract
    register(c)
    task = start(c)
    cancelled = admin_request(c, 'POST', f"/v1/tasks/{task['id']}/cancel")
    assert cancelled['status'] == 'cancelled'
    run = c.agent._current_run()
    c.agent._poll_once()
    assert run.context.cancel.is_set() and run.context.cancel_reason == 'task-terminal'
    finish_worker(c)
    final = admin_request(c, 'GET', f"/v1/tasks/{task['id']}")
    assert final['assignments'][0]['status'] == 'cancelled'
    assert c.agent.stats.stopped == [task['assignments'][0]['id']]
    assert not c.agent.stats.completed and not c.agent.stats.failed


def test_heartbeat_ttl_boundary_and_failure_detail(contract):
    c = contract
    register(c)
    task = start(c)
    c.now[0] += 4.999
    assert admin_request(c, 'GET', f"/v1/tasks/{task['id']}")['status'] == 'running'
    c.now[0] = 1005.0
    expired = admin_request(c, 'GET', f"/v1/tasks/{task['id']}")
    assert expired['status'] == 'failed'
    assert expired['error'] == 'assigned node heartbeat expired'
    assert expired['assignments'][0]['status'] == 'failed'
    c.agent._poll_once()
    finish_worker(c)
    assert c.agent.stats.stopped == [task['assignments'][0]['id']]
    assert not c.agent.stats.completed


def test_restart_reuses_private_identity_and_updates_resources_without_logging_token(contract, caplog):
    c = contract
    caplog.set_level(logging.INFO)
    original = register(c)
    registration_count = sum(path == '/v1/nodes/register' for _, path, _ in c.transport.requests)
    # A fresh agent/client with the same state dir must work without a registration token.
    config = replace(c.agent.config, registration_token='', available_memory_mb=512,
                     accepting_tasks=False)
    second_client = ControllerClient(ORIGIN, transport=c.transport)
    restarted = NodeAgent(config, second_client, clock=lambda: c.now[0],
                          executor_factory=lambda: c.executor)
    try:
        restored = restarted.ensure_credentials()
        assert restored.node_id == original.node_id
        assert restored.node_token == original.node_token
        c.now[0] += 1
        restarted._send_heartbeat()
        assert restarted.stats.heartbeats_ok == 1
        nodes = admin_request(c, 'GET', '/v1/nodes')['nodes']
        assert len(nodes) == 1 and nodes[0]['id'] == original.node_id
        assert nodes[0]['platform'] == platform.platform()
        assert nodes[0]['available_memory_mb'] == 512
        assert nodes[0]['accepting_tasks'] is False and nodes[0]['status'] == 'draining'
        assert nodes[0]['last_seen'] == c.now[0]
        assert sum(path == '/v1/nodes/register' for _, path, _ in c.transport.requests) == registration_count
        assert 'reusing persisted credentials' in caplog.text
        assert original.node_token not in caplog.text and REGISTRATION not in caplog.text
    finally:
        restarted.shutdown()
        second_client.close()
