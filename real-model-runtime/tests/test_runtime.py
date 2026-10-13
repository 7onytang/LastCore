import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import run_process, Cancelled, TimedOut, RuntimeFailure, LocalRuntime

class LifecycleTests(unittest.TestCase):
    def test_success(self):
        output, _, _ = run_process([sys.executable, '-c', 'print("ok")'], 3)
        self.assertEqual(output.strip(), 'ok')

    def test_backend_failure(self):
        with self.assertRaisesRegex(RuntimeFailure, 'exited 7'):
            run_process([sys.executable, '-c', 'import sys;sys.exit(7)'], 3)

    def test_timeout_reaps_process(self):
        self.check_cleanup(False)

    def test_cancellation_reaps_process(self):
        self.check_cleanup(True)

    def check_cleanup(self, cancel_it):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / 'pid'
            script = 'import os,time;open(' + repr(str(pidfile)) + ',"w").write(str(os.getpid()));time.sleep(30)'
            event = threading.Event()
            timer = threading.Timer(.25, event.set)
            if cancel_it:
                timer.start()
            try:
                with self.assertRaises(Cancelled if cancel_it else TimedOut):
                    run_process([sys.executable, '-c', script], .5, event)
                pid = int(pidfile.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                timer.cancel()

    def test_invalid_deadlines(self):
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                run_process(['/does/not/exist'], timeout)

    def test_caller_exception_reaps_process(self):
        import runtime
        original = runtime.subprocess.Popen
        launched = []
        def track(*args, **kwargs):
            process = original(*args, **kwargs)
            launched.append(process)
            return process
        original_sleep = time.sleep
        first = True
        def interrupt_once(seconds):
            nonlocal first
            if first:
                first = False
                raise KeyboardInterrupt
            original_sleep(seconds)
        with patch('runtime.subprocess.Popen', side_effect=track), patch('runtime.time.sleep', side_effect=interrupt_once):
            with self.assertRaises(KeyboardInterrupt):
                run_process([sys.executable, '-c', 'import time;time.sleep(30)'], 5)
        self.assertIsNotNone(launched[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(launched[0].pid, 0)

    def test_pre_cancel_does_not_launch(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            run_process(['/does/not/exist'], 1, event)

    def test_model_integrity(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / 'model'
            model.write_bytes(b'corrupt')
            from runtime import sha256
            with self.assertRaisesRegex(RuntimeFailure, 'model checksum mismatch'):
                LocalRuntime(sys.executable, model, sha256(sys.executable))

if __name__ == '__main__':
    unittest.main()
