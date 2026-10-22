"""Offline monitor regressions: no real network, ACME request or compute task."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import ssl
import stat
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location(
    "tls_monitor", Path(__file__).resolve().parents[1] / "tls_monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
IP = "8.134.161.123"
NOW = 1_790_000_000
HEALTH = b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n{"status":"ok"}'
# Python 3.6 reports certificate handshake failures as SSLError.
VERIFY_ERROR = getattr(ssl, "SSLCertVerificationError", ssl.SSLError)


class CertificateChecks(unittest.TestCase):
    def probe(self, remaining=72 * 3600, lifetime=160 * 3600,
              san=IP, response=HEALTH, trust_error=None, fragments=None):
        end = NOW + remaining
        certificate = {
            "notBefore": time.strftime(
                "%b %d %H:%M:%S %Y GMT", time.gmtime(end - lifetime)),
            "notAfter": time.strftime(
                "%b %d %H:%M:%S %Y GMT", time.gmtime(end)),
            "subjectAltName": [("IP Address", san)],
            "serialNumber": "PUBLIC-SERIAL",
        }
        connection = MagicMock()
        connection.getpeercert.return_value = certificate
        connection.version.return_value = "TLSv1.3"
        connection.recv.side_effect = fragments or [response, b""]
        context = MagicMock()
        context.wrap_socket.return_value.__enter__.return_value = connection
        if trust_error:
            context.wrap_socket.side_effect = trust_error
        with patch.object(monitor.ssl, "create_default_context", return_value=context), \
                patch.object(monitor.socket, "create_connection") as connect, \
                patch.object(monitor.time, "time", return_value=NOW):
            result = monitor.check_tls("127.0.0.1", IP)
        connect.assert_called_once_with(("127.0.0.1", 443), timeout=8)
        context.wrap_socket.assert_called_once()
        self.assertEqual(context.wrap_socket.call_args[1]["server_hostname"], IP)
        connection.sendall.assert_called_once_with(
            ("GET /healthz HTTP/1.1\r\nHost: " + IP +
             "\r\nConnection: close\r\n\r\n").encode("ascii"))
        return result

    def test_valid_certificate_and_fragmented_health(self):
        result = self.probe(fragments=[HEALTH[:11], HEALTH[11:43], HEALTH[43:], b""])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["remaining_hours"], 72)
        self.assertEqual(result["certificate_serial"], "PUBLIC-SERIAL")
        self.assertTrue(result["trusted_chain"])
        self.assertEqual(result["https_health"], "ok")
        self.assertTrue(result["expires_at_beijing"].endswith("+08:00"))

    def test_warning_boundary(self):
        for seconds, expected in [(48 * 3600, "ok"), (48 * 3600 - 1, "warning")]:
            with self.subTest(seconds=seconds):
                self.assertEqual(self.probe(remaining=seconds)["status"], expected)

    def test_wrong_ip_or_long_certificate_rejected(self):
        for kwargs in [{"san": "192.0.2.1"}, {"lifetime": 7 * 86400 + 1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.probe(**kwargs)

    def test_untrusted_and_expired_certificates_fail_closed(self):
        # The real default SSL context rejects these during the handshake.
        for reason in ["certificate has expired", "unable to get local issuer certificate"]:
            with self.subTest(reason=reason), self.assertRaises(VERIFY_ERROR):
                self.probe(trust_error=VERIFY_ERROR(reason))

    def test_unhealthy_or_malformed_responses_rejected(self):
        for response in [HEALTH.replace(b"200 OK", b"503 Unavailable"),
                         HEALTH.replace(b'"ok"', b'"failed"'),
                         b'HTTP/1.1 200 OK\r\n\r\nnot-json',
                         b'HTTP/1.1 200 OK',
                         HEALTH + b"x" * 65536]:
            with self.subTest(response=response[:50]), self.assertRaises(ValueError):
                self.probe(response=response)

    def test_invalid_ip_rejected_before_connect(self):
        with patch.object(monitor.socket, "create_connection") as connect:
            with self.assertRaises(ValueError):
                monitor.check_tls("127.0.0.1", "not-an-ip")
        connect.assert_not_called()


class StateChecks(unittest.TestCase):
    def run_main(self, state, result=None, error=None):
        env = {"LASTCORE_TLS_STATUS_PATH": str(state),
               "LASTCORE_TLS_CONNECT_HOST": "127.0.0.1",
               "LASTCORE_TLS_SERVER_IP": IP, "LASTCORE_TLS_PORT": "443",
               "LASTCORE_TLS_WARN_HOURS": "48"}
        with patch.dict(os.environ, env), \
                patch.object(monitor, "check_tls", return_value=result, side_effect=error), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = monitor.main()
        return code, json.loads(output.getvalue())

    def test_exit_status_state_permissions_and_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "status.json"
            for status, expected in [("ok", 0), ("warning", 1)]:
                with self.subTest(status=status):
                    code, output = self.run_main(state, {"status": status})
                    self.assertEqual(code, expected)
                    self.assertEqual(json.loads(state.read_text()), output)
                    self.assertTrue(output["checked_at_beijing"].endswith("+08:00"))
                    if os.name != "nt":
                        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
                    self.assertFalse(state.with_suffix(".tmp").exists())

    def test_probe_failure_replaces_old_success_with_error(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "status.json"
            self.run_main(state, {"status": "ok"})
            code, output = self.run_main(state, error=VERIFY_ERROR("expired"))
            self.assertEqual(code, 2)
            self.assertEqual(output["status"], "error")
            self.assertEqual(json.loads(state.read_text()), output)
            self.assertNotIn("trusted_chain", output)

    def test_state_write_failure_never_reports_success(self):
        with tempfile.TemporaryDirectory() as folder:
            code, output = self.run_main(Path(folder) / "missing" / "status.json", {"status": "ok"})
            self.assertEqual(code, 2)
            self.assertEqual(output["status"], "error")
            self.assertIn("cannot save probe state", output["error"])


if __name__ == "__main__":
    unittest.main()
