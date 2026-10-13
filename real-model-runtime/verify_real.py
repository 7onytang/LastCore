"""Opt-in real model smoke and cancellation evidence; run after bootstrap."""
import json
import os
from pathlib import Path
import platform
import threading
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import runtime


def main():
    root = Path(__file__).resolve().parent
    evidence = root / 'evidence'
    evidence.mkdir(exist_ok=True)
    manifest = json.loads((root / '.cache/installed.json').read_text())
    engine = runtime.LocalRuntime(root / '.cache/backend/build/bin/llama-completion',
                                  root / '.cache/model.gguf', manifest['backend_sha256'])
    smoke = engine.generate('What is the capital of France?', tokens=32, timeout=60)
    assert 'Paris' in smoke['text'], 'Smoke prompt answer changed; inspect diagnostics'
    assert 'prompt eval time' in smoke['diagnostics'] and 'eval time' in smoke['diagnostics']
    (evidence / 'smoke.json').write_text(json.dumps(smoke, ensure_ascii=False, indent=2) + '\n')
    original = runtime.subprocess.Popen
    checks = []
    for mode in ('timeout', 'cancel'):
        launched = []
        def track(*args, **kwargs):
            process = original(*args, **kwargs)
            launched.append(process)
            return process
        event = threading.Event()
        timer = threading.Timer(.05, event.set)
        if mode == 'cancel':
            timer.start()
        try:
            with patch('runtime.subprocess.Popen', side_effect=track):
                engine.generate('Write a long story about Paris.', tokens=256,
                                timeout=.02 if mode == 'timeout' else 60, cancel=event)
            raise AssertionError('Expected interruption')
        except (runtime.TimedOut, runtime.Cancelled) as error:
            assert isinstance(error, runtime.TimedOut if mode == 'timeout' else runtime.Cancelled)
            assert launched[0].returncode is not None
            try:
                os.kill(launched[0].pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError('Backend process survived')
            checks.append({'case': mode, 'result': type(error).__name__, 'backend_reaped': True})
        finally:
            timer.cancel()
    result = {'checked_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'platform': platform.platform(), 'architecture': platform.machine(),
              'manifest': manifest, 'real_backend_lifecycle': checks,
              'limitations': ['local CPU only', 'Controller and communication protocols unchanged',
                              'Android device not verified', 'No distributed model integration']}
    (evidence / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'text': smoke['text'], 'lifecycle': checks}))

if __name__ == '__main__':
    main()
