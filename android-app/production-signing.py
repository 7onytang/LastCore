#!/usr/bin/env python3
"""Explicit owner-key APK preparation; credentials never enter argv/logs/provenance."""
import argparse
from dataclasses import dataclass, field
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
FIELDS = {'storeFile', 'keyAlias', 'storePassword', 'keyPassword',
          'expectedCertificateSha256', 'versionCode', 'versionName'}
STORE_PASS_ENV = 'LASTCORE_PRIVATE_APK_STORE_PASSWORD'
KEY_PASS_ENV = 'LASTCORE_PRIVATE_APK_KEY_PASSWORD'


class ProductionError(Exception):
    """Only fixed, public diagnostic messages may be used."""


@dataclass(frozen=True)
class OwnerConfig:
    store_file: Path = field(repr=False)
    key_alias: str = field(repr=False)
    store_password: str = field(repr=False)
    key_password: str = field(repr=False)
    expected_sha256: str
    version_code: int
    version_name: str


def fingerprint(value):
    normalized = value.replace(':', '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{64}', normalized):
        raise ProductionError('Expected public certificate SHA256 is invalid')
    return normalized


def private_file(value, repo):
    try:
        path = Path(value).expanduser().resolve(strict=True)
        info = path.stat()
        if (path.is_relative_to(repo.resolve()) or not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise ProductionError('Owner signing files must be private and outside the repository')
        return path
    except (OSError, ValueError, RuntimeError):
        raise ProductionError('Owner signing file is unavailable or unsafe') from None


def load_config(path, repo=REPO):
    try:
        private = private_file(path, repo)
        if private.stat().st_size > 16384:
            raise ProductionError('Owner signing configuration is invalid')
        text = private.read_text(encoding='utf-8')
        values = {}
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, separator, value = line.partition('=')
            key = key.strip()
            if not separator or key not in FIELDS or key in values or not value or '\x00' in value:
                raise ProductionError('Owner signing configuration is incomplete or invalid')
            values[key] = value  # Literal, single-line values; no shell/property expansion.
        if set(values) != FIELDS:
            raise ProductionError('Owner signing configuration is incomplete or invalid')
        store = private_file(values['storeFile'], repo)  # Stat only: never inspect key material here.
        alias = values['keyAlias']
        if (alias.strip().lower() == 'androiddebugkey' or store.name.lower() == 'debug.keystore'
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', alias)):
            raise ProductionError('Debug or invalid key identity cannot be production')
        if (not re.fullmatch(r'[1-9][0-9]{0,9}', values['versionCode'])
                or not 1 <= int(values['versionCode']) <= 2147483647
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,63}', values['versionName'])):
            raise ProductionError('Owner application version is invalid')
        return OwnerConfig(store, alias, values['storePassword'], values['keyPassword'],
                           fingerprint(values['expectedCertificateSha256']),
                           int(values['versionCode']), values['versionName'])
    except ProductionError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise ProductionError('Owner signing configuration is unavailable or invalid') from None


def run_checked(command, env, runner=subprocess.run):
    try:
        result = runner(command, env=env, capture_output=True, text=True,
                        errors='replace', timeout=1800, check=False)
        if result.returncode:
            raise ProductionError('Build or signing tool failed; no production artifact published')
        return result.stdout
    except ProductionError:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ProductionError('Build or signing tool failed; no production artifact published') from None


def verify_signer(text, expected):
    count = re.findall(r'^Number of signers:\s*([0-9]+)\s*$', text, re.MULTILINE)
    digests = re.findall(r'^Signer #([0-9]+) certificate SHA-256 digest:\s*([^\r\n]+)', text, re.MULTILINE)
    subjects = re.findall(r'^Signer #([0-9]+) certificate DN:\s*([^\r\n]+)', text, re.MULTILINE)
    if (count != ['1'] or len(digests) != 1 or digests[0][0] != '1'
            or len(subjects) != 1 or subjects[0][0] != '1'
            or 'Android Debug'.casefold() in subjects[0][1].casefold()
            or fingerprint(digests[0][1]) != expected
            or not re.search(r'^Verified using v2 scheme .*:\s*true\s*$', text, re.MULTILINE)):
        raise ProductionError('APK signer verification failed; no production artifact published')
    return {'certificate_sha256': expected, 'signer_count': 1,
            'verified_v2': True, 'android_debug_certificate': False}


def binding_module():
    spec = importlib.util.spec_from_file_location('lastcore_apk_binding', ROOT / 'artifact-binding.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_production(config_path, output_dir=None, *, root=ROOT, repo=REPO,
                     runner=subprocess.run, binding=None, environment=None):
    config = load_config(config_path, repo)  # Must succeed before any subprocess/build.
    binding = binding or binding_module()
    env = dict(os.environ if environment is None else environment)
    env.pop(STORE_PASS_ENV, None); env.pop(KEY_PASS_ENV, None)
    env['ORG_GRADLE_PROJECT_lastcoreVersionCode'] = str(config.version_code)
    env['ORG_GRADLE_PROJECT_lastcoreVersionName'] = config.version_name
    sdk = Path(env.get('ANDROID_HOME', root / '.toolchain/sdk'))
    expected = binding.source()
    output = Path(output_dir or root / 'verification/production').resolve()
    if output.is_relative_to(repo.resolve()) and not output.is_relative_to((root / 'verification').resolve()):
        raise ProductionError('Production outputs must be ignored verification files or outside the repository')
    output.mkdir(parents=True, exist_ok=True)
    stem = f"LastCore-Android-{config.version_name}-arm64-production-{expected['source_commit'][:8]}"
    destinations = [output / (stem + suffix) for suffix in ('.apk', '.json', '.apk.sha256')]
    if any(path.exists() for path in destinations):
        raise ProductionError('Refusing to replace existing production artifacts')
    # Default builder still emits and verifies Debug + unsigned release + instrumentation.
    run_checked([str(root / 'build-apk.sh')], env, runner)
    if binding.source() != expected:
        raise ProductionError('Build source changed; no production artifact published')
    unsigned = root / 'app/build/outputs/apk/release/app-release-unsigned.apk'
    if not unsigned.is_file():
        raise ProductionError('Unsigned release input missing; no Debug fallback is permitted')
    signer = sdk / 'build-tools/35.0.0/apksigner'
    aapt = sdk / 'build-tools/35.0.0/aapt'
    zipalign = sdk / 'build-tools/35.0.0/zipalign'
    created = []
    with tempfile.TemporaryDirectory(prefix='.signing-stage-', dir=output) as temp:
        stage = Path(temp)
        candidate = stage / 'candidate.apk'
        aligned = stage / 'aligned.apk'
        run_checked([str(zipalign), '-f', '-P', '16', '4', str(unsigned), str(aligned)], env, runner)
        secret_env = dict(env, **{STORE_PASS_ENV:config.store_password, KEY_PASS_ENV:config.key_password})
        run_checked([str(signer), 'sign', '--ks', str(config.store_file), '--ks-key-alias', config.key_alias,
                     '--ks-pass', 'env:' + STORE_PASS_ENV, '--key-pass', 'env:' + KEY_PASS_ENV,
                     '--out', str(candidate), str(aligned)], secret_env, runner)
        verification = run_checked([str(signer), 'verify', '--verbose', '--print-certs',
                                   '--min-sdk-version', '29', '--max-sdk-version', '35', str(candidate)], env, runner)
        signing = verify_signer(verification, config.expected_sha256)
        try:
            policy = binding.verify_apk(expected, candidate, 'release', sdk)
        except (Exception, SystemExit):
            raise ProductionError('Compiled APK/source/license policy failed; no production artifact published') from None
        badging = run_checked([str(aapt), 'dump', 'badging', str(candidate)], env, runner)
        package = re.search(r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging, re.MULTILINE)
        if (not package or package.groups() != ('ai.lastcore.android', str(config.version_code), config.version_name)
                or 'application-debuggable' in badging):
            raise ProductionError('Compiled APK identity/version is not the owner release')
        # A fake/stale verifier cannot silently supply a hash for different bytes.
        apk_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if (not isinstance(policy, dict) or policy.get('sha256') != apk_hash
                or policy.get('size_bytes') != candidate.stat().st_size):
            raise ProductionError('Verified APK bytes changed; no production artifact published')
        report = dict(expected, built_at=datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                      distribution='initial sideload; Google Play not assessed', version_code=config.version_code,
                      version_name=config.version_name, signing='owner production certificate verified',
                      production_signed=True, signer=signing, sha256=apk_hash, size_bytes=policy['size_bytes'],
                      device_acceptance='unverified', signature_verified_api_range=[29,35])
        staged = [candidate, stage/'provenance.json', stage/'checksum.txt']
        staged[1].write_text(json.dumps(report, indent=2) + '\n')
        staged[2].write_text(f'{apk_hash}  {destinations[0].name}\n')
        try:
            for source, destination in zip(staged, destinations):
                os.link(source, destination)  # Exclusive publication; never overwrite old bytes.
                created.append(destination)
        except OSError:
            for path in created:
                path.unlink()
            raise ProductionError('Publication failed; no production artifact retained') from None
    return report


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        raise ProductionError('Production arguments invalid; supply --signing-config and optional --output-dir')


def main(argv=None):
    try:
        parser = PrivateParser(description=__doc__)
        parser.add_argument('--signing-config', required=True)
        parser.add_argument('--output-dir')
        args = parser.parse_args(argv)
        report = build_production(args.signing_config, args.output_dir)
        print(json.dumps({'source_commit':report['source_commit'], 'sha256':report['sha256'],
                          'certificate_sha256':report['signer']['certificate_sha256'],
                          'production_signed':True}))
        return 0
    except (ProductionError, OSError, ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code == 0:
            return 0
        print('BLOCKED: production preparation failed; no verified production artifact published', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
