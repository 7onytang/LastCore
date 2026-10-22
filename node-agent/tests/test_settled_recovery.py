"""Damaged deduplication state must fence work, not silently allow replay."""
import pytest
from unittest.mock import Mock

from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.config import AgentConfig
from lastcore_node_agent.credentials import StateError


def agent_with_no_allowed_io(tmp_path):
    client = Mock()
    for method in ("register", "heartbeat", "assignments", "report", "report_result"):
        getattr(client, method).side_effect = AssertionError("network operation before recovery")
    executor = Mock(side_effect=AssertionError("execution before recovery"))
    observer = Mock()
    agent = NodeAgent(AgentConfig(controller_url="https://controller.invalid",
                                  endpoint="http://127.0.0.1:9101",
                                  available_memory_mb=1024, state_dir=tmp_path,
                                  registration_token="fixture-only"),
                      client, executor_factory=executor, observer=observer)
    return agent, client, executor, observer


@pytest.mark.parametrize("contents", ['{"settled":null}', '{"settled":[17]}',
                                      '{"settled":[{"id":"a","status":"running"}]}'])
def test_startup_corruption_stops_before_registration_or_execution(tmp_path, contents):
    path = tmp_path / "journal.json"
    path.write_text(contents)
    agent, client, executor, observer = agent_with_no_allowed_io(tmp_path)
    with pytest.raises(StateError):
        agent.run_forever()
    assert agent.stop_event.is_set()
    observer.assert_called_with({"connection": "recovery-required"})
    assert client.mock_calls == []
    executor.assert_not_called()
    assert path.read_text() == contents
    assert sorted(p.name for p in tmp_path.iterdir()) == ["journal.json"]


def test_corruption_during_settle_sets_recovery_and_preserves_pending(tmp_path):
    path = tmp_path / "journal.json"
    contents = '{"settled":[{"id":"old","status":"completed"},null]}'
    path.write_text(contents)
    pending = tmp_path / "pending-reports.json"
    saved = b'{"a":{"status":"completed","error":null}}'
    pending.write_bytes(saved)
    agent, client, executor, observer = agent_with_no_allowed_io(tmp_path)
    with pytest.raises(StateError):
        agent._settle("a", "completed")
    assert agent.stop_event.is_set()
    observer.assert_called_with({"connection": "recovery-required"})
    assert path.read_text() == contents
    assert pending.read_bytes() == saved
    assert client.mock_calls == []
    executor.assert_not_called()
