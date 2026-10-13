"""Protocol/HTTP fixture regressions: no real model, real credential or cloud request."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import worker

HTTP_CLIENT = httpx.Client
ORIGIN = 'https://controller.example.invalid'
INSTALLED = {'backend_sha256': '0' * 64}
NODE = {'node_id': '11111111-1111-1111-1111-111111111111', 'node_token': 'synthetic-node-token-only-for-tests'}
JOB = {'id': '22222222-2222-2222-2222-222222222222', 'lease_epoch': 1, '_execution_key': 'a' * 64}


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lastcore-worker-recovery-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.config = {'controller_url': ORIGIN, 'state_dir': str(self.state),
                       'runtime_root': str(self.root / 'no-model-cache'), 'registration_token': 'synthetic-registration-token'}
        self.requests = []

    def private(self, name, value):
        path = self.state / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def node(self, origin=ORIGIN):
        value = dict(NODE)
        if origin is not None:
            value['controller_origin'] = origin
        return self.private('node-credentials.json', value)

    def transport(self, handler):
        def record(request):
            self.requests.append(request)
            return handler(request)
        def client(**kwargs):
            return HTTP_CLIENT(**kwargs, transport=httpx.MockTransport(record))
        return patch.object(worker.httpx, 'Client', side_effect=client)

    def close(self, instance):
        instance.http.close()
        instance.lock.close()

    def test_confirmed_rejection_allows_corrected_registration_but_unknown_does_not(self):
        for status in (401, 403, 422):
            with self.subTest(status=status):
                self.requests.clear()
                self.state.joinpath('node-credentials.json').unlink(missing_ok=True)
                def handler(request):
                    if request.url.path.endswith('/register'):
                        if request.headers['Authorization'].endswith('synthetic-wrong-token'):
                            return httpx.Response(status, json={'detail': 'rejected'})
                        return httpx.Response(201, json=NODE)
                    return httpx.Response(200, json={})
                with self.transport(handler), patch.object(worker.Worker, 'verify_installation', return_value=INSTALLED):
                    bad = dict(self.config, registration_token='synthetic-wrong-token')
                    with self.assertRaises(worker.WorkerFailure) as failure:
                        worker.Worker(bad)
                    self.assertEqual(failure.exception.status, status)
                    self.assertFalse((self.state / 'registration.pending').exists())
                    instance = worker.Worker(self.config)
                    self.close(instance)
                self.assertEqual(sum(r.url.path.endswith('/register') for r in self.requests), 2)
                self.assertEqual(json.loads((self.state / 'node-credentials.json').read_text())['controller_origin'], ORIGIN)
        self.state.joinpath('node-credentials.json').unlink()
        for mode in ('timeout', 'server500', 'invalid201', 'invalid-node201'):
            with self.subTest(mode=mode):
                self.requests.clear()
                self.state.joinpath('registration.pending').unlink(missing_ok=True)
                def unknown(request):
                    if mode == 'timeout':
                        raise httpx.ReadTimeout('synthetic unknown outcome', request=request)
                    if mode == 'invalid-node201':
                        return httpx.Response(201, json={'node_id': NODE['node_id'], 'node_token': ''})
                    return httpx.Response(500 if mode == 'server500' else 201, content=b'not-json')
                with self.transport(unknown), patch.object(worker.Worker, 'verify_installation', return_value=INSTALLED):
                    with self.assertRaises(Exception):
                        worker.Worker(self.config)
                    self.assertTrue((self.state / 'registration.pending').exists())
                    with self.assertRaisesRegex(worker.WorkerFailure, 'outcome is unknown'):
                        worker.Worker(self.config)
                    self.assertEqual(len(self.requests), 1)

    def test_missing_empty_or_header_invalid_registration_never_marks_unsent_request_unknown(self):
        for token in (None, '', 'contains\nnewline', 'contains space', '非ASCII'):
            with self.subTest(token=repr(token)), patch.object(worker.httpx, 'Client') as client:
                config = dict(self.config)
                if token is None:
                    config.pop('registration_token')
                else:
                    config['registration_token'] = token
                with self.assertRaisesRegex(worker.WorkerFailure, 'valid registration credential'):
                    worker.Worker(config)
                self.assertFalse((self.state / 'registration.pending').exists())
                client.assert_not_called()

    def test_origin_mismatch_and_legacy_require_offline_explicit_migration(self):
        path = self.node()
        original = path.read_bytes()
        with patch.object(worker.httpx, 'Client') as client:
            with self.assertRaisesRegex(worker.WorkerFailure, 'another Controller'):
                worker.Worker(dict(self.config, controller_url='https://other.example.invalid'))
            client.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        path = self.node(origin=None)
        original = path.read_bytes()
        ledger = self.private('current-job.json', {'job': JOB, 'phase': 'stopped', 'outcome': {'status': 'failed', 'error_code': 'worker_restart'}})
        ledger_bytes = ledger.read_bytes()
        with patch.object(worker.httpx, 'Client') as client:
            with self.assertRaisesRegex(worker.WorkerFailure, 'legacy credentials lack'):
                worker.Worker(self.config)
            with self.assertRaisesRegex(worker.WorkerFailure, 'must match'):
                worker.bind_legacy_origin(self.config, 'https://different.example.invalid')
            self.assertEqual(path.read_bytes(), original)
            worker.bind_legacy_origin(self.config, 'https://CONTROLLER.example.invalid:443/')
            client.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['controller_origin'], ORIGIN)
        self.assertEqual(ledger.read_bytes(), ledger_bytes)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(worker.WorkerFailure, 'another Controller'):
            worker.bind_legacy_origin(dict(self.config, controller_url='https://other.example.invalid'), 'https://other.example.invalid')
        with patch.object(worker.httpx, 'Client') as client:
            with self.assertRaisesRegex(worker.WorkerFailure, 'already running'):
                state, lock = worker.open_state(self.config)
                try:
                    worker.bind_legacy_origin(self.config, ORIGIN)
                finally:
                    lock.close()
            client.assert_not_called()

    def test_origin_normalization_preserves_authority_and_refuses_paths(self):
        for value, expected in [('https://EXAMPLE.com:443/', 'https://example.com'), ('https://example.com:8443', 'https://example.com:8443'), ('https://BÜCHER.example/', 'https://xn--bcher-kva.example'), ('https://[0:0:0:0:0:0:0:1]:443/', 'https://[::1]')]:
            self.assertEqual(worker.validate_url(value), expected)
        for value in ['https://example.com/path', 'https://example.com:0', 'https://example.com//', 'https://example.com?token=hidden', 'https://user:hidden@example.com']:
            with self.assertRaises(worker.WorkerFailure) as failure:
                worker.validate_url(value)
            self.assertNotIn('hidden', str(failure.exception))

    def test_stopped_and_positive_cleanup_recover_before_missing_model_without_new_claim(self):
        for phase in ('stopped', 'positive-completion', 'claimed'):
            with self.subTest(phase=phase):
                self.requests.clear()
                self.node()
                value = {'job': JOB, 'phase': phase}
                if phase == 'stopped':
                    value['outcome'] = {'status': 'failed', 'error_code': 'worker_restart'}
                elif phase == 'positive-completion':
                    completion = self.private('completion.json', {'backend_reaped': True, 'status': 'cancelled'})
                    value.update(phase='launched', completion_path=str(completion))
                ledger = self.private('current-job.json', value)
                with self.transport(lambda request: httpx.Response(200, json={})), patch.object(worker.subprocess, 'Popen') as launch:
                    with self.assertRaises(FileNotFoundError):
                        worker.Worker(self.config)
                    launch.assert_not_called()
                self.assertFalse(ledger.exists())
                self.assertEqual([r.url.path for r in self.requests], ['/v1/inference/jobs/' + JOB['id'] + '/report'])
                body = json.loads(self.requests[0].content)
                self.assertEqual(body['execution_key'], JOB['_execution_key'])
                self.assertEqual(body['status'], 'failed')
                self.assertEqual(body['error_code'], 'worker_restart')

    def test_unknown_report_retains_exact_outcome_and_does_not_ack_or_check_model(self):
        self.node()
        ledger = self.private('current-job.json', {'job': JOB, 'phase': 'stopped', 'outcome': {'status': 'failed', 'error_code': 'deadline'}})
        before = ledger.read_bytes()
        def unknown(request):
            raise httpx.ReadTimeout('synthetic report uncertainty', request=request)
        with self.transport(unknown), patch.object(worker.Worker, 'verify_installation') as verify, patch.object(worker.subprocess, 'Popen') as launch:
            for _ in range(2):
                with self.assertRaises(worker.WorkerFailure):
                    worker.Worker(self.config)
                self.assertEqual(ledger.read_bytes(), before)
            verify.assert_not_called();launch.assert_not_called()
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(r.url.path.endswith('/report') for r in self.requests))
        self.assertEqual(self.requests[0].content, self.requests[1].content)

    def test_unknown_native_cleanup_never_reports_or_launches(self):
        self.node()
        ledger = self.private('current-job.json', {'job': JOB, 'phase': 'launched'})
        with self.transport(lambda request: httpx.Response(200, json={})), patch.object(worker.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(worker.WorkerFailure, 'stop is unconfirmed'):
                worker.Worker(self.config)
            launch.assert_not_called()
        self.assertTrue(ledger.exists());self.assertEqual(self.requests, [])

    def test_confirmed_report_conflict_acknowledges_only_after_positive_stop(self):
        self.node()
        ledger = self.private('current-job.json', {'job': JOB, 'phase': 'stopped', 'outcome': {'status': 'failed', 'error_code': 'worker_restart'}})
        def conflict(request):
            return httpx.Response(409 if request.url.path.endswith('/report') else 200, json={})
        with self.transport(conflict), patch.object(worker.subprocess, 'Popen') as launch:
            with self.assertRaises(FileNotFoundError):
                worker.Worker(self.config)
            launch.assert_not_called()
        self.assertFalse(ledger.exists())
        self.assertEqual([r.url.path.rsplit('/', 1)[-1] for r in self.requests], ['report', 'stop-ack'])


if __name__ == '__main__':
    unittest.main()
