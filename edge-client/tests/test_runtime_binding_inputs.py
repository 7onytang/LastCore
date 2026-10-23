"""Verify the production manifest binds newly imported runtime modules, without building."""
import ast
from pathlib import Path


def test_runtime_input_assignment_includes_all_new_control_and_snapshot_modules():
    root=Path(__file__).resolve().parents[2]
    source=root/'edge-client/packaging/static_manifest.py'
    tree=ast.parse(source.read_text())
    assignment=next(node for node in ast.walk(tree) if isinstance(node,ast.Assign)
        and any(isinstance(target,ast.Name) and target.id=='runtime_inputs' for target in node.targets))
    context={}
    exec(compile(ast.Module(body=[assignment],type_ignores=[]),str(source),'exec'),context)
    inputs=context['runtime_inputs']
    assert len(inputs)==len(set(inputs))
    for relative in ('node-agent/lastcore_node_agent/local_status.py','node-agent/lastcore_node_agent/local_control.py'):
        assert relative in inputs
        assert (root/relative).is_file()
    # Keep existing source bindings rather than reducing the list to the new files.
    assert 'edge-client/gui.py' in inputs and 'node-agent/lastcore_node_agent/agent.py' in inputs


def test_snapshot_module_must_reach_the_actual_frozen_archive_gate():
    import importlib.util
    from types import SimpleNamespace
    import pytest
    source=Path(__file__).resolve().parents[1]/'packaging/static_manifest.py'
    spec=importlib.util.spec_from_file_location('snapshot_manifest',source)
    manifest=importlib.util.module_from_spec(spec);spec.loader.exec_module(manifest)
    class Archive:
        toc={'PYZ.pyz':(0,1,1,0,'z')}
        def open_embedded_archive(self,name):
            return SimpleNamespace(toc={'lastcore_node_agent.local_status':(0,1,2)})
    assert manifest.require_control_module('fake.exe',lambda _:Archive(),required='lastcore_node_agent.local_status')=='lastcore_node_agent.local_status'
    class Missing(Archive):
        def open_embedded_archive(self,name):
            return SimpleNamespace(toc={'lastcore_node_agent.local_control':(0,1,2)})
    with pytest.raises(RuntimeError,match='local_status'):
        manifest.require_control_module('fake.exe',lambda _:Missing(),required='lastcore_node_agent.local_status')
