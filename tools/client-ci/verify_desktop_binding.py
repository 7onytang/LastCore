#!/usr/bin/env python3
"""Verify manifest files and frozen project code against the current checkout."""
import hashlib
import json
import marshal
from pathlib import Path
import subprocess
import sys
import types
from PyInstaller.archive.readers import CArchiveReader

repo = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
bundle = repo / 'node-agent/dist/lastcore-node-agent'
manifest = json.loads(bundle.with_suffix('.manifest.json').read_text())
sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
if manifest['source_commit'] != sha or not manifest['source_tracked_clean']:
    raise SystemExit('bundle source commit/clean assertion failed')
subprocess.run(['git', '-C', str(repo), 'diff', '--exit-code', 'HEAD'], check=True)
actual = {}
for p in sorted(bundle.rglob('*')):
    name = str(p.relative_to(bundle))
    if p.is_symlink(): actual[name] = {'symlink': str(p.readlink())}
    elif p.is_file(): actual[name] = {'size_bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
if actual != manifest['files']: raise SystemExit('bundle files changed or missing')

def normalized(code):
    if isinstance(code, types.CodeType):
        return code.replace(co_filename='<bound-source>', co_consts=tuple(normalized(v) for v in code.co_consts))
    if isinstance(code, tuple): return tuple(normalized(v) for v in code)
    return code

def compare(code, path):
    expected = compile(path.read_bytes(), str(path), 'exec', dont_inherit=True, optimize=0)
    if normalized(code) != normalized(expected): raise SystemExit(f'frozen code differs: {path}')

if (bundle / '_internal/licenses/lastcore/LICENSE').read_bytes() != (repo / 'LICENSE').read_bytes():
    raise SystemExit('LastCore license missing or mismatched')

required = set()
for parent, package in [('node-agent', 'lastcore_node_agent'), ('inference-engine', 'inference_engine'), ('communication', 'communication')]:
    for path in (repo / parent / package).rglob('*.py'):
        module = '.'.join(path.relative_to(repo / parent).with_suffix('').parts)
        if module.endswith('.__init__'): module = module[:-9]
        required.add(module)
seen = set()
checked = []
for executable, entry in [('lastcore-node-agent', 'run_agent'), ('lastcore-preflight', 'preflight')]:
    archive = CArchiveReader(str(bundle / executable))
    compare(marshal.loads(archive.extract(entry)), repo / f'node-agent/desktop/{entry}.py')
    checked.append(entry)
    pyz = archive.open_embedded_archive('PYZ.pyz')
    for module in pyz.toc:
        root = next((r for package, r in [('lastcore_node_agent', 'node-agent'), ('inference_engine', 'inference-engine'), ('communication', 'communication')]
                     if module == package or module.startswith(package + '.')), None)
        if root:
            p = repo / root / module.replace('.', '/')
            p = p / '__init__.py' if p.is_dir() else p.with_suffix('.py')
        elif module == 'env_file': p = repo / 'node-agent/desktop/env_file.py'
        else: continue
        compare(pyz.extract(module), p)
        if root: seen.add(module)
        checked.append(f'{executable}:{module}')
if required != seen: raise SystemExit(f'project module closure differs: {required ^ seen}')
if len(checked) < 20: raise SystemExit('project module closure unexpectedly absent')
print(json.dumps({'source_commit': sha, 'files_verified': len(actual), 'code_objects_verified': checked,
                  'signing': 'no Developer ID signing or notarization', 'passed': True}, indent=2))
