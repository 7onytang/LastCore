"""Download pinned public artifacts and build only a local llama-completion."""
import json
from pathlib import Path
import subprocess
import tarfile
import urllib.request

from runtime import MODEL_SHA256, sha256

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / '.cache'
COMMIT = '8345f333951c661d166b00e6f9362e553768f292'
SOURCE_SHA256 = '84050e783dbd0c4a4678f90f253b0881df04e90a98c20f73ae3709751c4a4850'
REVISION = '04f7f8a05a8e4bc208e88d11e8c9122be36c2f9b'
MODEL_URL = f'https://huggingface.co/lmstudio-community/SmolLM2-135M-Instruct-GGUF/resolve/{REVISION}/SmolLM2-135M-Instruct-Q4_K_M.gguf'
SOURCE_URL = f'https://github.com/ggml-org/llama.cpp/archive/{COMMIT}.tar.gz'


def download(url, path, digest, maximum):
    if path.exists() and sha256(path) == digest:
        return
    temporary = path.with_suffix('.partial')
    try:
        count = 0
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open('wb') as out:
            while block := response.read(1024 * 1024):
                count += len(block)
                if count > maximum:
                    raise RuntimeError('artifact exceeds download size limit')
                out.write(block)
        if sha256(temporary) != digest:
            raise RuntimeError('artifact checksum mismatch')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    CACHE.mkdir(exist_ok=True)
    source = CACHE / 'backend.tar.gz'
    download(SOURCE_URL, source, SOURCE_SHA256, 100 * 1024 * 1024)
    download(MODEL_URL, CACHE / 'model.gguf', MODEL_SHA256, 300 * 1024 * 1024)
    backend = CACHE / 'backend'
    if not backend.exists():
        with tarfile.open(source) as archive:
            archive.extractall(CACHE, filter='data')
        (CACHE / f'llama.cpp-{COMMIT}').rename(backend)
    cmake = ROOT / '.venv/bin/cmake'
    if not cmake.exists():
        raise RuntimeError('Install local CMake first; see README.md')
    subprocess.run([str(cmake), '-S', str(backend), '-B', str(backend / 'build'),
                    '-DCMAKE_BUILD_TYPE=Release', f'-DLLAMA_BUILD_COMMIT={COMMIT}', '-DLLAMA_BUILD_NUMBER=0', '-DLLAMA_CURL=OFF', '-DGGML_METAL=OFF',
                    '-DLLAMA_BUILD_TESTS=OFF', '-DLLAMA_BUILD_SERVER=OFF'], check=True)
    subprocess.run([str(cmake), '--build', str(backend / 'build'), '--target', 'llama-completion', '-j', '4'], check=True)
    manifest = {'backend_commit': COMMIT, 'backend_license': 'MIT', 'source_sha256': SOURCE_SHA256,
                'backend_sha256': sha256(backend / 'build/bin/llama-completion'),
                'model_revision': REVISION, 'model_license': 'Apache-2.0',
                'model_sha256': MODEL_SHA256, 'model_bytes': (CACHE / 'model.gguf').stat().st_size}
    (CACHE / 'installed.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest))

if __name__ == '__main__':
    main()
