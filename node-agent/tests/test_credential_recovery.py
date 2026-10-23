"""Credential admission/recovery fixtures. No real Controller or task execution."""
import json
from unittest.mock import Mock

import pytest

from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.config import AgentConfig
from lastcore_node_agent.credentials import (
    NodeCredentials, StateError, load_credentials, save_credentials,
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
    assert not list(tmp_path.iterdir())
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
