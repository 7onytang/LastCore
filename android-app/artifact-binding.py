#!/usr/bin/env python3
"""Bind clean tracked Android source to APKs; verify compiled security policy."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
ASSET = ROOT / 'app/build/generated/binding-assets/source-binding.json'

def git(*args): return subprocess.check_output(['git', '-C', str(REPO), *args], text=True).strip()
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def source():
    if git('status', '--porcelain', '--untracked-files=all', '--', 'android-app', '.github/workflows/android-native.yml'):
        raise SystemExit('Android source must be committed and clean before artifact binding')
    sha = git('rev-parse', 'HEAD')
    if os.environ.get('GITHUB_SHA') and sha != os.environ['GITHUB_SHA']:
        raise SystemExit('checkout does not match GITHUB_SHA')
    files = git('ls-files', 'android-app').splitlines()
    return {'source_commit': sha, 'source_tracked_clean': True,
            'android_source_files': {p: digest(REPO / p) for p in files},
            'timezone': 'Asia/Shanghai', 'device_tests': 'compiled only, no device execution',
            'node_admission': 'disabled; distributed transport not integrated'}

def verify():
    expected = source()
    if json.loads(ASSET.read_text()) != expected: raise SystemExit('generated binding asset is stale')
    sdk = Path(os.environ.get('ANDROID_HOME', ROOT / '.toolchain/sdk'))
    tool = sdk / 'build-tools/35.0.0/aapt'
    artifacts = {}
    for variant in ['debug', 'release']:
        name = 'app-debug.apk' if variant == 'debug' else 'app-release-unsigned.apk'
        apk = ROOT / f'app/build/outputs/apk/{variant}/{name}'
        with zipfile.ZipFile(apk) as z:
            if json.loads(z.read('assets/source-binding.json')) != expected:
                raise SystemExit('APK source binding differs')
            for license_name in ['llama.cpp-LICENSE.txt', 'SmolLM2-APACHE-2.0.txt']:
                p = ROOT / 'app/src/main/assets/licenses' / license_name
                if p.exists() and z.read('assets/licenses/' + license_name) != p.read_bytes():
                    raise SystemExit('shipped license mismatch')
        badging = subprocess.check_output([str(tool), 'dump', 'badging', str(apk)], text=True)
        permissions = [l for l in badging.splitlines() if l.startswith('uses-permission:')]
        if permissions != ["uses-permission: name='android.permission.INTERNET'"]: raise SystemExit('unexpected permission')
        for item in ["sdkVersion:'29'", "targetSdkVersion:'35'"]:
            if item not in badging: raise SystemExit('SDK mismatch')
        xml = subprocess.check_output([str(tool), 'dump', 'xmltree', str(apk), 'AndroidManifest.xml'], text=True)
        for flag in ['allowBackup', 'usesCleartextTraffic']:
            matches = [l for l in xml.splitlines() if 'android:' + flag + '(' in l]
            if len(matches) != 1 or '(type 0x12)0x0' not in matches[0]: raise SystemExit('manifest ' + flag + ' must be false')
        if any(l.strip().startswith(('E: service', 'E: receiver')) for l in xml.splitlines()): raise SystemExit('unexpected background component')
        if variant == 'release' and 'application-debuggable' in badging: raise SystemExit('release debuggable')
        artifacts[str(apk.relative_to(ROOT))] = {'sha256': digest(apk), 'size_bytes': apk.stat().st_size,
            'signing': 'debug key only' if variant == 'debug' else 'unsigned release, no publishing'}
    report = dict(expected, built_at=datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), artifacts=artifacts)
    test = ROOT / 'app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk'
    report['instrumentation_apk'] = {'sha256': digest(test), 'size_bytes': test.stat().st_size, 'execution': 'not run'}
    out = ROOT / 'verification/artifact-manifest.json'; out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('mode', choices=['prepare', 'verify']); args=p.parse_args()
    if args.mode == 'prepare':
        ASSET.parent.mkdir(parents=True, exist_ok=True)
        ASSET.write_text(json.dumps(source(), indent=2) + '\n')
    else: verify()
