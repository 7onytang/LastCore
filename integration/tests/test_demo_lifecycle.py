"""Lifecycle regression tests for the persistent internal demo.

The fixture launches small, real child processes in place of the heavyweight
Controller and Node Agent commands.  Each child owns a separate TCP port and
process group, so cleanup and ownership assertions exercise the same OS-level
contracts as the demo without requiring model dependencies or a live server.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from integration import demo


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _stub_code(port: int, marker: str) -> str:
    return f"""import signal, socket
marker = {marker!r}
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', {port}))
s.listen(8)
stopping = [False]
signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0, True))
signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__(0, True))
s.settimeout(0.1)
while not stopping[0]:
    try:
        s.accept()[0].close()
    except socket.timeout:
        pass
s.close()
"""


@pytest.fixture
def lifecycle_runtime(monkeypatch, tmp_path):
    """Patch heavyweight runtime pieces while retaining real process behavior."""

    started: list[dict] = []
    launch_count = {"value": 0}
    fail_at = {"value": None}

    class FakeConfig:
        n_layers = 4

        def save(self, path):
            Path(path).write_text('{"model_id":"stub"}\n', encoding="utf-8")

    def tiny_config(_model_id):
        return FakeConfig()

    def generate_weights(_config, seed):
        return {"seed": seed}

    def save_weights(path, weights):
        Path(path).write_text(json.dumps(weights) + "\n", encoding="utf-8")

    fake_package = types.ModuleType("inference_engine")
    fake_package.__path__ = []
    fake_config = types.ModuleType("inference_engine.config")
    fake_config.tiny_config = tiny_config
    fake_weights = types.ModuleType("inference_engine.weights")
    fake_weights.generate_weights = generate_weights
    fake_weights.save_weights = save_weights
    monkeypatch.setitem(sys.modules, "inference_engine", fake_package)
    monkeypatch.setitem(sys.modules, "inference_engine.config", fake_config)
    monkeypatch.setitem(sys.modules, "inference_engine.weights", fake_weights)

    def stub_launch(name, argv, *, cwd, env, log_path, port, marker):
        launch_count["value"] += 1
        if fail_at["value"] == launch_count["value"]:
            raise RuntimeError(f"injected startup failure at process {launch_count['value']}")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        log_path = Path(log_path)
        handle = log_path.open("w", encoding="utf-8")
        actual_argv = [sys.executable, "-c", _stub_code(port, marker)]
        process = subprocess.Popen(actual_argv, cwd=str(cwd), env=env, stdout=handle,
                                   stderr=subprocess.STDOUT, start_new_session=True, text=True)
        handle.close()
        info = demo.ps_info(process.pid)
        assert info.get("alive"), f"stub {name} exited immediately"
        record = {
            "name": name,
            "pid": process.pid,
            "pgid": info["pgid"],
            "lstart": info["lstart"],
            "argv": actual_argv,
            "requested_argv": argv,
            "cwd": str(cwd),
            "log": str(log_path),
            "port": port,
            "marker": marker,
            "command": info["command"],
            "stub": True,
        }
        started.append(record)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not _port_open(port):
            time.sleep(0.01)
        return record

    monkeypatch.setattr(demo, "ensure_venvs", lambda: {})
    monkeypatch.setattr(demo, "free_port", _free_port, raising=False)
    monkeypatch.setattr(demo, "launch", stub_launch)
    monkeypatch.setattr(demo, "http_json", lambda *_args, **_kwargs: (200, {"nodes": []}),
                        raising=False)
    monkeypatch.setattr(demo, "wait_until", lambda predicate, **_kwargs: predicate(),
                        raising=False)
    monkeypatch.setattr(demo, "_wait_registered",
                        lambda _url, _token, nodes: [r.update(node_id=f"stub-{key}")
                                                     for key, r in nodes.items()])
    monkeypatch.setattr(demo, "port_open", _port_open, raising=False)

    def args(workdir: Path, *, force: bool = False):
        return SimpleNamespace(workdir=str(workdir), seed=7, heartbeat_ttl=8,
                               complete_after=1, force=force)

    def stop_all():
        seen = set()
        for record in reversed(started):
            if record["pid"] in seen:
                continue
            seen.add(record["pid"])
            demo.kill_record(record, timeout=2)

    yield SimpleNamespace(args=args, started=started, fail_at=fail_at,
                          launch_count=launch_count, stop_all=stop_all)
    stop_all()


@pytest.mark.parametrize("failure_number,expected_survivors", [(2, 1), (3, 2)])
def test_start_failure_reclaims_every_process_started_before_failure(
    lifecycle_runtime, tmp_path, failure_number, expected_survivors
):
    lifecycle_runtime.fail_at["value"] = failure_number

    with pytest.raises(RuntimeError, match="injected startup failure"):
        demo.cmd_start(lifecycle_runtime.args(tmp_path))

    launched = lifecycle_runtime.started
    assert len(launched) == expected_survivors
    assert all(not demo.ps_info(record["pid"]).get("alive") for record in launched)
    assert all(not _port_open(record["port"]) for record in launched)
    assert not (tmp_path / demo.STATE_NAME).exists()


def test_start_rejects_existing_live_state_without_force(lifecycle_runtime, tmp_path):
    assert demo.cmd_start(lifecycle_runtime.args(tmp_path)) == 0
    original = json.loads((tmp_path / demo.STATE_NAME).read_text(encoding="utf-8"))
    original_pids = [record["pid"] for record in original["processes"]]

    assert demo.cmd_start(lifecycle_runtime.args(tmp_path)) == 1
    current = json.loads((tmp_path / demo.STATE_NAME).read_text(encoding="utf-8"))
    assert [record["pid"] for record in current["processes"]] == original_pids
    assert all(demo.ps_info(pid).get("alive") for pid in original_pids)


def test_force_start_reclaims_old_processes_and_writes_new_state(lifecycle_runtime, tmp_path):
    assert demo.cmd_start(lifecycle_runtime.args(tmp_path)) == 0
    old_state = json.loads((tmp_path / demo.STATE_NAME).read_text(encoding="utf-8"))
    old_pids = {record["pid"] for record in old_state["processes"]}

    assert demo.cmd_start(lifecycle_runtime.args(tmp_path, force=True)) == 0
    new_state = json.loads((tmp_path / demo.STATE_NAME).read_text(encoding="utf-8"))
    new_pids = {record["pid"] for record in new_state["processes"]}
    assert old_pids.isdisjoint(new_pids)
    assert all(not demo.ps_info(pid).get("alive") for pid in old_pids)
    assert all(demo.ps_info(pid).get("alive") for pid in new_pids)
    assert new_state["created_at"] != old_state["created_at"] or new_pids != old_pids


def test_stop_protects_unrelated_live_stub_and_repeat_stop_is_idempotent(
    lifecycle_runtime, tmp_path
):
    assert demo.cmd_start(lifecycle_runtime.args(tmp_path)) == 0
    state_path = tmp_path / demo.STATE_NAME
    state = json.loads(state_path.read_text(encoding="utf-8"))
    original_node = next(record for record in state["processes"] if record["name"] == "node-a")

    foreign_port = _free_port()
    foreign_log = tmp_path / "foreign.log"
    foreign = subprocess.Popen(
        [sys.executable, "-c", _stub_code(foreign_port, "foreign-stub")],
        stdout=foreign_log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT,
        start_new_session=True, text=True,
    )
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not _port_open(foreign_port):
            time.sleep(0.01)
        foreign_info = demo.ps_info(foreign.pid)
        replacement = {**original_node, "pid": foreign.pid, "pgid": foreign_info["pgid"],
                       "lstart": foreign_info["lstart"], "port": foreign_port,
                       "marker": "not-the-foreign-marker"}
        state["processes"] = [replacement if record["name"] == "node-a" else record
                               for record in state["processes"]]
        demo.write_state(tmp_path, state)

        assert demo.cmd_stop(lifecycle_runtime.args(tmp_path)) == 1
        assert demo.ps_info(foreign.pid).get("alive")
        assert _port_open(foreign_port)

        # Restore the original ownership record, then verify normal stop and a
        # second stop both complete without touching the unrelated stub.
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["processes"] = [original_node if record["name"] == "node-a" else record
                               for record in state["processes"]]
        demo.write_state(tmp_path, state)
        assert demo.cmd_stop(lifecycle_runtime.args(tmp_path)) == 0
        assert demo.cmd_stop(lifecycle_runtime.args(tmp_path)) == 0
        assert demo.ps_info(foreign.pid).get("alive")
        assert _port_open(foreign_port)
    finally:
        if demo.ps_info(foreign.pid).get("alive"):
            os.killpg(foreign_info["pgid"], signal.SIGTERM)
            try:
                foreign.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(foreign_info["pgid"], signal.SIGKILL)
                foreign.wait(timeout=2)
