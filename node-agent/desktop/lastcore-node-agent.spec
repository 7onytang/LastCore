# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: LastCore node-agent desktop bundle (LC-12 phase 2, macOS).

Produces one single-directory bundle ``dist/lastcore-node-agent/`` containing
two console executables that share one ``_internal`` dependency tree:

- ``lastcore-node-agent`` entry ``desktop/run_agent.py``  (--env-file flow unchanged)
- ``lastcore-preflight``  entry ``desktop/preflight.py``  (--env-file flow unchanged)

Documented semantics preserved by this spec:

- configuration is loaded at runtime from a private KEY=VALUE env file
  (``--env-file``); nothing is embedded, no token is baked into the binary;
- state directory, credentials journal and exit-lock semantics are the
  unchanged source code from ``lastcore_node_agent/``;
- no code-sign identity / notarization (``codesign_identity=None``); the only
  signature is the linker's mandatory ad-hoc signature on Apple Silicon;
- ``target_arch=None`` builds for the build host's native CPU (macOS CPU
  build); PyInstaller cannot cross-compile, so Windows bundling requires a
  real Windows host and remains explicitly unverified;
- third-party license material travels with the bundle: the ``.dist-info``
  (METADATA + license files) of every distribution the app ships is copied
  via PyInstaller's ``copy_metadata()`` hook utility, and the generated
  ``third-party-notices.md`` (see ``desktop/collect_licenses.py``) lands in
  ``_internal/licenses/`` together with the build interpreter's full CPython
  license text (PSF License Version 2 plus predecessor terms) collected by
  the same script, so the embedded libpython ships its complete license text.
"""
import json
import os
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

spec_dir = SPECPATH
node_agent_root = os.path.abspath(os.path.join(spec_dir, os.pardir))

sys.path[:0] = [node_agent_root, os.path.join(node_agent_root, "..", "inference-engine"),
                os.path.join(node_agent_root, "..", "communication")]

# collect_licenses.py (run by build_bundle.sh before this spec) derives the
# frozen runtime dependency closure from the build venv and records it here.
licenses_manifest_path = os.path.join(node_agent_root, "build", "licenses",
                                      "manifest.json")
with open(licenses_manifest_path, encoding="utf-8") as _fh:
    licenses_manifest = json.load(_fh)

# dist-info directories of every shipped distribution (license transport).
license_datas = []
for _entry in licenses_manifest["runtime_distributions"]:
    license_datas += copy_metadata(_entry["name"])
license_datas += copy_metadata(licenses_manifest["build_tool"]["name"])
# generated third-party notices document + full CPython license text
# (both written by collect_licenses.py into build/licenses)
license_datas.append(
    (os.path.join(node_agent_root, "build", "licenses"), "licenses"))

# run_agent.py loads the package dynamically via runpy.run_module(), which
# static analysis cannot see; every submodule must be listed here.
# Dynamic executor imports require complete project package collection. NumPy's
# maintained PyInstaller hook handles extension modules and bundled native libs.
hidden = []
for package in ("lastcore_node_agent", "inference_engine", "communication"):
    hidden += collect_submodules(package, on_error="raise")


agent_analysis = Analysis(
    [os.path.join(spec_dir, "run_agent.py")],
    pathex=[node_agent_root, spec_dir, os.path.join(node_agent_root, "..", "inference-engine"),
            os.path.join(node_agent_root, "..", "communication")],
    binaries=[],
    datas=license_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

preflight_analysis = Analysis(
    [os.path.join(spec_dir, "preflight.py")],
    pathex=[node_agent_root, spec_dir],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

agent_pyz = PYZ(agent_analysis.pure)
preflight_pyz = PYZ(preflight_analysis.pure)

agent_exe = EXE(
    agent_pyz,
    agent_analysis.scripts,
    [],
    exclude_binaries=True,
    name="lastcore-node-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

preflight_exe = EXE(
    preflight_pyz,
    preflight_analysis.scripts,
    [],
    exclude_binaries=True,
    name="lastcore-preflight",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    agent_exe,
    preflight_exe,
    agent_analysis.binaries,
    agent_analysis.datas,
    preflight_analysis.binaries,
    preflight_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="lastcore-node-agent",
)
