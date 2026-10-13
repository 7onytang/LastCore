import importlib.util
import json
import os
import stat
from pathlib import Path
from xml.etree import ElementTree

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1] / "desktop"
SPEC = importlib.util.spec_from_file_location("desktop_preflight", DESKTOP_DIR / "preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def statuses(result):
    return {status for status, _ in result}


def messages(result):
    return [message for _, message in result]


def test_probe_memory_mb_positive():
    memory = preflight.probe_memory_mb()
    assert memory is None or memory > 0


def test_check_python_passes_on_current_interpreter():
    result = []
    preflight.check_python(result)
    assert "PASS" in statuses(result)


def test_check_httpx_passes_in_test_env():
    result = []
    preflight.check_httpx(result)
    assert statuses(result) == {"PASS"}


def test_resolve_state_dir_default_and_explicit():
    assert preflight.resolve_state_dir(None) == Path.home() / ".lastcore" / "node-agent"
    assert preflight.resolve_state_dir("/tmp/x") == Path("/tmp/x")


def test_check_credentials_reports_node_id_without_token(tmp_path):
    from lastcore_node_agent.credentials import NodeCredentials, save_credentials
    save_credentials(tmp_path, NodeCredentials("node-42", "secret-token-value", 30))
    result = []
    preflight.check_credentials(result, tmp_path)
    assert statuses(result) == {"PASS"}
    assert "node-42" in messages(result)[0]
    assert "secret-token-value" not in messages(result)[0]


def test_check_credentials_corrupt_file_fails(tmp_path):
    (tmp_path / "node-credentials.json").write_text("{not json", encoding="utf-8")
    result = []
    preflight.check_credentials(result, tmp_path)
    assert statuses(result) == {"FAIL"}


def test_check_credentials_missing_without_token_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("NODE_AGENT_REGISTRATION_TOKEN", raising=False)
    result = []
    preflight.check_credentials(result, tmp_path)
    assert statuses(result) == {"FAIL"}


def test_check_state_dir_reports_mode(tmp_path):
    result = []
    preflight.check_state_dir(result, tmp_path / "state")
    assert statuses(result) <= {"PASS", "WARN"}
    assert (tmp_path / "state").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX state-directory mode bits only")
def test_new_state_dir_is_created_private(tmp_path):
    state_dir = tmp_path / "state"
    result = []
    preflight.check_state_dir(result, state_dir)
    assert statuses(result) == {"PASS", "WARN"}
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX state-directory mode bits only")
def test_nonprivate_state_dir_fails_without_exposing_credentials(tmp_path, monkeypatch, capsys):
    from lastcore_node_agent.credentials import NodeCredentials, save_credentials

    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    save_credentials(state_dir, NodeCredentials("node-42", "secret-token-value", 30))
    state_dir.chmod(0o755)
    monkeypatch.delenv("NODE_AGENT_CONTROLLER_URL", raising=False)
    monkeypatch.delenv("NODE_AGENT_ENDPOINT", raising=False)
    monkeypatch.delenv("NODE_AGENT_STATE_DIR", raising=False)
    monkeypatch.setenv("NODE_AGENT_REGISTRATION_TOKEN", "secret-token-value")
    monkeypatch.setattr(preflight, "probe_memory_mb", lambda: 2048)
    monkeypatch.setattr(preflight, "probe_disk_free_mb", lambda _: 2048)

    exit_code = preflight.main(["--json", "--state-dir", str(state_dir)])
    output = capsys.readouterr().out
    captured = json.loads(output)
    assert exit_code == 1
    assert captured["ok"] is False
    assert any(check["status"] == "FAIL" and "group/other accessible" in check["message"]
               for check in captured["checks"])
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o755
    assert "secret-token-value" not in output


def test_unknown_memory_fails_when_minimum_is_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "probe_memory_mb", lambda: None)
    monkeypatch.setattr(preflight, "probe_disk_free_mb", lambda _: 2048)
    result = []
    preflight.check_resources(result, tmp_path, 1_000_000_000)
    assert result[0][0] == "FAIL"
    assert "unknown" in messages(result)[0]
    assert "1000000000 MB" in messages(result)[0]


def test_resource_memory_shortfall_fails_and_sufficient_memory_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "probe_disk_free_mb", lambda _: 2048)
    monkeypatch.setattr(preflight, "probe_memory_mb", lambda: 512)
    insufficient = []
    preflight.check_resources(insufficient, tmp_path, 1024)
    assert insufficient[0][0] == "FAIL"
    assert "below minimum 1024 MB" in messages(insufficient)[0]

    monkeypatch.setattr(preflight, "probe_memory_mb", lambda: 2048)
    sufficient = []
    preflight.check_resources(sufficient, tmp_path, 1024)
    assert sufficient[0][0] == "PASS"
    assert "2048 MB" in messages(sufficient)[0]


