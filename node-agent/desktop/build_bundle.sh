#!/usr/bin/env bash
# Build the LastCore node-agent desktop bundle on macOS (LC-12 phase 2).
#
# Reproducible procedure:
#   1. macOS (Darwin) host; Windows bundling needs a real Windows host and is
#      explicitly unverified (desktop/README.md "安装打包路线").
#   2. Python 3.12 venv (BUNDLE_PYTHON overrides the interpreter; BUNDLE_VENV
#      overrides the venv location, default: node-agent/.venv).
#   3. Complete build dependency lock: desktop/requirements-build-lock.txt pins
#      the runtime closure (httpx==0.28.1 + its transitive deps) AND the build
#      toolchain (pyinstaller==6.22.3, pyinstaller-hooks-contrib and every
#      transitive dep) with sha256 wheel hashes, installed with
#      --require-hashes into the clean venv below. Lock scope (macOS arm64,
#      CPython 3.12) and the regeneration procedure are documented in the lock
#      header; binary hashes are NOT claimed identical across other Python or
#      macOS versions/platforms. Test-only dependencies (pytest) are NOT
#      installed here - run the regression suite from a separate verification
#      venv (desktop/README.md).
#   4. Third-party license material: desktop/collect_licenses.py derives the
#      frozen runtime dependency closure from the build venv, writes the
#      third-party notices into build/licenses/ (the spec transports each
#      distribution's dist-info via PyInstaller copy_metadata and the notices
#      into _internal/licenses/), copies the build interpreter's own full
#      CPython license text (PSF License Version 2 plus predecessor terms)
#      into the same directory for the embedded libpython, and after the
#      build cross-checks the distributions actually frozen (Analysis TOC)
#      and the license files - CPython text included - inside the bundle
#      against that manifest.
#   5. pip runs through pip_no_sysconf_proxy.py (skips only the macOS
#      SystemConfiguration proxy fallback; standard HTTP_PROXY/HTTPS_PROXY/
#      ALL_PROXY/NO_PROXY environment behavior is preserved; no system
#      settings touched).
#   6. PyInstaller --clean --noconfirm with desktop/lastcore-node-agent.spec.
#   7. Prints bundle path, size and sha256 of both executables.
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

pip_install --require-hashes -r "$spec_dir/requirements-build-lock.txt"

echo "== build environment =="
"$venv_dir/bin/python" --version
"$venv_dir/bin/python" -m pip --version
"$venv_dir/bin/python" -c "import httpx; print('httpx', httpx.__version__)"
"$venv_dir/bin/python" -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)"
"$venv_dir/bin/python" -c "import platform; print('macOS', platform.mac_ver()[0], platform.machine())"
shasum -a 256 "$spec_dir/requirements-build-lock.txt"

echo "== third-party license material =="
"$venv_dir/bin/python" "$spec_dir/collect_licenses.py" collect \
    --requirements "$node_agent_root/requirements.txt" \
    --lock "$spec_dir/requirements-build-lock.txt" \
    --output-dir "$node_agent_root/build/licenses"

echo "== building =="
"$venv_dir/bin/python" -m PyInstaller \
    --clean --noconfirm \
    --distpath "$node_agent_root/dist" \
    --workpath "$node_agent_root/build/pyinstaller" \
    "$spec_dir/lastcore-node-agent.spec"

bundle="$node_agent_root/dist/lastcore-node-agent"

echo "== license verification =="
"$venv_dir/bin/python" "$spec_dir/collect_licenses.py" verify \
    --manifest "$node_agent_root/build/licenses/manifest.json" \
    --bundle-dir "$bundle" \
    --workpath "$node_agent_root/build/pyinstaller"

echo "== artifacts =="
echo "bundle: $bundle"
du -sh "$bundle"
shasum -a 256 "$bundle/lastcore-node-agent" "$bundle/lastcore-preflight"
