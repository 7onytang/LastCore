#!/usr/bin/env python3
"""A local, token-free probe. Journal + a restricted state file, no external alerts."""
import datetime
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time


def check_tls(connect_host, server_ip, port=443, warn_hours=48):
    ipaddress.ip_address(server_ip)
    context = ssl.create_default_context()
    if hasattr(ssl, 'TLSVersion'):
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    else:
        context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    with socket.create_connection((connect_host, port), timeout=8) as raw:
        with context.wrap_socket(raw, server_hostname=server_ip) as connection:
            connection.settimeout(8)
            certificate = connection.getpeercert()
            tls_version = connection.version()
            not_after = ssl.cert_time_to_seconds(certificate['notAfter'])
            not_before = ssl.cert_time_to_seconds(certificate['notBefore'])
            remaining = not_after - time.time()
            sans = certificate.get('subjectAltName', [])
            if ('IP Address', server_ip) not in sans:
                raise ValueError('expected IP absent from certificate SAN')
            # Reject an unexpectedly long IP certificate, even if the chain is trusted.
            if not_after - not_before > 7 * 86400:
                raise ValueError('IP certificate is longer than the expected shortlived profile')
            connection.sendall(('GET /healthz HTTP/1.1\r\nHost: ' + server_ip +
                                '\r\nConnection: close\r\n\r\n').encode('ascii'))
            response = b''
            while True:
                part = connection.recv(8192)
                if not part:
                    break
                response += part
                if len(response) > 65536:
                    raise ValueError('health response exceeded size limit')
            headers, separator, body = response.partition(b'\r\n\r\n')
            status_line = headers.split(b'\r\n', 1)[0].split()
            if not separator or len(status_line) < 2 or status_line[1] != b'200':
                raise ValueError('HTTPS health endpoint did not return 200')
            # healthz is a small JSON response with Content-Length, never bearer-authenticated.
            if json.loads(body).get('status') != 'ok':
                raise ValueError('HTTPS health endpoint did not return expected status')
    return {
        'status': 'warning' if remaining < warn_hours * 3600 else 'ok',
        'server_ip': server_ip,
        'trusted_chain': True,
        'tls_version': tls_version,
        'certificate_serial': certificate.get('serialNumber'),
        'not_after_unix': int(not_after),
        'expires_at_beijing': datetime.datetime.fromtimestamp(
            not_after, datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
        'remaining_hours': round(remaining / 3600, 2),
        'https_health': 'ok',
    }


def main():
    state = Path(os.environ.get('LASTCORE_TLS_STATUS_PATH', '/var/lib/lastcore-caddy/tls-monitor.json'))
    try:
        result = check_tls(
            os.environ.get('LASTCORE_TLS_CONNECT_HOST', '127.0.0.1'),
            os.environ.get('LASTCORE_TLS_SERVER_IP', '8.134.161.123'),
            int(os.environ.get('LASTCORE_TLS_PORT', '443')),
            int(os.environ.get('LASTCORE_TLS_WARN_HOURS', '48')),
        )
        code = 0 if result['status'] == 'ok' else 1
    except Exception as exc:
        result = {'status': 'error', 'error': str(exc)}
        code = 2
    result['checked_at_beijing'] = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))).isoformat()
    payload = json.dumps(result, sort_keys=True)
    try:
        temporary = state.with_suffix('.tmp')
        temporary.write_text(payload + '\n')
        temporary.chmod(0o600)
        temporary.replace(state)
    except OSError as exc:
        print(json.dumps({'status': 'error', 'error': 'cannot save probe state: ' + str(exc)}))
        return 2
    print(payload)
    return code


if __name__ == '__main__':
    sys.exit(main())
