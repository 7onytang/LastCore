from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from app.backup import BackupTimeout, backup_database


def make_database(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE values_table (value INTEGER)")
        db.execute("INSERT INTO values_table VALUES (42)")
        db.commit()


def assert_no_temporary_files(directory):
    assert not list(directory.glob(".sqlite-backup-*"))


def test_live_wal_snapshot_is_complete_and_source_is_unchanged(tmp_path):
    source = tmp_path / "数据库 ?# live.sqlite3"
    output = tmp_path / "snapshot.sqlite3"
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE values_table (value INTEGER)")
        writer.execute("INSERT INTO values_table VALUES (42)")
        writer.commit()
        source_files = [source, Path(str(source) + "-wal")]
        before = [path.read_bytes() for path in source_files]
        writer.execute("INSERT INTO values_table VALUES (99)")
        backup_database(source, output)
        assert [path.read_bytes() for path in source_files] == before
        assert not Path(str(output) + "-wal").exists()
        assert not Path(str(output) + "-shm").exists()
        with closing(sqlite3.connect(output.as_uri() + "?mode=ro", uri=True)) as copy:
            assert copy.execute("SELECT * FROM values_table").fetchall() == [(42,)]
            assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        writer.rollback()
    source.unlink()
    with closing(sqlite3.connect(output)) as copy:
        assert copy.execute("SELECT * FROM values_table").fetchall() == [(42,)]
    assert output.stat().st_mode & 0o777 == 0o600
    assert_no_temporary_files(tmp_path)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_refuses_existing_output_or_sidecar(tmp_path, suffix):
    source, output = tmp_path / "source.db", tmp_path / "backup.db"
    make_database(source)
    protected = Path(str(output) + suffix)
    protected.write_bytes(b"preserve existing bytes")
    with pytest.raises(FileExistsError):
        backup_database(source, output)
    assert protected.read_bytes() == b"preserve existing bytes"
    assert_no_temporary_files(tmp_path)


def test_refuses_same_source_and_output(tmp_path):
    source = tmp_path / "source.db"
    make_database(source)
    before = source.read_bytes()
    with pytest.raises(FileExistsError):
        backup_database(source, source)
    assert source.read_bytes() == before


def test_missing_source_does_not_create_database(tmp_path):
    source, output = tmp_path / "typo.db", tmp_path / "backup.db"
    with pytest.raises(FileNotFoundError):
        backup_database(source, output)
    assert not source.exists()
    assert not output.exists()
    assert_no_temporary_files(tmp_path)


def test_corrupt_source_cleans_partial_backup(tmp_path):
    source, output = tmp_path / "corrupt.db", tmp_path / "backup.db"
    source.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, output)
    assert not output.exists()
    assert source.read_bytes() == b"not a SQLite database"
    assert_no_temporary_files(tmp_path)


def test_integrity_check_rejects_invalid_copy_before_publication(tmp_path):
    source, output = tmp_path / "invalid.db", tmp_path / "backup.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE data (value INTEGER CHECK(value > 0))")
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO data VALUES (-1)")
        db.commit()
    # The backup API can copy every page successfully; integrity_check must
    # still reject the resulting invalid data before publishing any file.
    with pytest.raises(sqlite3.DatabaseError, match="integrity check failed"):
        backup_database(source, output)
    assert not output.exists()
    assert_no_temporary_files(tmp_path)


@pytest.mark.parametrize("timeout, lock_timeout, message", [
    (2, 0.05, "lock wait"), (0.05, 2, "overall"),
])
def test_exclusive_source_lock_is_bounded(tmp_path, timeout, lock_timeout, message):
    source, output = tmp_path / "source.db", tmp_path / "backup.db"
    make_database(source)
    with closing(sqlite3.connect(source)) as locker:
        locker.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with pytest.raises(BackupTimeout, match=message):
            backup_database(source, output, timeout=timeout, lock_timeout=lock_timeout)
        assert time.monotonic() - start < 1
        locker.rollback()
    assert not output.exists()
    assert_no_temporary_files(tmp_path)


def test_publication_race_does_not_overwrite_existing_file(tmp_path, monkeypatch):
    source, output = tmp_path / "source.db", tmp_path / "backup.db"
    make_database(source)
    real_link = os.link

    def competing_link(src, dst):
        Path(dst).write_bytes(b"concurrent backup")
        return real_link(src, dst)

    monkeypatch.setattr(os, "link", competing_link)
    with pytest.raises(FileExistsError):
        backup_database(source, output)
    assert output.read_bytes() == b"concurrent backup"
    assert_no_temporary_files(tmp_path)


def test_publication_failure_cleans_partial_backup(tmp_path, monkeypatch):
    source, output = tmp_path / "source.db", tmp_path / "backup.db"
    make_database(source)

    def unavailable_link(src, dst):
        raise OSError("filesystem does not support hard links")

    monkeypatch.setattr(os, "link", unavailable_link)
    with pytest.raises(OSError):
        backup_database(source, output)
    assert not output.exists()
    assert_no_temporary_files(tmp_path)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_rejects_unbounded_timeout(tmp_path, value):
    with pytest.raises(ValueError):
        backup_database(tmp_path / "source.db", tmp_path / "output.db", timeout=value)
    assert list(tmp_path.iterdir()) == []


def test_cli_uses_no_controller_credentials(tmp_path):
    source, output = tmp_path / "source.db", tmp_path / "backup.db"
    make_database(source)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("CONTROLLER_")}
    args = [sys.executable, "-m", "app.backup", "--database", str(source),
            "--output", str(output)]
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "integrity check passed" in result.stdout
    again = subprocess.run(args, env=env, capture_output=True, text=True, timeout=5)
    assert again.returncode == 1
    assert "already exists" in again.stderr
    assert "Traceback" not in again.stderr
    assert_no_temporary_files(tmp_path)
