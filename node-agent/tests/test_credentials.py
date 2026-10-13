import os
import stat

import pytest

from lastcore_node_agent.credentials import (
    NodeCredentials,
    StateError,
    acquire_state_lock,
    clear_pending_report,
    load_credentials,
    load_pending_reports,
    load_settled,
    record_pending_report,
    save_credentials,
    settle_journal,
)


def test_state_lock_is_exclusive(tmp_path):
    first = acquire_state_lock(tmp_path)
    assert first is not None
    try:
        assert acquire_state_lock(tmp_path) is None
    finally:
        first.close()
    second = acquire_state_lock(tmp_path)
    assert second is not None
    second.close()


def test_credentials_roundtrip(tmp_path):
    creds = NodeCredentials("node-1", "tok", 30)
    save_credentials(tmp_path, creds)
    assert load_credentials(tmp_path) == creds


def test_credentials_file_permissions(tmp_path):
    save_credentials(tmp_path, NodeCredentials("n", "t", 30))
    mode = stat.S_IMODE(os.stat(tmp_path / "node-credentials.json").st_mode)
    assert mode == 0o600


def test_state_dir_permissions(tmp_path):
    save_credentials(tmp_path, NodeCredentials("n", "t", 30))
    mode = stat.S_IMODE(os.stat(tmp_path).st_mode)
    assert mode & 0o077 == 0


def test_missing_credentials_returns_none(tmp_path):
    assert load_credentials(tmp_path) is None


def test_corrupted_credentials_raise(tmp_path):
    (tmp_path / "node-credentials.json").write_text("{not json")
    with pytest.raises(StateError):
        load_credentials(tmp_path)


def test_invalid_credentials_raise(tmp_path):
    (tmp_path / "node-credentials.json").write_text('{"node_id": ""}')
    with pytest.raises(StateError):
        load_credentials(tmp_path)


def test_journal_settle_and_load(tmp_path):
    for index in range(5):
        settle_journal(tmp_path, f"a-{index}", "completed")
    assert load_settled(tmp_path) == {f"a-{index}": "completed" for index in range(5)}


def test_journal_prunes_to_limit(tmp_path):
    for index in range(700):
        settle_journal(tmp_path, f"a-{index}", "completed")
    settled = load_settled(tmp_path)
    assert len(settled) == 512
    assert "a-0" not in settled
    assert "a-699" in settled


def test_journal_last_status_wins(tmp_path):
    settle_journal(tmp_path, "a", "completed")
    settle_journal(tmp_path, "a", "failed")
    assert load_settled(tmp_path) == {"a": "failed"}


def test_pending_report_missing_file(tmp_path):
    assert load_pending_reports(tmp_path) == {}


def test_pending_report_roundtrip(tmp_path):
    record_pending_report(tmp_path, "a-1", "completed", None)
    record_pending_report(tmp_path, "a-2", "failed", "ValueError: boom")
    assert load_pending_reports(tmp_path) == {
        "a-1": ("completed", None),
        "a-2": ("failed", "ValueError: boom"),
    }


def test_pending_report_clear(tmp_path):
    record_pending_report(tmp_path, "a-1", "failed", "boom")
    clear_pending_report(tmp_path, "a-1")
    assert load_pending_reports(tmp_path) == {}
    clear_pending_report(tmp_path, "missing")
    assert load_pending_reports(tmp_path) == {}


def test_pending_report_record_overwrites(tmp_path):
    record_pending_report(tmp_path, "a-1", "failed", "boom")
    record_pending_report(tmp_path, "a-1", "completed", None)
    assert load_pending_reports(tmp_path) == {"a-1": ("completed", None)}


def test_pending_report_corrupted_raises(tmp_path):
    (tmp_path / "pending-reports.json").write_text("{not json")
    with pytest.raises(StateError):
        load_pending_reports(tmp_path)
    with pytest.raises(StateError):
        record_pending_report(tmp_path, "a-1", "failed", None)


def test_pending_report_invalid_entry_raises(tmp_path):
    (tmp_path / "pending-reports.json").write_text('{"a-1": {"error": "no status"}}')
    with pytest.raises(StateError):
        load_pending_reports(tmp_path)
