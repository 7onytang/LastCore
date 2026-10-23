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
import tempfile
import math
import time
from urllib.parse import urlsplit
import webbrowser
import uuid
from env_file import read_env_file
from resource_snapshot import ResourceSampler
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from lastcore_node_agent.local_status import safe_task, safe_admission, ERROR_CODES

UI = Path(__file__).resolve().parents[2] / 'controller-server' / 'app' / 'ui'

class NodeProcess:
    def __init__(self, env_file: Path):
        values = read_env_file(env_file)
        self.mode = values.get('NODE_AGENT_EXECUTOR')
        if self.mode not in {'stage', 'demo'}:
            raise ValueError('Select stage or demo in the private configuration')
        parsed = urlsplit(values.get('NODE_AGENT_CONTROLLER_URL', ''))
        self.controller_origin = f'{parsed.scheme}://{parsed.netloc}' if parsed.hostname and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment else None
        self.configured_memory_mb = self._positive_number(values.get('NODE_AGENT_AVAILABLE_MEMORY_MB'))
        self.configured_weight = self._positive_number(values.get('NODE_AGENT_COMPUTE_WEIGHT', '1'))
        self.initial_accepting = not bool(values.get('NODE_AGENT_NO_ACCEPT'))
        self.pending_admission = None
        self.recent_errors = []
        self.previous_errors = set()
        self.env_file = env_file
        self.process = None
        self.stop_requested = False
        self.lock = threading.Lock()
        self.private_status = tempfile.TemporaryDirectory(prefix="lastcore-node-ui-")
        self.status_path = Path(self.private_status.name) / "status.json"
        self.control_path = Path(self.private_status.name) / 'control.json'
        self.sampler = ResourceSampler()

    @staticmethod
    def _positive_number(value):
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (TypeError, ValueError):
            return None

    def state(self):
        code = None if self.process is None else self.process.poll()
        running = self.process is not None and code is None
        observation={'connection':'unavailable','last_heartbeat':None,'task':None}
        try:
            if self.status_path.stat().st_size <= 32768:
                raw=json.loads(self.status_path.read_text())
                if isinstance(raw,dict):
                    for key in ['node_id','connection','poll_status','last_heartbeat','heartbeat_ttl','observed_at']:
                        if key in raw: observation[key]=raw[key]
                    observation['task']=safe_task(raw.get('task'))
                    observation['admission']=safe_admission(raw.get('admission'))
                    observation['control_error']='invalid-command' if raw.get('control_error')=='invalid-command' else None
                    history = raw.get('recent_errors')
                    if isinstance(history,list):
                        for error_code in history[-8:]:
                            if isinstance(error_code,str) and error_code in ERROR_CODES and error_code not in self.recent_errors:
                                self.recent_errors = (self.recent_errors + [error_code])[-8:]
        except (OSError,ValueError): pass
        if not running: observation['connection']='stopped'
        elif isinstance(observation.get('last_heartbeat'),(int,float)) and isinstance(observation.get('heartbeat_ttl'),(int,float)) and time.time()-observation['last_heartbeat']>observation['heartbeat_ttl']:
            observation['connection']='heartbeat-stale'
        admission = observation.get('admission')
        pending = self.pending_admission
        if pending and (not admission or admission['request_id'] != pending['request_id'] or admission['desired'] is not pending['desired']):
            admission = {**pending, 'confirmed': admission.get('confirmed') if admission else None,
                         'confirmed_request_id': admission.get('confirmed_request_id') if admission else None, 'status':'pending'}
        observation['admission'] = admission or {'desired':self.initial_accepting,'confirmed':None,
            'request_id':None,'confirmed_request_id':None,'status':'pending'}
        reasons = []
        if observation.get('control_error') == 'invalid-command': reasons.append('invalid-command')
        if observation.get('connection') in {'unreachable','heartbeat-failed','heartbeat-stale','rejected','recovery-required'}:
            reasons.append(observation['connection'])
        if observation.get('poll_status') in {'unavailable','rejected'}:
            reasons.append('poll-' + observation['poll_status'])
        task_error = (observation.get('task') or {}).get('error_code')
        if task_error in {'deadline','heartbeat-stall','execution_failed','executor_unavailable','cleanup_timeout'}:
            reasons.append(task_error)
        for reason in reasons:
            if reason not in self.previous_errors:
                self.recent_errors = (self.recent_errors + [reason])[-8:]
        self.previous_errors = set(reasons)
        return {'controller_origin':self.controller_origin,
                'configured_resources':{'available_memory_mb':self.configured_memory_mb,'compute_weight':self.configured_weight},
                'recent_errors':list(self.recent_errors), 'running':running,'stopping':running and self.stop_requested,'exit_code':code,
                'mode':self.mode,'observation':observation,'device':self.sampler.sample()}

    def start(self):
        with self.lock:
            if not self.state()['running']:
                self.stop_requested = False
                self.status_path.unlink(missing_ok=True)
                self.control_path.unlink(missing_ok=True)
                self.pending_admission = None
                # Never send child logs (which may contain private data) to the browser.
                options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == 'win32' else {'start_new_session': True}
                self.process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('run_agent.py')), '--env-file', str(self.env_file), '--ui-status-file', str(self.status_path), '--ui-control-file', str(self.control_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
            return self.state()

    def set_accepting(self, value):
        if type(value) is not bool:
            raise ValueError('accepting must be a boolean')
        with self.lock:
            state = self.state()
            if not state['running'] or state['stopping']:
                raise ValueError('node is not accepting controls while stopped or stopping')
            from lastcore_node_agent.local_control import write_control_command
            request_id = uuid.uuid4().hex
            write_control_command(self.control_path, request_id, value)
            self.pending_admission = {'desired':value, 'request_id':request_id}
            return self.state()

    def stop(self):
        with self.lock:
            if self.state()['running']:
                self.stop_requested = True
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
                action = {('GET','/desktop/state'):node.state, ('POST','/desktop/start'):node.start, ('POST','/desktop/stop'):node.stop, ('POST','/desktop/pause'):lambda:node.set_accepting(False), ('POST','/desktop/resume'):lambda:node.set_accepting(True)}.get((self.command,path))
                if action is None:
                    self.send(404,b'{}'); return
                try:
                    self.send(200,json.dumps(action()).encode())
                except (OSError, ValueError):
                    self.send(500,b'{}')
                return
            files = {'/':'desktop.html','/ui/styles.css':'styles.css','/ui/console.js':'console.js'}
            if self.command != 'GET' or path not in files:
                self.send(404,b'{}'); return
            name = files[path]
            mime = {'desktop.html':'text/html; charset=utf-8','styles.css':'text/css; charset=utf-8','console.js':'application/javascript; charset=utf-8'}[name]
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
