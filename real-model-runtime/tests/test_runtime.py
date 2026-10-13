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

    def test_real_subprocess_output_overflow_fails_without_truncated_success(self):
        from runtime import OUTPUT_LIMIT_BYTES
        for stream in ('stdout', 'stderr'):
            script = f'import sys;sys.{stream}.write("a"*({OUTPUT_LIMIT_BYTES}+1));sys.{stream}.flush()'
            with self.subTest(stream=stream), self.assertRaisesRegex(RuntimeFailure, f'{stream} exceeded'):
                run_process([sys.executable, '-c', script], 3)
        output, _, _ = run_process([sys.executable, '-c', f'import sys;sys.stdout.write("a"*{OUTPUT_LIMIT_BYTES})'], 3)
        self.assertEqual(len(output), OUTPUT_LIMIT_BYTES)

    def test_reused_instance_rejects_replaced_model_and_backend(self):
        import runtime
        for replace in ('model', 'backend'):
            with self.subTest(replace=replace), tempfile.TemporaryDirectory() as folder:
                backend, model = Path(folder) / 'backend', Path(folder) / 'model'
                backend.write_bytes(b'original-backend')
                model.write_bytes(b'original-model')
                with patch('runtime.MODEL_SHA256', runtime.sha256(model)):
                    engine = LocalRuntime(backend, model, runtime.sha256(backend))
                    with patch('runtime.run_process', return_value=('first response', '', .1)) as runner:
                        self.assertEqual(engine.generate('first')['text'], 'first response')
                        (model if replace == 'model' else backend).write_bytes(b'replaced-content')
                        with self.assertRaisesRegex(RuntimeFailure, f'{replace} checksum mismatch'):
                            engine.generate('second')
                        self.assertEqual(runner.call_count, 1)

    def test_process_group_children_removed_after_cancel_and_parent_early_exit(self):
        for early in (False, True):
            with self.subTest(early=early), tempfile.TemporaryDirectory() as folder:
                pidfile = Path(folder) / 'child-pid'
                child = ('import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                         f'open({str(pidfile)!r},"w").write(str(os.getpid()));time.sleep(30)')
                # Wait for child's signal handler before allowing the parent to exit.
                parent = ('import subprocess,sys,time,pathlib;'
                          f'subprocess.Popen([sys.executable,"-c",{child!r}]);'
                          f'p=pathlib.Path({str(pidfile)!r});'
                          'exec("while not p.exists(): time.sleep(.01)");'
                          f'time.sleep({.1 if early else 30})')
                event = threading.Event()
                timer = threading.Timer(.3, event.set)
                if not early:
                    timer.start()
                try:
                    if early:
                        run_process([sys.executable, '-c', parent], 3)
                    else:
                        with self.assertRaises(Cancelled):
                            run_process([sys.executable, '-c', parent], 3, event)
                    pid = int(pidfile.read_text())
                    for _ in range(40):
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(.025)
                    else:
                        self.fail('TERM-ignoring backend child survived group cleanup')
                finally:
                    timer.cancel()

if __name__ == '__main__':
    unittest.main()