def test_check_state_dir_creates_missing_parent_chain(tmp_path):
    state_dir = tmp_path / "first" / "second" / "node-agent"
    result = []
    preflight.check_state_dir(result, state_dir)
    assert state_dir.is_dir()
    assert "FAIL" not in statuses(result), result


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential mode bits only")
def test_check_credentials_rejects_group_or_other_readable_file(tmp_path):
    from lastcore_node_agent.credentials import NodeCredentials, save_credentials
    save_credentials(tmp_path, NodeCredentials("node-42", "secret-token-value", 30))
    path = tmp_path / "node-credentials.json"
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    result = []
    preflight.check_credentials(result, tmp_path)
    assert statuses(result) == {"FAIL"}
    assert "0600" in messages(result)[0]
    assert "secret-token-value" not in messages(result)[0]


def test_check_endpoint_port_free_and_busy():
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_host, free_port = probe.getsockname()[:2]
    probe.close()
    result = []
    preflight.check_endpoint_port(result, f"http://{free_host}:{free_port}")
    assert statuses(result) == {"PASS"}

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    busy_host, busy_port = blocker.getsockname()[:2]
    blocker.listen(1)
    result = []
    preflight.check_endpoint_port(result, f"http://{busy_host}:{busy_port}")
    blocker.close()
    assert statuses(result) == {"FAIL"}


def test_run_checks_no_failures_on_clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_AGENT_REGISTRATION_TOKEN", "bootstrap-token")
    result = preflight.run_checks(None, None, tmp_path, 0, 1.0)
    fatal = [(status, message) for status, message in result if status == "FAIL"]
    assert fatal == [], result
    assert "bootstrap-token" not in messages(result)


def test_json_output_shape(tmp_path, capsys):
    exit_code = preflight.main(["--json", "--state-dir", str(tmp_path)])
    captured = json.loads(capsys.readouterr().out)
    assert set(captured) == {"platform", "state_dir", "checks", "ok"}
    assert isinstance(captured["checks"], list)
    assert captured["ok"] == (exit_code == 0)


def test_preflight_loads_private_env_file_without_shell_expansion(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / "node-agent.env"
    state_dir = tmp_path / "state"
    env_file.write_text(
        f"NODE_AGENT_STATE_DIR={state_dir}\nNODE_AGENT_EXECUTOR=demo\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.delenv("NODE_AGENT_STATE_DIR", raising=False)
    exit_code = preflight.main(["--env-file", str(env_file), "--json"])
    captured = json.loads(capsys.readouterr().out)
    assert exit_code == 1  # this fixture intentionally lacks persisted/registration credentials
    assert captured["state_dir"] == str(state_dir)


def test_env_file_parser_allows_only_literal_node_agent_keys(tmp_path):
    desktop_dir = Path(__file__).resolve().parents[1] / "desktop"
    import sys
    sys.path.insert(0, str(desktop_dir))
    try:
        from env_file import read_env_file
        env_file = tmp_path / "env"
        env_file.write_text(
            "# private config\nNODE_AGENT_CONTROLLER_URL=http://127.0.0.1:8000\n"
            "NODE_AGENT_NAME=Desktop Node\nNODE_AGENT_REGISTRATION_TOKEN=a=b\n"
            "NODE_AGENT_STATE_DIR=$HOME/literal-path\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        assert read_env_file(env_file) == {
            "NODE_AGENT_CONTROLLER_URL": "http://127.0.0.1:8000",
            "NODE_AGENT_NAME": "Desktop Node",
            "NODE_AGENT_REGISTRATION_TOKEN": "a=b",
            "NODE_AGENT_STATE_DIR": "$HOME/literal-path",
        }
        env_file.write_text("PATH=/tmp/override\n", encoding="utf-8")
        with pytest.raises(ValueError, match="unsupported environment key"):
            read_env_file(env_file)
        env_file.write_text("NODE_AGENT_NAME=first\nNODE_AGENT_NAME=second\n", encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate environment key"):
            read_env_file(env_file)
        env_file.write_text("NODE_AGENT_REGISTRATION_TOKEN=do-not-print\n", encoding="utf-8")
        env_file.chmod(0o644)
        with pytest.raises(ValueError, match="not private") as error:
            read_env_file(env_file)
        assert "do-not-print" not in str(error.value)
    finally:
        sys.path.remove(str(desktop_dir))


def test_launchagent_uses_config_loader_and_does_not_embed_registration_token():
    desktop_dir = Path(__file__).resolve().parents[1] / "desktop"
    tree = ElementTree.parse(desktop_dir / "com.lastcore.node-agent.plist")
    program_args = []
    found = False
    for child in tree.getroot().find("dict"):
        if child.tag == "key" and child.text == "ProgramArguments":
            found = True
            continue
        if found and child.tag == "array":
            program_args = [item.text for item in child.findall("string")]
            break
        if found:
            break
    assert "RUN_AGENT_PLACEHOLDER" in program_args
    assert "--env-file" in program_args
    assert "ENV_FILE_PLACEHOLDER" in program_args
    assert "NODE_AGENT_REGISTRATION_TOKEN" not in [item.text for item in tree.findall(".//key")]


def test_windows_wrapper_resolves_node_agent_root_from_windows_folder():
    script = (Path(__file__).resolve().parents[1] / "desktop" / "windows" /
              "lastcore-node-agent.ps1").read_text(encoding="utf-8")
    assert '"..\\..\\.venv"' in script
    assert '"..\\.."' in script
    assert '"..\\run_agent.py"' in script
