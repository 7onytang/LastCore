#!/usr/bin/env python3
"""Bind clean tracked Android source to APKs; verify compiled security policy."""
import argparse
import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import zipfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
ASSET = ROOT / 'app/build/generated/binding-assets/source-binding.json'

def git(*args): return subprocess.check_output(['git', '-C', str(REPO), *args], text=True).strip()
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
SURFACE_PERMISSIONS = {
    'android.permission.INTERNET', 'android.permission.FOREGROUND_SERVICE',
    'android.permission.FOREGROUND_SERVICE_SPECIAL_USE', 'android.permission.POST_NOTIFICATIONS',
}
SERVICE_SUBTYPE = 'User-started visible on-device CPU whole-model worker with bounded assigned tasks and explicit Stop; no automatic restart.'

def verify_surface(badging, xml):
    """Validate the compiled aapt tree with scoped attributes, never by global substring allowance."""
    permissions = [line.strip() for line in badging.splitlines() if line.startswith('uses-permission')]
    expected = {"uses-permission: name='"+name+"'" for name in SURFACE_PERMISSIONS}
    if len(permissions) != 4 or set(permissions) != expected:
        raise SystemExit('unexpected permission')
    if not re.search(r"^package: name='ai\.lastcore\.android' ", badging, re.M):
        raise SystemExit('package mismatch')
    for item in ["sdkVersion:'29'", "targetSdkVersion:'35'"]:
        if item not in badging: raise SystemExit('SDK mismatch')
    nodes, stack = [], []
    for line in xml.splitlines():
        element = re.match(r'^(\s*)E: ([\w-]+)', line)
        if element:
            indent = len(element[1])
            while stack and stack[-1]['indent'] >= indent: stack.pop()
            node = {'tag':element[2], 'indent':indent, 'attrs':{}, 'parent':stack[-1] if stack else None}
            nodes.append(node);stack.append(node)
        else:
            attribute = re.match(r'^\s*A: ([\w:.-]+)(?:\([^)]*\))?=(.*)$', line)
            if attribute and stack:
                key, value = attribute[1], attribute[2].strip()
                if key in stack[-1]['attrs']: raise SystemExit('duplicate compiled attribute')
                stack[-1]['attrs'][key] = value
    def only(tag):
        found = [node for node in nodes if node['tag'] == tag]
        if len(found) != 1: raise SystemExit('unexpected component count: '+tag)
        return found[0]
    def string(node, key):
        match = re.match(r'^"([^"]*)"', node['attrs'].get(key, ''))
        if not match: raise SystemExit('missing compiled string: '+key)
        return match[1]
    def false(node, key):
        if node['attrs'].get(key) != '(type 0x12)0x0': raise SystemExit(key+' must be false')
    app = only('application')
    false(app, 'android:allowBackup');false(app, 'android:usesCleartextTraffic')
    if any('android:process' in node['attrs'] for node in nodes): raise SystemExit('unexpected secondary process')
    for node in nodes:
        for flag in ('android:isolatedProcess', 'android:useAppZygote', 'android:externalService', 'android:allowSharedIsolatedProcess'):
            if flag in node['attrs'] and node['attrs'][flag] != '(type 0x12)0x0':
                raise SystemExit('unexpected isolated service configuration')
        if 'android:zygotePreloadName' in node['attrs']: raise SystemExit('unexpected app zygote preload')
    if any(node['tag'] in ('receiver', 'provider', 'activity-alias') for node in nodes):
        raise SystemExit('unexpected background or exported component')
    service, activity = only('service'), only('activity')
    if service['parent'] is not app or activity['parent'] is not app: raise SystemExit('component parent mismatch')
    if string(service, 'android:name') not in ('.CloudWorkerService', 'ai.lastcore.android.CloudWorkerService'):
        raise SystemExit('unexpected service')
    false(service, 'android:exported')
    if service['attrs'].get('android:foregroundServiceType') != '(type 0x11)0x40000000':
        raise SystemExit('unexpected foreground service type')
    properties = [node for node in nodes if node['tag'] == 'property' and node['parent'] is service]
    if len(properties) != 1 or string(properties[0], 'android:name') != 'android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE' or string(properties[0], 'android:value') != SERVICE_SUBTYPE:
        raise SystemExit('special use purpose mismatch')
    if string(activity, 'android:name') not in ('.MainActivity', 'ai.lastcore.android.MainActivity'):
        raise SystemExit('unexpected activity')
    if activity['attrs'].get('android:exported') not in ('(type 0x12)0xffffffff', '(type 0x12)0x1'):
        raise SystemExit('launcher must be exported')
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
            'node_admission': 'legacy layers disabled; explicit foreground whole-model Controller worker',
            'model_backend': json.loads((ROOT / 'Model.lock.json').read_text()) if (ROOT / 'Model.lock.json').exists() else None}

