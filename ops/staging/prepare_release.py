"""Export a clean fixed Git commit to an immutable source archive + detached manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]

def archived_lock_digest(archive, revision):
    """Bind the manifest to bytes actually exported from the pinned commit."""
    with tarfile.open(archive, 'r:gz') as bundle:
        member = bundle.getmember(f'{revision}/ops/staging/requirements-linux-lock.txt')
        if not member.isfile():
            raise ValueError('archived runtime lock must be a regular file')
        with bundle.extractfile(member) as lock:
            return hashlib.file_digest(lock, 'sha256').hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    for extra in ([], ['--cached']):
        subprocess.run(['git', 'diff', '--exit-code', *extra], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    archive = args.output_dir / f'lastcore-{revision}.tar.gz'
    metadata = archive.with_suffix('.manifest.json')
    checksum = archive.with_suffix('.sha256')
    if any(path.exists() or path.is_symlink() for path in (archive, metadata, checksum)):
        raise FileExistsError('release artifact already exists')
    # Exclusive creation prevents concurrent invocation from replacing an archive.
    # Only remove the archive after this invocation exclusively created it.
    output = archive.open('xb')
    try:
        with output:
            subprocess.run(['git', 'archive', '--format=tar.gz', f'--prefix={revision}/', revision], cwd=ROOT, check=True, stdout=output)
        runtime_lock_digest = archived_lock_digest(archive, revision)
    except BaseException:
        archive.unlink()
        raise
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {'source_commit': revision, 'source_tracked_clean': True,
                'packaged_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                'archive': archive.name, 'archive_bytes': archive.stat().st_size, 'archive_sha256': digest,
                'scope': 'prepared-source-only; no deployment',
                'python_base_image': 'python:3.12-slim@sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe',
                'runtime_lock_sha256': runtime_lock_digest}
    with metadata.open('x') as output:
        output.write(json.dumps(manifest, indent=2)+'\n')
    with checksum.open('x') as output:
        output.write(f'{digest}  {archive.name}\n')
    print(json.dumps(manifest, indent=2))

if __name__ == '__main__':
    main()
