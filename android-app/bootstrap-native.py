#!/usr/bin/env python3
"""Fetch and verify only pinned MIT llama.cpp sources, never model weights."""
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / '.native-cache'
COMMIT = '8345f333951c661d166b00e6f9362e553768f292'
SHA256 = '84050e783dbd0c4a4678f90f253b0881df04e90a98c20f73ae3709751c4a4850'
URL = f'https://github.com/ggml-org/llama.cpp/archive/{COMMIT}.tar.gz'
CACHE.mkdir(exist_ok=True)
archive = CACHE / 'llama.cpp.tar.gz'
if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
    temporary = archive.with_suffix('.download')
    try:
        with urllib.request.urlopen(URL, timeout=60) as stream, temporary.open('wb') as out:
            total = 0
            while chunk := stream.read(1024*1024):
                total += len(chunk)
                if total > 50*1024*1024: raise RuntimeError('source download exceeds bound')
                out.write(chunk)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != SHA256: raise RuntimeError('source archive checksum mismatch')
        temporary.replace(archive)
    finally: temporary.unlink(missing_ok=True)
# Re-extract hash-verified sources each invocation; an edited cache cannot silently compile.
source = CACHE / 'llama.cpp'
if source.exists(): shutil.rmtree(source)
with tarfile.open(archive) as tar:
    tar.extractall(CACHE / 'extract', filter='data')
(CACHE / 'extract' / f'llama.cpp-{COMMIT}').rename(source)
(CACHE / 'extract').rmdir()
license_path = ROOT / 'app/src/main/assets/licenses/llama.cpp-LICENSE.txt'
if source.joinpath('LICENSE').read_bytes() != license_path.read_bytes(): raise RuntimeError('pinned MIT license differs')
(CACHE / 'verified-source.json').write_text(json.dumps({'backend_commit':COMMIT, 'source_archive_sha256':SHA256, 'url':URL},indent=2)+'\n')
print('Verified fixed llama.cpp source:', COMMIT)
