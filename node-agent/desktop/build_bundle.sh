#!/usr/bin/env bash
# Build the LastCore node-agent desktop bundle on macOS (LC-12 phase 2).
#
# Reproducible procedure:
#   1. macOS (Darwin) host; Windows bundling needs a real Windows host and is
#      explicitly unverified (desktop/README.md "安装打包路线").
#   2. Python 3.12 venv (BUNDLE_PYTHON overrides the interpreter; BUNDLE_VENV
#      overrides the venv location, default: node-agent/.venv).
#   3. Pinned dependencies: node-agent/requirements.txt (httpx==0.28.1) and
#      pyinstaller==6.22.3 (verified: latest on PyPI at build time; GPLv2 with
#      bundling exception; Requires-Python >=3.8,<3.16).
#   4. pip runs through pip_no_sysconf_proxy.py (process-scoped proxy
#      neutralization, no system settings touched).
#   5. PyInstaller --clean --noconfirm with desktop/lastcore-node-agent.spec.
#   6. Prints bundle path, size and sha256 of both executables.
#
# The bundle is NOT code-signed with an identity and NOT notarized; unsigned
# distribution on macOS is out of scope for this phase.
set -euo pipefail

spec_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
node_agent_root="$(cd "$spec_dir/.." && pwd)"
venv_dir="${BUNDLE_VENV:-$node_agent_root/.venv}"
python_bin="${BUNDLE_PYTHON:-python3.12}"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "build_bundle.sh: only macOS (Darwin) builds are implemented; host is $(uname -s)." >&2
    echo "Windows packaging must run on a real Windows host and is explicitly unverified." >&2
    exit 3
fi

if [ ! -x "$venv_dir/bin/python" ]; then
    "$python_bin" -m venv "$venv_dir"
fi

pip_install() {
    "$venv_dir/bin/python" "$spec_dir/pip_no_sysconf_proxy.py" \
        install --quiet --disable-pip-version-check "$@"
}

pip_install -r "$node_agent_root/requirements.txt"
pip_install -r "$node_agent_root/requirements-dev.txt"
pip_install "pyinstaller==6.22.3"

echo "== build environment =="
"$venv_dir/bin/python" --version
"$venv_dir/bin/python" -c "import httpx; print('httpx', httpx.__version__)"
"$venv_dir/bin/python" -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)"
"$venv_dir/bin/python" -c "import platform; print('macOS', platform.mac_ver()[0], platform.machine())"

echo "== building =="
"$venv_dir/bin/python" -m PyInstaller \
    --clean --noconfirm \
    --distpath "$node_agent_root/dist" \
    --workpath "$node_agent_root/build/pyinstaller" \
    "$spec_dir/lastcore-node-agent.spec"

bundle="$node_agent_root/dist/lastcore-node-agent"
echo "== artifacts =="
echo "bundle: $bundle"
du -sh "$bundle"
shasum -a 256 "$bundle/lastcore-node-agent" "$bundle/lastcore-preflight"
