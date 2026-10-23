"""Credential admission/recovery fixtures. No real Controller or task execution."""
import json
import os
from pathlib import Path
import stat
from unittest.mock import Mock

import httpx
import pytest

from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.config import AgentConfig
from lastcore_node_agent.credentials import (
    NodeCredentials, StateError, load_credentials, save_credentials,
    REGISTRATION_PENDING_FILE, begin_registration, load_registration_pending,
)


def record():
    return {"node_id": "fixture-node", "node_token": "private-fixture-token",
            "heartbeat_ttl_seconds": 30}


BAD_TTLS = [None, False, True, 0, -1, 4, 3601, 30.0, 30.9, "30", "", [], {},
            float("inf"), float("-inf"), float("nan")]
BAD_RECORDS = [dict(record(), heartbeat_ttl_seconds=ttl) for ttl in BAD_TTLS]
BAD_RECORDS += [dict(record(), **{field: value})
                for field in ("node_id", "node_token") for value in (None, False, 7, [], {}, "")]
BAD_RECORDS += [{k: v for k, v in record().items() if k != field}
                for field in ("node_id", "node_token")]


def fixture_agent(tmp_path):
    client = Mock()
    client.register.return_value = record()
    for method in ("heartbeat", "assignments", "report", "report_result"):
        getattr(client, method).side_effect = AssertionError("must stop before contacting Controller")
    executor = Mock(side_effect=AssertionError("must not execute"))
    observer = Mock()
    agent = NodeAgent(AgentConfig("https://fixture.invalid", "http://127.0.0.1:9101", 128,
                                  state_dir=tmp_path, registration_token="registration-fixture"),
                      client, executor_factory=executor, observer=observer)
    return agent, client, executor, observer


@pytest.mark.parametrize("data", BAD_RECORDS)
def test_invalid_saved_credentials_stop_before_any_network_and_preserve_state(tmp_path, data):
    path = tmp_path / "node-credentials.json"
    original = json.dumps(data).encode()
    path.write_bytes(original)
    pending = tmp_path / "pending-reports.json"
    pending.write_text('{"a":{"status":"completed","error":null}}')
    saved_pending = pending.read_bytes()
    agent, client, executor, observer = fixture_agent(tmp_path)
    with pytest.raises(StateError) as error:
        agent.run_forever()
    assert agent.stop_event.is_set()
    assert agent.credentials is None
    observer.assert_called_with({"connection": "recovery-required", "admission": agent.admission_state()})
    assert client.mock_calls == []
    executor.assert_not_called()
    assert path.read_bytes() == original
    assert pending.read_bytes() == saved_pending
    assert "private-fixture-token" not in str(error.value)
    assert "fixture-node" not in str(error.value)


@pytest.mark.parametrize("data", BAD_RECORDS + [None, [], 17, "not an object"])
def test_invalid_registration_response_is_not_saved_or_followed_by_heartbeat(tmp_path, data):
    agent, client, executor, observer = fixture_agent(tmp_path)
    client.register.return_value = data
    with pytest.raises(StateError) as error:
        agent.run_forever()
    client.register.assert_called_once()
    assert [call[0] for call in client.mock_calls] == ["register"]
    assert agent.stop_event.is_set()
    assert agent.credentials is None
    observer.assert_called_with({"connection": "recovery-required", "admission": agent.admission_state()})
    executor.assert_not_called()
    assert not (tmp_path / "node-credentials.json").exists()
    assert [path.name for path in tmp_path.iterdir()] == [REGISTRATION_PENDING_FILE]
    assert "private-fixture-token" not in str(error.value)


@pytest.mark.parametrize("ttl", [5, 30, 3600])
def test_contract_boundaries_persist_and_restore_same_identity(tmp_path, ttl):
    agent, client, executor, _ = fixture_agent(tmp_path)
    client.register.return_value = dict(record(), heartbeat_ttl_seconds=ttl)
    first = agent.ensure_credentials()
    assert first == NodeCredentials("fixture-node", "private-fixture-token", ttl)
    assert load_credentials(tmp_path) == first
    restarted, other_client, _, _ = fixture_agent(tmp_path)
    assert restarted.ensure_credentials() == first
    assert other_client.mock_calls == []
    executor.assert_not_called()


def test_only_omitted_ttl_keeps_legacy_default(tmp_path):
    data = record()
    del data["heartbeat_ttl_seconds"]
    (tmp_path / "node-credentials.json").write_text(json.dumps(data))
    assert load_credentials(tmp_path).heartbeat_ttl_seconds == 30
    (tmp_path / "node-credentials.json").unlink()
    agent, client, _, _ = fixture_agent(tmp_path)
    client.register.return_value = data
    assert agent.ensure_credentials().heartbeat_ttl_seconds == 30


