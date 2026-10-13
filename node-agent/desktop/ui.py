#!/usr/bin/env python3
"""Loopback-only, browser-based node controls; credentials stay in the env file."""
from __future__ import annotations
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
from urllib.parse import urlsplit
import webbrowser
from env_file import read_env_file

UI = Path(__file__).resolve().parents[2] / 'controller-server' / 'app' / 'ui'

class NodeProcess:
    def __init__(self, env_file: Path):
        values = read_env_file(env_file)
        self.mode = values.get('NODE_AGENT_EXECUTOR')
        if self.mode not in {'stage', 'demo'}:
            raise ValueError('Select stage or demo in the private configuration')
        self.env_file = env_file
        self.process = None
        self.lock = threading.Lock()

    def state(self):
        code = None if self.process is None else self.process.poll()
        return {'running': self.process is not None and code is None, 'exit_code': code, 'mode': self.mode}

    def start(self):
        with self.lock:
            if not self.state()['running']:
                # Never send child logs (which may contain private data) to the browser.
                options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == 'win32' else {'start_new_session': True}
                self.process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('run_agent.py')), '--env-file', str(self.env_file)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
            return self.state()

    def stop(self):
        with self.lock:
            if self.state()['running']:
                self.process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == 'win32' else signal.SIGINT)
                try:
                    self.process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    # Preserve uncertain recovery state rather than claiming a stop.
                    pass
            return self.state()


def make_server(node: NodeProcess, port: int = 0):
    session = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, data, mime='application/json'):
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def allowed(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            return self.headers.get('Host') == origin.removeprefix('http://') and self.headers.get('Origin', origin) == origin

        def dispatch(self):
            if not self.allowed():
                self.send(403,b'{}'); return
            path = urlsplit(self.path).path
            if path.startswith('/desktop/'):
                if not secrets.compare_digest(self.headers.get('X-LastCore-Session', ''),session):
                    self.send(403,b'{}'); return
                action = {('GET','/desktop/state'):node.state, ('POST','/desktop/start'):node.start, ('POST','/desktop/stop'):node.stop}.get((self.command,path))
                if action is None:
                    self.send(404,b'{}'); return
                try:
                    self.send(200,json.dumps(action()).encode())
                except (OSError, ValueError):
                    self.send(500,b'{}')
                return
            files = {'/':'index.html','/ui/styles.css':'styles.css','/ui/console.js':'console.js'}
            if self.command != 'GET' or path not in files:
                self.send(404,b'{}'); return
            name = files[path]
            mime = {'index.html':'text/html; charset=utf-8','styles.css':'text/css; charset=utf-8','console.js':'application/javascript; charset=utf-8'}[name]
            self.send(200,(UI/name).read_bytes(),mime)

        do_GET = dispatch
        do_POST = dispatch
    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads = True
    return server, session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',required=True)
    parser.add_argument('--no-browser',action='store_true')
    args = parser.parse_args()
    try:
        node = NodeProcess(Path(args.env_file).expanduser().resolve())
    except ValueError:
        parser.error('Invalid private configuration; check file permissions and stage/demo mode')
    server,session = make_server(node)
    url = f'http://127.0.0.1:{server.server_port}/?desktop=1#{session}'
    print(f'LastCore local panel: http://127.0.0.1:{server.server_port}/ (session URL is private)')
    if args.no_browser:
        print(url)
    else:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state = node.stop()
        if state['running']:
            print('Node stop is pending; recovery state remains intact. Check the process before restarting.',file=sys.stderr)

if __name__ == '__main__':
    main()