def verify_apk(expected, apk, variant, sdk):
    """Shared source/license/compiled-policy checks; this does not validate signing."""
    apk = Path(apk)
    tool = sdk / 'build-tools/35.0.0/aapt'
    with zipfile.ZipFile(apk) as z:
        if json.loads(z.read('assets/source-binding.json')) != expected:
            raise SystemExit('APK source binding differs')
        for license_name in [p.name for p in (ROOT / 'app/src/main/assets/licenses').glob('*') if p.is_file()]:
            p = ROOT / 'app/src/main/assets/licenses' / license_name
            if p.exists() and z.read('assets/licenses/' + license_name) != p.read_bytes():
                raise SystemExit('shipped license mismatch')
    if (ROOT / 'Model.lock.json').exists():
        with zipfile.ZipFile(apk) as z:
            libs = [n for n in z.namelist() if n.startswith('lib/') and n.endswith('.so')]
            if set(libs) != {'lib/arm64-v8a/liblastcore_local.so'}:
                raise SystemExit('unexpected native library closure: ' + str(libs))
            elf_dir = ROOT / 'verification' / variant; elf_dir.mkdir(parents=True, exist_ok=True)
            host = 'darwin-x86_64' if os.uname().sysname == 'Darwin' else 'linux-x86_64'
            readelf = sdk / 'ndk/28.2.13676358/toolchains/llvm/prebuilt' / host / 'bin/llvm-readelf'
            for name in libs:
                elf = elf_dir / Path(name).name; elf.write_bytes(z.read(name))
                headers = subprocess.check_output([str(readelf), '-h', '-lW', str(elf)], text=True, stderr=subprocess.PIPE)
                if 'AArch64' not in headers or 'ELF64' not in headers: raise SystemExit('wrong ELF architecture')
                loads = [line.split()[-1] for line in headers.splitlines() if line.strip().startswith('LOAD ')]
                if not loads or any(int(a, 16) < 16384 for a in loads): raise SystemExit('native ELF is not 16 KiB aligned')
        subprocess.run([str(sdk / 'build-tools/35.0.0/zipalign'), '-c', '-P', '16', '4', str(apk)], check=True, capture_output=True, text=True)
    badging = subprocess.check_output([str(tool), 'dump', 'badging', str(apk)], text=True, stderr=subprocess.PIPE)
    xml = subprocess.check_output([str(tool), 'dump', 'xmltree', str(apk), 'AndroidManifest.xml'], text=True, stderr=subprocess.PIPE)
    verify_surface(badging, xml)
    if variant == 'release' and 'application-debuggable' in badging: raise SystemExit('release debuggable')
    return {'sha256': digest(apk), 'size_bytes': apk.stat().st_size,
            'signing': 'debug key only' if variant == 'debug' else 'unsigned release, no publishing'}

def verify():
    expected = source()
    if json.loads(ASSET.read_text()) != expected: raise SystemExit('generated binding asset is stale')
    sdk = Path(os.environ.get('ANDROID_HOME', ROOT / '.toolchain/sdk'))
    tool = sdk / 'build-tools/35.0.0/aapt'
    artifacts = {}
    for variant in ['debug', 'release']:
        name = 'app-debug.apk' if variant == 'debug' else 'app-release-unsigned.apk'
        apk = ROOT / f'app/build/outputs/apk/{variant}/{name}'
        artifacts[str(apk.relative_to(ROOT))] = verify_apk(expected, apk, variant, sdk)
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
