"""Regression tests for the demo state file's security and failure contract.

These tests exercise the filesystem boundary around ``integration.demo`` rather
than asserting a particular helper implementation.  In particular, the mode
check observes the file immediately after creation, before the public helper's
return value can hide an insecure creation window.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import stat
from pathlib import Path

import pytest

from integration import demo


def _state(token: str, **extra: object) -> dict:
    value = {"version": 1, "tokens": {"admin": token, "registration": "registration"}}
    value.update(extra)
    return value


def test_new_state_is_private_from_first_observable_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A permissive umask must not expose a newly-created state file."""

    observed_modes: list[int] = []
    observed_open_modes: list[int] = []
    real_chmod = demo.os.chmod
    real_open = demo.os.open

    def observe_before_chmod(path: str | os.PathLike[str], mode: int, *args, **kwargs) -> None:
        observed_modes.append(stat.S_IMODE(Path(path).stat().st_mode))
        real_chmod(path, mode, *args, **kwargs)

    def observe_open(path, flags, mode=0o777, *args, **kwargs):
        if Path(path).parent == tmp_path:
            observed_open_modes.append(stat.S_IMODE(mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(demo.os, "chmod", observe_before_chmod)
    monkeypatch.setattr(demo.os, "open", observe_open)
    previous_umask = os.umask(0)
    try:
        path = demo.write_state(tmp_path, _state("new-secret"))
    finally:
        os.umask(previous_umask)

    assert path.stat().st_mode & 0o077 == 0
    assert observed_modes or observed_open_modes, "creation must be observable through chmod or os.open"
    if observed_modes:
        assert observed_modes[0] & 0o077 == 0
    if observed_open_modes:
        assert all(mode & 0o077 == 0 for mode in observed_open_modes)


def test_state_overwrite_remains_complete_and_readable(tmp_path: Path) -> None:
    first = _state("first-secret", processes=[], nodes={})
    second = _state("second-secret", processes=[{"pid": 123}], nodes={"a": {}})

    demo.write_state(tmp_path, first)
    demo.write_state(tmp_path, second)

    assert demo.read_state(tmp_path) == second
    assert json.loads((tmp_path / demo.STATE_NAME).read_text(encoding="utf-8")) == second
    assert stat.S_IMODE((tmp_path / demo.STATE_NAME).stat().st_mode) == 0o600


def test_replace_failure_preserves_old_state_and_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _state("old-secret", processes=[], nodes={})
    demo.write_state(tmp_path, old)
    before = {entry.name for entry in tmp_path.iterdir()}

    def fail_replace(*_args, **_kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(demo.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        demo.write_state(tmp_path, _state("new-secret", processes=[{"pid": 456}], nodes={}))

    assert demo.read_state(tmp_path) == old
    assert {entry.name for entry in tmp_path.iterdir()} == before
    assert "new-secret" not in (tmp_path / demo.STATE_NAME).read_text(encoding="utf-8")


def test_write_failure_preserves_old_state_and_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _state("old-secret", processes=[], nodes={})
    demo.write_state(tmp_path, old)
    before = {entry.name for entry in tmp_path.iterdir()}
    old_bytes = (tmp_path / demo.STATE_NAME).read_bytes()
    new = _state("new-secret", processes=[{"pid": 789}], nodes={})
    new_token = new["tokens"]["admin"]
    write_fds: set[int] = set()
    injection_hits = 0
    partial_write_observed = False
    real_io_open = io.open
    real_builtin_open = builtins.open
    real_fdopen = os.fdopen
    real_os_open = os.open
    real_os_write = os.write

    def is_write_flags(flags: int) -> bool:
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))

    def track_open(path, flags, *args, **kwargs):
        fd = real_os_open(path, flags, *args, **kwargs)
        if is_write_flags(flags):
            try:
                if Path(path).parent == tmp_path:
                    write_fds.add(fd)
            except (TypeError, ValueError):
                pass
        return fd

    class PartialWriteStream:
        def __init__(self, stream):
            self._stream = stream

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def write(self, data):
            nonlocal injection_hits, partial_write_observed
            if injection_hits:
                return self._stream.write(data)
            size = max(1, len(data) // 2)
            written = self._stream.write(data[:size])
            self._stream.flush()
            injection_hits += 1
            partial_write_observed = written == size
            raise OSError("injected partial write failure")

        def __getattr__(self, name):
            return getattr(self._stream, name)

    def wrap_stream(stream, in_scope: bool, mode: str):
        if in_scope and any(flag in mode for flag in ("w", "a", "x", "+")):
            return PartialWriteStream(stream)
        return stream

    def inject_text_write(file, mode="r", *args, **kwargs):
        stream = real_io_open(file, mode, *args, **kwargs)
        try:
            in_scope = Path(file).parent == tmp_path if not isinstance(file, int) else file in write_fds
        except (TypeError, ValueError):
            in_scope = False
        return wrap_stream(stream, in_scope, mode)

    def inject_builtin_open(file, mode="r", *args, **kwargs):
        stream = real_builtin_open(file, mode, *args, **kwargs)
        try:
            in_scope = Path(file).parent == tmp_path if not isinstance(file, int) else file in write_fds
        except (TypeError, ValueError):
            in_scope = False
        return wrap_stream(stream, in_scope, mode)

    def inject_fdopen(fd, mode="r", *args, **kwargs):
        return wrap_stream(real_fdopen(fd, mode, *args, **kwargs), fd in write_fds, mode)

    def inject_binary_write(fd, data):
        nonlocal injection_hits, partial_write_observed
        if fd in write_fds and not injection_hits:
            size = max(1, len(data) // 2)
            written = real_os_write(fd, data[:size])
            injection_hits += 1
            partial_write_observed = written == size
            raise OSError("injected partial write failure")
        return real_os_write(fd, data)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fdopen", inject_fdopen)
    monkeypatch.setattr(os, "write", inject_binary_write)
    monkeypatch.setattr(builtins, "open", inject_builtin_open)
    monkeypatch.setattr(io, "open", inject_text_write)
    with pytest.raises(OSError, match="injected partial write failure"):
        demo.write_state(tmp_path, new)

    state_path = tmp_path / demo.STATE_NAME
    assert injection_hits == 1, "fault injection must reach a real state-file write"
    assert partial_write_observed, "fault injection must persist a partial prefix before failing"
    assert state_path.read_bytes() == old_bytes
    assert demo.read_state(tmp_path) == old
    assert {entry.name for entry in tmp_path.iterdir()} == before
    assert all(new_token.encode() not in entry.read_bytes() for entry in tmp_path.iterdir())


def test_existing_state_symlink_cannot_overwrite_external_target(tmp_path: Path) -> None:
    external = tmp_path / "external-state.json"
    external.write_text(json.dumps(_state("outside-secret")), encoding="utf-8")
    state_path = tmp_path / demo.STATE_NAME
    state_path.symlink_to(external)

    try:
        demo.write_state(tmp_path, _state("inside-secret"))
    except OSError:
        # Refusing to operate on an existing symlink is a valid safe policy.
        pass

    assert json.loads(external.read_text(encoding="utf-8")) == _state("outside-secret")
    if state_path.is_symlink():
        assert state_path.resolve() == external
    else:
        assert state_path.exists()
        assert not state_path.is_symlink()
        assert demo.read_state(tmp_path) == _state("inside-secret")
