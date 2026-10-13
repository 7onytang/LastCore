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
        assert request('/')[0] == 200
        assert request('/desktop/state')[0] == 403
        assert request('/desktop/start',method='POST')[0] == 403
        assert request('/desktop/state',{'X-LastCore-Session':key})[0] == 200
        assert request('/desktop/start',{'X-LastCore-Session':key,'Origin':'https://attacker.invalid'},'POST')[0] == 403
        assert request('/desktop/start',{'X-LastCore-Session':key,'Host':'attacker.invalid'},'POST')[0] == 403
        assert request('/ui/../../env_file.py')[0] == 404
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
