"""Recover an isolated copy of the real two-node run database and validate HTTP identity."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    args = parser.parse_args()
    summary = json.loads((args.run_dir / 'summary.json').read_text())
    assert summary['cleanup_ok'] and summary['all_commands_exit_ok']
    source = args.run_dir / 'controller.sqlite3'
    digest_before = hashlib.sha256(source.read_bytes()).hexdigest()
    work = Path(tempfile.mkdtemp(prefix='lastcore-recovery-'))
    snapshot, restored = work / 'snapshot.sqlite3', work / 'restored.sqlite3'
    env = {k: v for k, v in os.environ.items() if not k.startswith('CONTROLLER_')}
    env['TZ'] = 'Asia/Shanghai'
    cmd = [sys.executable, '-m', 'app.backup', '--database', str(source), '--output', str(snapshot)]
    backup = subprocess.run(cmd, cwd=ROOT / 'controller-server', env=env, capture_output=True, text=True, timeout=15)
    assert backup.returncode == 0, backup.stderr
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest_before
    # Preserve snapshot and recover only to an exclusive new path.
    with restored.open('xb') as output, snapshot.open('rb') as input_file:
        shutil.copyfileobj(input_file, output)
    def counts(path):
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
            return {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                    for table in ('nodes', 'tasks', 'assignments')}
    assert counts(source) == counts(restored)
    diagnostics = subprocess.run([sys.executable, '-m', 'app.diagnostics', '--database', str(restored)],
                                 cwd=ROOT / 'controller-server', env=env, capture_output=True, text=True, timeout=15)
    assert diagnostics.returncode == 0 and json.loads(diagnostics.stdout)['status'] == 'ok'
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    admin, registration = secrets.token_hex(32), secrets.token_hex(32)
    runtime_env = dict(env, CONTROLLER_ADMIN_TOKEN=admin, CONTROLLER_REGISTRATION_TOKEN=registration,
                       CONTROLLER_DATABASE_PATH=str(restored))
    with (work / 'controller.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:create_app', '--factory',
                                    '--host', '127.0.0.1', '--port', str(port)],
                                   cwd=ROOT / 'controller-server', env=runtime_env, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=2) as client:
                for _ in range(100):
                    assert process.poll() is None
                    try:
                        if client.get('/healthz').status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.05)
                else:
                    raise TimeoutError('recovered Controller health')
                rows = client.get('/v1/nodes', headers={'Authorization': f'Bearer {admin}'}).json()['nodes']
                identities = []
                for name in ('state-a', 'state-b'):
                    credential = json.loads((args.run_dir / name / 'node-credentials.json').read_text())
                    assert credential['node_id'] in {node['id'] for node in rows}
                    heartbeat = client.post(f'/v1/nodes/{credential["node_id"]}/heartbeat',
                                            headers={'Authorization': f'Bearer {credential["node_token"]}'},
                                            json={'available_memory_mb': 4096, 'accepting_tasks': True})
                    assert heartbeat.status_code == 200
                    identities.append(credential['node_id'])
                assert len(rows) == 2
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
    captured = (work / 'controller.log').read_text()
    assert admin not in captured and registration not in captured
    for name in ('state-a', 'state-b'):
        assert json.loads((args.run_dir / name / 'node-credentials.json').read_text())['node_token'] not in captured
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))
    result = {'scope': 'local recovery of integrated tiny run, no cloud', 'workdir': str(work),
              'counts_preserved': counts(source), 'snapshot_sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest(),
              'diagnostics': json.loads(diagnostics.stdout), 'source_bytes_unchanged_by_backup': True,
              'two_persisted_node_credentials_reaccepted': True, 'controller_reaped': process.poll() is not None,
              'listener_rebindable': True, 'secrets_absent': True, 'passed': True}
    (work / 'evidence.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
