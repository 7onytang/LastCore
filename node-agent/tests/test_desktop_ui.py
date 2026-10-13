"""Exercise real loopback HTTP security without starting a configured worker."""
import importlib.util
from pathlib import Path
import sys
import threading
import urllib.request
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'desktop'))
import ui

class IdleNode:
    def state(self): return {'running':False,'exit_code':None,'mode':'stage'}
    def start(self): raise AssertionError('Unauthorized start must not execute')
    def stop(self): raise AssertionError('Unauthorized stop must not execute')

def test_local_controls_require_session_and_reject_foreign_host_origin():
    server,key = ui.make_server(IdleNode())
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    def request(path,headers=None,method='GET'):
        try:
            with urllib.request.urlopen(urllib.request.Request(origin+path,headers=headers or {},method=method)) as r: return r.status,r.read()
        except urllib.error.HTTPError as e: return e.code,e.read()
    try:
        status,body = request('/')
        assert status == 200
        assert b'data-surface="desktop"' in body
        assert b'admin-token' not in body and b'node-list' not in body and b'task-list' not in body
        assert request('/desktop/state')[0] == 403
        assert request('/desktop/start',method='POST')[0] == 403
        assert request('/desktop/state',{'X-LastCore-Session':key})[0] == 200
        assert request('/desktop/start',{'X-LastCore-Session':key,'Origin':'https://attacker.invalid'},'POST')[0] == 403
        assert request('/desktop/start',{'X-LastCore-Session':key,'Host':'attacker.invalid'},'POST')[0] == 403
        assert request('/ui/../../env_file.py')[0] == 404
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_stopping_is_not_reported_as_stopped_or_restarted(tmp_path, monkeypatch):
    """A delayed child must keep its recovery barrier and prevent duplicate launch."""
    import subprocess
    env = tmp_path/'node.env'; env.write_text('NODE_AGENT_EXECUTOR=demo\n'); env.chmod(0o600)
    node = ui.NodeProcess(env)
    class DelayedProcess:
        exit_code = None
        def poll(self): return self.exit_code
        def send_signal(self, value): pass
        def wait(self, timeout): raise subprocess.TimeoutExpired('node',timeout)
    process = DelayedProcess(); node.process = process
    state = node.stop()
    assert state['running'] and state['stopping']
    monkeypatch.setattr(ui.subprocess,'Popen',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('must not restart a live process')))
    assert node.start()['stopping']
    process.exit_code = 0
    state = node.state()
    assert not state['running'] and not state['stopping'] and state['exit_code'] == 0
    assert state['observation']['connection'] == 'stopped'
