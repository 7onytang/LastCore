"""Packaging gate rejects either executable when the core helper is absent."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('control_manifest',
    Path(__file__).resolve().parents[1] / 'packaging' / 'static_manifest.py')
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


def test_frozen_control_helper_is_required_without_executing_any_module(tmp_path):
    source = tmp_path / 'control.py'
    source.write_text('raise AssertionError("must never execute this module")\n')
    code = compile(source.read_bytes(), '/different/build/location.py', 'exec')
    class Archive:
        toc = {'PYZ.pyz': (0, 1, 1, 0, 'z')}
        def open_embedded_archive(self, name):
            assert name == 'PYZ.pyz'
            return SimpleNamespace(toc={'lastcore_node_agent.local_control': (0, 1, 2)}, extract=lambda _: code)
    assert manifest.require_control_module('fixture.exe', lambda _: Archive(), source) == 'lastcore_node_agent.local_control'
    class Missing(Archive):
        def open_embedded_archive(self, name):
            return SimpleNamespace(toc={'lastcore_node_agent.agent': (0, 1, 2)})
    with pytest.raises(RuntimeError, match='helper missing'):
        manifest.require_control_module('fixture.exe', lambda _: Missing(), source)
    source.write_text('raise AssertionError("changed source must be rejected")\n')
    with pytest.raises(RuntimeError, match='differs from fixed source'):
        manifest.require_control_module('fixture.exe', lambda _: Archive(), source)
