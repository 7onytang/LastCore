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
  real Windows host and remains explicitly unverified.
"""
import os

spec_dir = SPECPATH
node_agent_root = os.path.abspath(os.path.join(spec_dir, os.pardir))

# run_agent.py loads the package dynamically via runpy.run_module(), which
# static analysis cannot see; every submodule must be listed here.
hidden = [
    "lastcore_node_agent",
    "lastcore_node_agent.__main__",
    "lastcore_node_agent.agent",
    "lastcore_node_agent.config",
    "lastcore_node_agent.controller",
    "lastcore_node_agent.credentials",
    "lastcore_node_agent.executor",
]

agent_analysis = Analysis(
    [os.path.join(spec_dir, "run_agent.py")],
    pathex=[node_agent_root, spec_dir],
    binaries=[],
    datas=[],
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
