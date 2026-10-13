"""Desktop launcher must select inference explicitly and isolate private settings."""
import importlib.util
import os
from pathlib import Path
import sys

import pytest

DESKTOP = Path(__file__).resolve().parents[1] / 'desktop'
sys.path.insert(0, str(DESKTOP))
spec = importlib.util.spec_from_file_location('desktop_launcher', DESKTOP / 'run_agent.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def private_config(tmp_path, text):
    path = tmp_path / 'node.env'
    path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize('mode', ['', 'NODE_AGENT_EXECUTOR=custom:executor\n'])
def test_launcher_rejects_missing_or_custom_executor(tmp_path, mode):
    with pytest.raises(SystemExit) as exc:
        launcher.main(['--env-file', str(private_config(tmp_path, mode))])
    assert exc.value.code == 2


def test_launcher_clears_inherited_weights_and_credential(tmp_path, monkeypatch):
    monkeypatch.setenv('NODE_AGENT_WEIGHTS', '/unintended/checkpoint')
    monkeypatch.setenv('NODE_AGENT_REGISTRATION_TOKEN', 'inherited-secret')
    captured = {}
    def run_module(*args, **kwargs):
        captured.update(os.environ)
    monkeypatch.setattr(launcher.runpy, 'run_module', run_module)
    # Restore even keys the wrapper intentionally removes from the environment.
    old = dict(os.environ)
    try:
        assert launcher.main(['--env-file', str(private_config(tmp_path, 'NODE_AGENT_EXECUTOR=stage\n'))]) == 0
        assert captured['NODE_AGENT_EXECUTOR'] == 'stage'
        assert 'NODE_AGENT_WEIGHTS' not in captured
        assert 'NODE_AGENT_REGISTRATION_TOKEN' not in captured
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_launcher_redacts_invalid_runtime_configuration(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ValueError('accidental-sensitive-value')
    monkeypatch.setattr(launcher.runpy, 'run_module', fail)
    old = dict(os.environ)
    try:
        assert launcher.main(['--env-file', str(private_config(tmp_path, 'NODE_AGENT_EXECUTOR=stage\n'))]) == 4
        output = capsys.readouterr().err
        assert 'invalid node configuration' in output
        assert 'accidental-sensitive-value' not in output
    finally:
        os.environ.clear()
        os.environ.update(old)
