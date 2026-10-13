#!/usr/bin/env python3
"""Package committed TLS ops + digest-pinned Caddy. Never performs remote operations."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
PREFIX = 'ops/public-ip-https/'
FILES = ['Caddyfile', 'Caddyfile.staging', 'install.sh', 'start.sh', 'rollback.sh', 'tls_monitor.py',
         'lastcore-caddy.service', 'lastcore-tls-monitor.service', 'lastcore-tls-monitor.timer',
         'README.md', 'SOURCES.json', 'tests/proxy_smoke.py']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--artifact-cache', type=Path)
    parser.add_argument('--local-test', action='store_true', help='Also package Mac arm64 Caddy and validate its config locally')
    args = parser.parse_args()
    for extra in [[], ['--cached']]:
        subprocess.run(['git', 'diff', '--exit-code', *extra], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    target = args.output_dir
    (target / 'ops').mkdir()
    (target / 'research').mkdir()
    (target / 'tools').mkdir()
    (target / 'tests').mkdir()
    (target / 'evidence').mkdir()
    # Export exact tracked commit contents instead of copying arbitrary working-tree files.
    for filename in FILES:
        contents = subprocess.check_output(['git', 'show', commit + ':' + PREFIX + filename], cwd=ROOT)
        output = target / ('tests/proxy_smoke.py' if filename.startswith('tests/') else 'ops/' + filename)
        output.write_bytes(contents)
        if filename.endswith('.sh'):
            output.chmod(0o755)
    sources = json.loads((target / 'ops/SOURCES.json').read_text())
    def fetch(entry, filename):
        dest = target / filename
        cached = args.artifact_cache / filename if args.artifact_cache else None
        if cached and cached.is_file():
            shutil.copyfile(cached, dest)
        else:
            with urllib.request.urlopen(entry['url'], timeout=60) as response:
                dest.write_bytes(response.read())
        if digest(dest) != entry['sha256']:
            raise ValueError('Official artifact digest mismatch: ' + filename)
        return dest
    fetch(sources['linux_amd64'], 'tools/caddy_2.11.7_linux_amd64.tar.gz')
    fetch(sources['license'], 'research/CADDY-LICENSE.txt')
    manifest = {'source_commit': commit, 'caddy_version': sources['caddy_version'],
                'packaged_at_beijing': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
                'scope': 'prepared-only; no remote install, no ACME account or certificate request',
                'public_ip': '8.134.161.123', 'upstream': '127.0.0.1:8000'}
    (target / 'source.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if args.local_test:
        archive = fetch(sources['mac_arm64'], 'tools/caddy_2.11.7_mac_arm64.tar.gz')
        binary_dir = target / 'tools/mac_arm64'
        binary_dir.mkdir()
        # Extract only a known single regular file; never a whole archive.
        with tarfile.open(archive) as bundle:
            member = bundle.getmember('caddy')
            if not member.isfile():
                raise ValueError('Caddy archive member is not a regular file')
            with bundle.extractfile(member) as stream:
                (binary_dir / 'caddy').write_bytes(stream.read())
        caddy = binary_dir / 'caddy'
        caddy.chmod(0o755)
        with (target / 'evidence/adapted.json').open('wb') as output:
            subprocess.run([str(caddy), 'adapt', '--config', str(target / 'ops/Caddyfile'), '--adapter', 'caddyfile', '--pretty'], check=True, stdout=output)
        with (target / 'evidence/validate.txt').open('wb') as output:
            subprocess.run([str(caddy), 'validate', '--config', str(target / 'ops/Caddyfile'), '--adapter', 'caddyfile'], check=True, stdout=output, stderr=output)
    # Include every packaged input and evidence generated so far. Future test logs go outside package.
    paths = sorted(path for path in target.rglob('*') if path.is_file())
    (target / 'SHA256SUMS').write_text(''.join(digest(path) + '  ' + str(path.relative_to(target)) + '\n' for path in paths))
    manifest['package_manifest_sha256'] = digest(target / 'SHA256SUMS')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
