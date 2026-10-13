"""Child guardian: parent EOF, cancellation or lease expiry stops/reaps the native group."""
import json
import os
from pathlib import Path
import select
import sys
import threading
import time
import tempfile

import runtime


def supervise(fd, emit, factory=None):
    cancel = threading.Event()
    thread = None
    result = {}
    lease_deadline = 0.0
    buffer = b''
    completion_path = None

    def execute(request):
        try:
            root = Path(request['runtime_root'])
            installed = json.loads((root / '.cache/installed.json').read_text())
            engine = factory(request) if factory else runtime.LocalRuntime(
                root / '.cache/backend/build/bin/llama-completion', root / '.cache/model.gguf', installed['backend_sha256'])
            value = engine.generate(request['prompt'], tokens=request['max_tokens'],
                                    timeout=request['execution_timeout_seconds'], cancel=cancel)
            if len(value['text'].encode('utf-8')) > 65536:
                raise runtime.RuntimeFailure('result byte budget exceeded')
            result.update(status='succeeded', result={key: value[key] for key in ('text', 'elapsed_seconds', 'model_sha256')})
        except runtime.Cancelled:
            result.update(status='cancelled')
        except runtime.TimedOut:
            result.update(status='failed', error_code='deadline')
        except Exception:
            # Never copy native stderr, prompt, paths or command arguments to the Controller/logs.
            result.update(status='failed', error_code='backend_error')

    try:
        while thread is None or thread.is_alive():
            if thread is not None and time.monotonic() >= lease_deadline:
                cancel.set()
            readable, _, _ = select.select([fd], [], [], .02)
            if readable:
                data = os.read(fd, 16384)
                if not data:
                    cancel.set()
                    if thread is None:
                        return
                    break
                buffer += data
                if len(buffer) > 16384:
                    raise ValueError('guardian command exceeds byte budget')
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    message = json.loads(line)
                    if thread is None:
                        completion_path = Path(message['completion_path'])
                        lease_deadline = float(message['lease_deadline_monotonic'])
                        thread = threading.Thread(target=execute, args=(message,))
                        thread.start()
                    elif message.get('cancel'):
                        cancel.set()
                    else:
                        lease_deadline = float(message['lease_deadline_monotonic'])
        if thread is not None:
            thread.join()
        if completion_path is not None:
            # Persist positive cleanup evidence before emitting/losing parent output.
            result['backend_reaped'] = True
            fd_out, temporary = tempfile.mkstemp(prefix='.guardian-', dir=completion_path.parent)
            with os.fdopen(fd_out, 'w') as output:
                json.dump(result, output); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, completion_path)
        emit(result)
    finally:
        cancel.set()
        if thread is not None:
            thread.join()


if __name__ == '__main__':
    supervise(sys.stdin.fileno(), lambda value: print(json.dumps(value), flush=True))
