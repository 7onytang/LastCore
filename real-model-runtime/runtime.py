"""Local llama.cpp adapter. No network server or Controller protocol integration."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

MODEL_SHA256 = 'bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169'

class RuntimeFailure(RuntimeError):
    pass

class Cancelled(RuntimeFailure):
    pass

class TimedOut(RuntimeFailure):
    pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def stop_process(process):
    # Dedicated session contains backend children too; reap the direct child.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # Kill descendants that survived TERM even when their parent already exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_process(command, timeout, cancel=None):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive')
    if cancel and cancel.is_set():
        raise Cancelled('cancelled before launch')
    started = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True)
        try:
            while process.poll() is None:
                if cancel and cancel.is_set():
                    raise Cancelled('generation cancelled')
                if time.monotonic() - started >= timeout:
                    raise TimedOut('generation deadline exceeded')
                time.sleep(0.02)
            out.seek(0)
            err.seek(0)
            stdout = out.read(1024 * 1024).decode('utf-8', errors='replace')
            stderr = err.read(1024 * 1024).decode('utf-8', errors='replace')
            if process.returncode:
                raise RuntimeFailure(f'backend exited {process.returncode}: {stderr[-2000:]}')
            return stdout, stderr, time.monotonic() - started
        finally:
            stop_process(process)


class LocalRuntime:
    def __init__(self, backend, model, backend_hash):
        self.backend, self.model = Path(backend).resolve(), Path(model).resolve()
        if sha256(self.backend) != backend_hash:
            raise RuntimeFailure('backend checksum mismatch')
        if sha256(self.model) != MODEL_SHA256:
            raise RuntimeFailure('model checksum mismatch')

    def generate(self, prompt, tokens=32, timeout=60, cancel=None):
        if not prompt.strip() or len(prompt.encode('utf-8')) > 4096:
            raise ValueError('prompt must contain 1 to 4096 UTF-8 bytes')
        if not 1 <= tokens <= 256:
            raise ValueError('tokens must be between 1 and 256')
        # SmolLM2 chat template; CLI performs generation only, no interactive loop.
        formatted = '<|im_start|>user\n' + prompt + '<|im_end|>\n<|im_start|>assistant\n'
        command = [str(self.backend), '-m', str(self.model), '-p', formatted,
                   '-n', str(tokens), '-c', '2048', '-ngl', '0', '--temp', '0',
                   '--seed', '42', '--no-display-prompt', '--no-conversation', '--simple-io']
        output, diagnostics, elapsed = run_process(command, timeout, cancel)
        if not output.strip():
            raise RuntimeFailure('backend produced no output')
        return {'text': output.strip(), 'elapsed_seconds': round(elapsed, 3),
                'max_new_tokens': tokens, 'model_sha256': MODEL_SHA256,
                'scope': 'local-real-model', 'diagnostics': diagnostics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--tokens', type=int, default=32)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--backend', type=Path, default=Path(__file__).parent / '.cache/backend/build/bin/llama-completion')
    parser.add_argument('--model', type=Path, default=Path(__file__).parent / '.cache/model.gguf')
    parser.add_argument('--manifest', type=Path, default=Path(__file__).parent / '.cache/installed.json')
    args = parser.parse_args()
    cancel = threading.Event()
    old = {sig: signal.signal(sig, lambda *_: cancel.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        manifest = json.loads(args.manifest.read_text())
        result = LocalRuntime(args.backend, args.model, manifest['backend_sha256']).generate(
            args.prompt, args.tokens, args.timeout, cancel)
        print(json.dumps(result, ensure_ascii=False))
    except (RuntimeFailure, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}))
        return 1
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