@pytest.mark.parametrize("ttl", BAD_TTLS)
def test_invalid_constructed_ttl_cannot_replace_existing_credentials(tmp_path, ttl):
    save_credentials(tmp_path, NodeCredentials("existing", "keep-private", 30))
    path = tmp_path / "node-credentials.json"
    original = path.read_bytes()
    with pytest.raises(StateError):
        save_credentials(tmp_path, NodeCredentials("new", "new-private", ttl))
    assert path.read_bytes() == original


def test_unknown_created_registration_is_never_posted_again_even_after_restart(tmp_path):
    from lastcore_node_agent.controller import ControllerClient
    created = []
    def lost_ack(request):
        assert request.url.path == '/v1/nodes/register'
        # Model the non-idempotent creation, then lose its response in transport.
        created.append('fixture-created-node')
        raise httpx.ReadTimeout('private-provider-error must not escape', request=request)
    client = ControllerClient('https://fixture.invalid', transport=httpx.MockTransport(lost_ack))
    source, _, executor, observer = fixture_agent(tmp_path)
    source.client = client
    try:
        for index, agent in enumerate((source, source, fixture_agent(tmp_path)[0])):
            agent.client = client
            with pytest.raises(StateError, match='outcome is unknown') as error:
                if index == 1:
                    agent.ensure_credentials()
                else:
                    agent.run_forever()
            assert 'private-provider-error' not in str(error.value)
            assert 'registration-fixture' not in str(error.value)
            assert agent.stop_event.is_set()
            assert created == ['fixture-created-node']
        assert load_registration_pending(tmp_path)
        assert observer.call_args.args[0]['connection'] == 'recovery-required'
        executor.assert_not_called()
    finally:
        client.close()


@pytest.mark.parametrize('status', [401, 403, 422, 500, 502])
def test_http_status_cannot_prove_registration_had_no_side_effect(tmp_path, status):
    from lastcore_node_agent.controller import ControllerError
    agent, client, _, _ = fixture_agent(tmp_path)
    client.register.side_effect = ControllerError(status, 'private-server-body')
    with pytest.raises(StateError, match='outcome is unknown') as error:
        agent.run_forever()
    assert 'private-server-body' not in str(error.value)
    assert load_registration_pending(tmp_path)
    restarted, other, _, _ = fixture_agent(tmp_path)
    with pytest.raises(StateError): restarted.run_forever()
    client.register.assert_called_once()
    assert other.mock_calls == []


@pytest.mark.parametrize('after_write', [False, True])
def test_identity_save_failure_keeps_intent_and_never_reposts(tmp_path, monkeypatch, after_write):
    import lastcore_node_agent.agent as implementation
    save = implementation.save_credentials
    def failed_save(directory, credentials):
        if after_write:
            save(directory, credentials)
        raise PermissionError('private path must not escape')
    monkeypatch.setattr(implementation, 'save_credentials', failed_save)
    agent, client, _, _ = fixture_agent(tmp_path)
    with pytest.raises(StateError, match='persistence failed') as error: agent.run_forever()
    assert 'private path' not in str(error.value)
    assert load_registration_pending(tmp_path)
    client.register.assert_called_once()
    restarted, other, _, _ = fixture_agent(tmp_path)
    if after_write:
        assert restarted.ensure_credentials() == NodeCredentials.from_record(record())
        assert not load_registration_pending(tmp_path)
    else:
        with pytest.raises(StateError, match='outcome is unknown'): restarted.run_forever()
    assert other.mock_calls == []


def test_cleanup_failure_preserves_saved_identity_then_recovers_without_register(tmp_path, monkeypatch):
    import lastcore_node_agent.agent as implementation
    clear = implementation.clear_registration_pending
    def failed_clear(directory): raise PermissionError('private path must not escape')
    monkeypatch.setattr(implementation, 'clear_registration_pending', failed_clear)
    agent, client, _, _ = fixture_agent(tmp_path)
    for _ in range(2):
        with pytest.raises(StateError, match='identity saved but intent cleanup failed'):
            agent.ensure_credentials()
    client.register.assert_called_once()
    identity = (tmp_path / 'node-credentials.json').read_bytes()
    assert load_registration_pending(tmp_path)
    monkeypatch.setattr(implementation, 'clear_registration_pending', clear)
    restarted, other, _, _ = fixture_agent(tmp_path)
    assert restarted.ensure_credentials() == NodeCredentials.from_record(record())
    assert not load_registration_pending(tmp_path)
    assert (tmp_path / 'node-credentials.json').read_bytes() == identity
    other.register.assert_not_called()
    other.heartbeat.side_effect = None
    other.heartbeat.return_value = {'accepting_tasks': True}
    restarted._send_heartbeat()
    other.heartbeat.assert_called_once()


