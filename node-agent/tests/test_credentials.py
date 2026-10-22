import os
import json
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

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

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]

_CHILD_LOCK_PROBE = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, sys.argv[2])
    from lastcore_node_agent.credentials import acquire_state_lock

    handle = acquire_state_lock(Path(sys.argv[1]))
    print("LOCKED" if handle is not None else "BLOCKED")
    """
)


def _probe_lock_in_subprocess(state_dir: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_LOCK_PROBE, str(state_dir), str(_PACKAGE_ROOT)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


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


def test_state_lock_excludes_other_process(tmp_path):
    held = acquire_state_lock(tmp_path)
    assert held is not None
    try:
        assert _probe_lock_in_subprocess(tmp_path) == "BLOCKED"
    finally:
        held.close()
    assert _probe_lock_in_subprocess(tmp_path) == "LOCKED"


def test_credentials_roundtrip(tmp_path):
    creds = NodeCredentials("node-1", "tok", 30)
    save_credentials(tmp_path, creds)
    assert load_credentials(tmp_path) == creds


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX mode bits are not enforced by os.chmod on Windows")
def test_credentials_file_permissions(tmp_path):
    save_credentials(tmp_path, NodeCredentials("n", "t", 30))
    mode = stat.S_IMODE(os.stat(tmp_path / "node-credentials.json").st_mode)
    assert mode == 0o600


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX mode bits are not enforced by os.chmod on Windows")
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


INVALID_SETTLED_JOURNALS = [
    {}, {"settled": None}, {"settled": False}, {"settled": "completed"},
    {"settled": {}}, {"settled": [], "future_version": 2},
    {"settled": [None]}, {"settled": [17]}, {"settled": [{}]},
    {"settled": [{"id": "a"}]}, {"settled": [{"status": "completed"}]},
    {"settled": [{"id": "", "status": "completed"}]},
    {"settled": [{"id": 17, "status": "completed"}]},
    {"settled": [{"id": "a", "status": None}]},
    {"settled": [{"id": "a", "status": []}]},
    {"settled": [{"id": "a", "status": "running"}]},
    {"settled": [{"id": "a", "status": "future-terminal"}]},
    {"settled": [{"id": "a", "status": "completed", "unknown": True}]},
    {"settled": [{"id": "done", "status": "completed"}, 17]},
    {"settled": [{"id": str(i), "status": "completed"} for i in range(513)]},
]


@pytest.mark.parametrize("data", INVALID_SETTLED_JOURNALS)
def test_invalid_settled_journal_preserved_on_read_and_write(tmp_path, data):
    path = tmp_path / "journal.json"
    original = json.dumps(data).encode()
    path.write_bytes(original)
    with pytest.raises(StateError):
        load_settled(tmp_path)
    with pytest.raises(StateError):
        settle_journal(tmp_path, "new-task", "completed")
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["journal.json"]


@pytest.mark.parametrize("contents", ["null", "[]", "{bad-json", '{"settled":[],"settled":[]}'])
def test_unreadable_settled_journal_preserved(tmp_path, contents):
    path = tmp_path / "journal.json"
    path.write_text(contents)
    with pytest.raises(StateError):
        load_settled(tmp_path)
    with pytest.raises(StateError):
        settle_journal(tmp_path, "new-task", "completed")
    assert path.read_text() == contents


def test_all_existing_terminal_statuses_and_duplicate_ids_are_supported(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text(json.dumps({"settled": [
        {"id": "a", "status": "failed"}, {"id": "b", "status": "stopped"},
        {"id": "c", "status": "skipped-terminal"}, {"id": "a", "status": "completed"},
    ]}))
    assert load_settled(tmp_path) == {"a": "completed", "b": "stopped", "c": "skipped-terminal"}
    settle_journal(tmp_path, "d", "completed")
    assert load_settled(tmp_path)["a"] == "completed"
    assert load_settled(tmp_path)["d"] == "completed"


def test_absent_and_explicit_empty_settled_journal(tmp_path):
    assert load_settled(tmp_path) == {}
    (tmp_path / "journal.json").write_text('{"settled":[]}')
    assert load_settled(tmp_path) == {}
    settle_journal(tmp_path, "a", "completed")
    assert load_settled(tmp_path) == {"a": "completed"}


@pytest.mark.parametrize("assignment_id,status", [("", "completed"), (None, "completed"),
                                                  ("a", "running"), ("a", [])])
def test_invalid_settled_write_preserves_valid_journal(tmp_path, assignment_id, status):
    settle_journal(tmp_path, "existing", "stopped")
    path = tmp_path / "journal.json"
    original = path.read_bytes()
    with pytest.raises(StateError):
        settle_journal(tmp_path, assignment_id, status)
    assert path.read_bytes() == original


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
