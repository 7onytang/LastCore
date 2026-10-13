#!/usr/bin/env python3
"""Local transport regression with a test certificate and fake upstream; no ACME traffic."""
import copy
import http.client
import http.server
import importlib.util
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time

BASE = Path(__file__).resolve().parents[1]
TOKEN = 'test-only-bearer-sentinel-NOT-A-REAL-SECRET'
IP = '8.134.161.123'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Upstream(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        self.hits.append((self.path, self.headers.get('Authorization')))
        authorized = self.headers.get('Authorization') == 'Bearer ' + TOKEN
        status = 200 if self.path == '/healthz' or authorized else 401
        body = json.dumps({'status': 'ok'} if self.path == '/healthz' else {'authorized': authorized}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    config = json.loads((BASE / 'evidence/adapted.json').read_text())
    assert config['admin']['disabled'] is True
    policy = config['apps']['tls']['automation']['policies']
    assert len(policy) == 1 and policy[0]['subjects'] == [IP]
    issuer = policy[0]['issuers'][0]
    assert len(policy[0]['issuers']) == 1 and issuer['module'] == 'acme'
    assert issuer['profile'] == 'shortlived'
    assert issuer['ca'] == 'https://acme-v02.api.letsencrypt.org/directory'
    assert issuer['test_ca'] == 'https://acme-staging-v02.api.letsencrypt.org/directory'
    assert issuer['challenges']['tls-alpn']['disabled'] is True
    def handlers(routes):
        for route in routes:
            for item in route['handle']:
                yield item
                yield from handlers(item.get('routes', []))
    servers = config['apps']['http']['servers']
    assert len(servers) == 2
    http_server = next(s for s in servers.values() if s['listen'] == [':80'])
    https_server = next(s for s in servers.values() if s['listen'] == [':443'])
    assert not any(h['handler'] == 'reverse_proxy' for h in handlers(http_server['routes']))
    assert not any('match' in route for route in http_server['routes'])
    proxy = [h for h in handlers(https_server['routes']) if h['handler'] == 'reverse_proxy']
    assert len(proxy) == 1 and proxy[0]['upstreams'] == [{'dial': '127.0.0.1:8000'}]
    assert all(p['default_sni'] == IP for p in https_server['tls_connection_policies'])
    assert not any(p.get('log_credentials') for p in servers.values())
    upstream_port, http_port, https_port = free_port(), free_port(), free_port()
    upstream = http.server.ThreadingHTTPServer(('127.0.0.1', upstream_port), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    process = None
    with tempfile.TemporaryDirectory(prefix='lastcore-proxy-test-') as td:
        temp = Path(td)
        cert_config = temp / 'openssl.cnf'
        cert_config.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n'
                               '[dn]\nCN=LastCore LOCAL TEST ONLY\n[ext]\nsubjectAltName=IP:' + IP + '\n'
                               'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-config', str(cert_config), '-keyout', str(temp / 'key.pem'),
                        '-out', str(temp / 'cert.pem')], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        test_config = copy.deepcopy(config)
        test_config['storage']['root'] = str(temp / 'state')
        test_config['apps']['tls'] = {'certificates': {'load_files': [{'certificate': str(temp / 'cert.pem'), 'key': str(temp / 'key.pem')}]}}
        test_servers = test_config['apps']['http']['servers']
        for server in test_servers.values():
            server['automatic_https'] = {'disable_certificates': True, 'disable_redirects': True}
            if server['listen'] == [':80']:
                server['listen'] = ['127.0.0.1:' + str(http_port)]
            else:
                server['listen'] = ['127.0.0.1:' + str(https_port)]
                for handler in handlers(server['routes']):
                    if handler['handler'] == 'reverse_proxy':
                        handler['upstreams'][0]['dial'] = '127.0.0.1:' + str(upstream_port)
        cfg = temp / 'config.json'
        cfg.write_text(json.dumps(test_config))
        log = temp / 'caddy.log'
        try:
            with log.open('wb') as sink:
                process = subprocess.Popen([str(BASE / 'tools/mac_arm64/caddy'), 'run', '--config', str(cfg)],
                                           stdout=sink, stderr=sink, env={'PATH': '/usr/bin:/bin', 'HOME': td,
                                                                       'XDG_DATA_HOME': td, 'XDG_CONFIG_HOME': td})
            deadline = time.monotonic() + 10
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', http_port), timeout=0.3):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise AssertionError('Caddy did not become ready: ' + log.read_text())
                    time.sleep(0.1)
            context = ssl.create_default_context(cafile=str(temp / 'cert.pem'))
            checks = []
            def request(tls, path, authorization=None, host=IP):
                headers = {'Host': host}
                if authorization:
                    headers['Authorization'] = authorization
                if tls:
                    conn = http.client.HTTPSConnection('127.0.0.1', https_port, context=context, timeout=5)
                    # Numeric IP: TLS uses no SNI; trust checks still verify the IP SAN.
                    conn.sock = context.wrap_socket(socket.create_connection(('127.0.0.1', https_port), timeout=5), server_hostname=IP)
                else:
                    conn = http.client.HTTPConnection('127.0.0.1', http_port, timeout=5)
                conn.request('GET', path, headers=headers)
                response = conn.getresponse()
                status, body = response.status, response.read()
                conn.close()
                return status, body
            for path in ['/healthz', '/v1/nodes', '/.well-known/acme-challenge/nonexistent']:
                for host in [IP, 'unknown.example']:
                    assert request(False, path, 'Bearer ' + TOKEN, host)[0] == 400
            assert not Upstream.hits
            checks.append('all plaintext paths/hosts rejected without forwarding')
            assert request(True, '/healthz')[0] == 200
            assert request(True, '/v1/nodes')[0] == 401
            assert request(True, '/v1/nodes', 'Bearer wrong')[0] == 401
            assert request(True, '/v1/nodes', 'Bearer ' + TOKEN)[0] == 200
            checks.append('no-SNI IP TLS and authorized/unauthorized forwarding passed')
            spec = importlib.util.spec_from_file_location('tls_monitor', BASE / 'ops/tls_monitor.py')
            monitor = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(monitor)
            original_context = monitor.ssl.create_default_context
            monitor.ssl.create_default_context = lambda: original_context(cafile=str(temp / 'cert.pem'))
            result = monitor.check_tls('127.0.0.1', IP, https_port, 48)
            assert result['status'] == 'warning' and result['tls_version'] in ['TLSv1.2', 'TLSv1.3']
            checks.append('monitor trusted SAN/health/expiry warning tested with local test CA only')
            monitor.ssl.create_default_context = original_context
            try:
                monitor.check_tls('127.0.0.1', IP, https_port, 48)
            except ssl.SSLCertVerificationError:
                checks.append('monitor rejects untrusted test certificate with default trust store')
            else:
                raise AssertionError('monitor accepted untrusted test CA')
            upstream.shutdown()
            upstream.server_close()
            assert request(True, '/v1/nodes', 'Bearer ' + TOKEN)[0] == 502
            time.sleep(0.1)
            assert TOKEN not in log.read_text()
            checks.append('upstream failure returns 502 and bearer sentinel absent from logs')
            print(json.dumps({'scope': 'LOCAL MOCK TRANSPORT; NO ACME ACCOUNT OR CERTIFICATE REQUESTED', 'checks': checks}, indent=2))
        finally:
            if process:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if thread.is_alive():
                upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    main()