@pytest.mark.parametrize('with_identity', [False, True])
@pytest.mark.parametrize('raw', [b'', b'null', b'[]', b'{', b'{"version":true,"phase":"pending"}',
    b'{"version":1,"phase":"done"}', b'{"version":1,"phase":"pending","extra":1}',
    b'{"version":1,"version":1,"phase":"pending"}'])
def test_corrupt_intent_preserves_bytes_and_refuses_all_network(tmp_path, with_identity, raw):
    if with_identity: save_credentials(tmp_path, NodeCredentials.from_record(record()))
    marker = tmp_path / REGISTRATION_PENDING_FILE
    marker.write_bytes(raw); marker.chmod(0o600)
    agent, client, _, _ = fixture_agent(tmp_path)
    with pytest.raises(StateError, match='intent is invalid'): agent.run_forever()
    assert marker.read_bytes() == raw
    assert client.mock_calls == []


def test_lost_known_intent_or_identity_never_falls_back_to_registration(tmp_path):
    from lastcore_node_agent.controller import ControllerError
    agent, client, _, _ = fixture_agent(tmp_path)
    client.register.side_effect = ControllerError(500, 'fixture ambiguous creation')
    with pytest.raises(StateError): agent.ensure_credentials()
    (tmp_path / REGISTRATION_PENDING_FILE).unlink()  # Simulate external data loss, not a recovery instruction.
    with pytest.raises(StateError, match='outcome is unknown'): agent.ensure_credentials()
    client.register.assert_called_once()
    other_dir = tmp_path / 'known'
    other, registered, _, _ = fixture_agent(other_dir)
    other.ensure_credentials()
    (other_dir / 'node-credentials.json').unlink()
    with pytest.raises(StateError, match='identity changed or disappeared'): other.ensure_credentials()
    registered.register.assert_called_once()


def test_loaded_intent_cannot_disappear_then_enable_post_in_the_same_agent(tmp_path):
    begin_registration(tmp_path)
    agent, client, _, _ = fixture_agent(tmp_path)
    with pytest.raises(StateError, match='outcome is unknown'): agent.ensure_credentials()
    (tmp_path / REGISTRATION_PENDING_FILE).unlink()
    with pytest.raises(StateError, match='outcome is unknown'): agent.ensure_credentials()
    assert client.mock_calls == []


def test_intent_is_exclusive_private_and_success_is_cleared_only_after_identity(tmp_path):
    begin_registration(tmp_path)
    path = tmp_path / REGISTRATION_PENDING_FILE
    before = path.read_bytes()
    with pytest.raises(StateError): begin_registration(tmp_path)
    assert path.read_bytes() == before
    assert load_registration_pending(tmp_path)
    assert not list(tmp_path.glob('.registration-stage-*'))
    if os.name != 'nt': assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('token', ['contains space', 'contains\nnewline', '非ASCII', 7])
def test_invalid_unsent_registration_input_never_creates_unknown_marker(tmp_path, token):
    from dataclasses import replace
    agent, client, _, _ = fixture_agent(tmp_path)
    agent.config = replace(agent.config, registration_token=token)
    with pytest.raises(StateError, match='valid local registration credential'): agent.run_forever()
    assert not load_registration_pending(tmp_path)
    assert client.mock_calls == []


def test_intent_write_failure_prevents_first_post(tmp_path, monkeypatch):
    import lastcore_node_agent.credentials as implementation
    def failed_write(*args): raise PermissionError('private path must not escape')
    monkeypatch.setattr(implementation, '_atomic_write_json', failed_write)
    agent, client, _, _ = fixture_agent(tmp_path)
    with pytest.raises(StateError, match='cannot reserve registration intent'): agent.run_forever()
    assert client.mock_calls == []


def test_existing_identity_offline_is_connection_retry_not_enrollment_recovery(tmp_path):
    save_credentials(tmp_path, NodeCredentials.from_record(record()))
    original = (tmp_path / 'node-credentials.json').read_bytes()
    agent, client, _, observer = fixture_agent(tmp_path)
    client.heartbeat.side_effect = httpx.ConnectError('fixture offline')
    waits = []
    def wait(delay):
        waits.append(delay)
        if len(waits) == 2: agent.shutdown()
        return agent.stop_event.is_set()
    agent.stop_event.wait = wait
    assert agent._join_controller() is False
    assert client.heartbeat.call_count == 2 and not client.register.called
    assert all(call.args[0]['connection'] == 'unreachable' for call in observer.call_args_list)
    assert (tmp_path / 'node-credentials.json').read_bytes() == original
    assert not load_registration_pending(tmp_path)


