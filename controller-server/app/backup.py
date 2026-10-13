"""WAL-safe, read-only-source SQLite backups without service credentials."""
import argparse
from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time


class BackupTimeout(TimeoutError):
    """The backup exceeded its lock wait or overall time budget."""


def backup_database(database, output, *, timeout=60.0, lock_timeout=5.0):
    """Publish a verified snapshot to a new output file on a local filesystem.

    Timeout checks run between SQLite steps; blocked filesystem I/O cannot be
    interrupted. Output's parent must already exist and support hard links.
    """
    for value in (timeout, lock_timeout):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeouts must be finite positive seconds")
    started = time.monotonic()
    source = Path(database).resolve(strict=True)
    if not source.is_file():
        raise ValueError("source must be an existing database file")
    destination = Path(output).absolute()

    def check_target():
        for path in (destination, *(Path(str(destination) + suffix)
                                    for suffix in ("-wal", "-shm", "-journal"))):
            if os.path.lexists(path):
                raise FileExistsError("output or its SQLite sidecar already exists")

    def check_deadline():
        if time.monotonic() - started >= timeout:
            raise BackupTimeout("backup exceeded overall timeout")

    check_target()
    fd, name = tempfile.mkstemp(prefix=".sqlite-backup-", suffix=".sqlite3",
                                dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    busy_since = None

    def progress(status, remaining, total):
        nonlocal busy_since
        check_deadline()
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            now = time.monotonic()
            if busy_since is None:
                busy_since = now
            if now - busy_since >= lock_timeout:
                raise BackupTimeout("backup exceeded lock wait timeout")
        else:
            busy_since = None

    try:
        # URI encoding preserves spaces, question marks, # and non-ASCII paths.
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True,
                                     timeout=0)) as reader:
            with closing(sqlite3.connect(temporary, timeout=0)) as writer:
                reader.backup(writer, pages=128, progress=progress, sleep=0.01)
                check_deadline()
                # A backup inherits WAL mode. Convert only the copy so its main
                # file is independently readable without any WAL/SHM sidecars.
                writer.execute("PRAGMA journal_mode=DELETE")
                writer.set_progress_handler(
                    lambda: int(time.monotonic() - started >= timeout), 1000)
                try:
                    result = writer.execute("PRAGMA integrity_check").fetchall()
                except sqlite3.OperationalError as exc:
                    check_deadline()
                    raise exc
                if result != [("ok",)]:
                    raise sqlite3.DatabaseError("backup integrity check failed")
        check_deadline()
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
        check_deadline()
        check_target()
        # link is atomic and fails if a competing process creates the target.
        # rename/replace would overwrite a backup after a check-then-act race.
        os.link(temporary, destination)
        return destination
    finally:
        for path in (temporary, *(Path(str(temporary) + suffix)
                                 for suffix in ("-wal", "-shm", "-journal"))):
            path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a verified online SQLite backup")
    parser.add_argument("--database", required=True, help="existing source database")
    parser.add_argument("--output", required=True, help="new output file in an existing directory")
    parser.add_argument("--timeout", type=float, default=60.0, help="overall seconds (default: 60)")
    parser.add_argument("--lock-timeout", type=float, default=5.0, help="lock wait seconds (default: 5)")
    args = parser.parse_args(argv)
    try:
        backup_database(args.database, args.output, timeout=args.timeout,
                        lock_timeout=args.lock_timeout)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    print("Backup completed; integrity check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