def test_registration_persists_identity_directory_before_unlinking_intent(tmp_path, monkeypatch):
    import lastcore_node_agent.agent as agent_implementation
    import lastcore_node_agent.credentials as state_implementation
    events = []
    agent, client, _, _ = fixture_agent(tmp_path)
    client.register.side_effect = lambda *args: (events.append('POST') or record())
    real_fsync, real_replace = state_implementation.os.fsync, state_implementation.os.replace
    real_read = agent_implementation.load_credentials
    real_sync = state_implementation._sync_registration_directory
    real_unlink = Path.unlink
    def fsync(fd):
        if 'POST' in events and not stat.S_ISDIR(os.fstat(fd).st_mode): events.append('file-fsync')
        return real_fsync(fd)
    def replace(source, target):
        if Path(target).name == 'node-credentials.json': events.append('replace')
        return real_replace(source, target)
    def read(directory):
        value = real_read(directory)
        if 'POST' in events: events.append('readback')
        return value
    def sync(directory):
        if 'POST' in events: events.append('directory-sync-barrier')
        return real_sync(directory)
    def unlink(path, *args, **kwargs):
        if path.name == REGISTRATION_PENDING_FILE: events.append('unlink-intent')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(state_implementation.os, 'fsync', fsync)
    monkeypatch.setattr(state_implementation.os, 'replace', replace)
    monkeypatch.setattr(agent_implementation, 'load_credentials', read)
    monkeypatch.setattr(state_implementation, '_sync_registration_directory', sync)
    monkeypatch.setattr(Path, 'unlink', unlink)
    agent.ensure_credentials()
    assert events == ['POST', 'file-fsync', 'replace', 'readback',
                      'directory-sync-barrier', 'unlink-intent', 'directory-sync-barrier']


def test_identity_directory_sync_failure_keeps_intent_on_initial_and_restart_paths(tmp_path, monkeypatch):
    import lastcore_node_agent.credentials as implementation
    sync = implementation._sync_registration_directory
    def failed_sync(directory):
        if (directory / 'node-credentials.json').exists():
            raise PermissionError('private directory detail must not escape')
        sync(directory)
    monkeypatch.setattr(implementation, '_sync_registration_directory', failed_sync)
    agent, client, _, _ = fixture_agent(tmp_path)
    with pytest.raises(StateError, match='intent cleanup failed') as error: agent.run_forever()
    assert 'private directory detail' not in str(error.value)
    marker = tmp_path / REGISTRATION_PENDING_FILE
    marker_bytes = marker.read_bytes()
    identity = (tmp_path / 'node-credentials.json').read_bytes()
    client.register.assert_called_once()
    restarted, other, _, _ = fixture_agent(tmp_path)
    with pytest.raises(StateError, match='intent cleanup failed'): restarted.run_forever()
    assert marker.read_bytes() == marker_bytes
    assert (tmp_path / 'node-credentials.json').read_bytes() == identity
    assert other.mock_calls == []
    monkeypatch.setattr(implementation, '_sync_registration_directory', sync)
    recovered, no_registration, _, _ = fixture_agent(tmp_path)
    assert recovered.ensure_credentials() == NodeCredentials.from_record(record())
    assert not marker.exists() and no_registration.mock_calls == []


def test_post_unlink_sync_failure_still_leaves_synchronized_identity_for_restart(tmp_path, monkeypatch):
    import lastcore_node_agent.credentials as implementation
    sync = implementation._sync_registration_directory
    events = []
    def failed_last_sync(directory):
        events.append('directory-sync')
        if len(events) == 3: raise OSError('fixture deletion persistence failure')
        sync(directory)
    monkeypatch.setattr(implementation, '_sync_registration_directory', failed_last_sync)
    agent, client, _, _ = fixture_agent(tmp_path)
    with pytest.raises(StateError, match='intent cleanup failed'): agent.run_forever()
    assert len(events) == 3 and not (tmp_path / REGISTRATION_PENDING_FILE).exists()
    assert load_credentials(tmp_path) == NodeCredentials.from_record(record())
    restarted, other, _, _ = fixture_agent(tmp_path)
    assert restarted.ensure_credentials() == agent.credentials
    assert other.mock_calls == [] and client.register.call_count == 1


def test_posix_directory_sync_closes_descriptor_even_when_fsync_fails(monkeypatch, tmp_path):
    import lastcore_node_agent.credentials as implementation
    from types import SimpleNamespace
    events = []
    def fsync(fd):
        events.append(('fsync', fd))
        raise OSError('fixture directory sync failure')
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='posix', O_RDONLY=0, O_DIRECTORY=65536,
        open=lambda directory, flags: (events.append(('open', directory, flags)) or 71),
        fsync=fsync, close=lambda fd: events.append(('close', fd))))
    with pytest.raises(OSError): implementation._sync_registration_directory(tmp_path)
    assert events == [('open', tmp_path, 65536), ('fsync', 71), ('close', 71)]
