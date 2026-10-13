#!/usr/bin/env python3
"""Bounded local E2E: real Controller and two macOS bundle processes, tiny CPU only.

Run from a verification Python with controller-server requirements + httpx + numpy.
No cloud resources/accounts are touched. Temporary files and JSON evidence remain.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / 'inference-engine'), str(REPO / 'communication')]
from inference_engine import PipelineClient, forward_full
from inference_engine.config import tiny_config


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(predicate, processes, what, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(f'process {process.pid} exited {process.returncode} waiting for {what}')
        try:
            if predicate():
                return
        except (httpx.ConnectError, httpx.ReadError):
            pass
        time.sleep(.15)
    raise TimeoutError(what)


def lock_available(path):
    with (path / 'agent.lock').open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return process.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle-dir', default=str(REPO / 'node-agent/dist/lastcore-node-agent'))
    parser.add_argument('--controller-python', default=sys.executable,
                        help='Python with controller-server dependencies; no installation is performed')
    args = parser.parse_args()
    bundle = Path(args.bundle_dir).resolve()
    work = Path(tempfile.mkdtemp(prefix='lastcore-bundle-stage '))
    admin, registration = secrets.token_hex(24), secrets.token_hex(24)
    ports = [port() for _ in range(3)]
    url = f'http://127.0.0.1:{ports[0]}'
    processes, logs, agents = [], [], []
    result = {'workdir': str(work), 'bundle': str(bundle), 'controller': 'real local controller-server; no cloud',
              'model': 'lastcore/tiny-cpu; generated weights, no pretrained LLM', 'passed': False}
    base_env = {k: v for k, v in os.environ.items() if not k.startswith('NODE_AGENT_') and k != 'PYTHONPATH'}
    base_env['TZ'] = 'Asia/Shanghai'
    try:
        env = dict(base_env, CONTROLLER_ADMIN_TOKEN=admin, CONTROLLER_REGISTRATION_TOKEN=registration,
                   CONTROLLER_DATABASE_PATH=str(work / 'controller.sqlite3'))
        ctrl_log = (work / 'controller.log').open('w')
        logs.append(ctrl_log)
        controller = subprocess.Popen([args.controller_python, '-m', 'uvicorn', 'app.main:create_app', '--factory',
                                       '--host', '127.0.0.1', '--port', str(ports[0])],
                                      cwd=REPO / 'controller-server', env=env, stdout=ctrl_log, stderr=ctrl_log)
        processes.append(controller)
        with httpx.Client(base_url=url, headers={'Authorization': f'Bearer {admin}'},
                          timeout=3, trust_env=False) as client:
            def get(path):
                response = client.get(path); response.raise_for_status(); return response.json()
            def post(path, body=None):
                response = client.post(path, json=body); response.raise_for_status(); return response.json()
            wait_for(lambda: get('/healthz')['status'] == 'ok', processes, 'controller healthy')
            config_paths = []
            for index in range(2):
                state = work / f'node {index}'
                config = work / f'node {index}.env'
                values = {'EXECUTOR': 'stage', 'CONTROLLER_URL': url,
                          'ENDPOINT': f'http://127.0.0.1:{ports[index+1]}',
                          'STATE_DIR': str(state), 'REGISTRATION_TOKEN': registration,
                          'NAME': f'bundle-{index}', 'AVAILABLE_MEMORY_MB': 4096 if index == 0 else 2048,
                          'COMPUTE_WEIGHT': 2 if index == 0 else 1,
                          'HEARTBEAT_INTERVAL': .5, 'POLL_INTERVAL': .2}
                config.write_text(''.join(f'NODE_AGENT_{k}={v}\n' for k, v in values.items()))
                config.chmod(0o600)
                config_paths.append(config)
                preflight = subprocess.run([str(bundle / 'lastcore-preflight'), '--env-file', str(config), '--json'],
                                           env=base_env, capture_output=True, text=True, timeout=15)
                assert preflight.returncode == 0 and json.loads(preflight.stdout)['ok'], preflight.stderr
            def launch(suffix):
                for index, config in enumerate(config_paths):
                    log = (work / f'node-{index}-{suffix}.log').open('w'); logs.append(log)
                    proc = subprocess.Popen([str(bundle / 'lastcore-node-agent'), '--env-file', str(config)],
                                            cwd=work, env=base_env, stdout=log, stderr=log)
                    agents.append(proc); processes.append(proc)
                if suffix == 'restart':
                    wait_for(lambda: all('reusing persisted' in (work / f'node-{i}-{suffix}.log').read_text()
                                         for i in range(2)), [controller] + agents, 'both restarted processes loaded credentials')
                wait_for(lambda: len([n for n in get('/v1/nodes')['nodes'] if n['status'] == 'available']) == 2,
                         [controller] + agents, 'two available bundle nodes')
            launch('initial')
            node_ids = sorted(n['id'] for n in get('/v1/nodes')['nodes'])
            task = post('/v1/tasks', {'model_id': 'lastcore/tiny-cpu', 'num_layers': 4,
                                     'layer_memory_mb': 512, 'timeout_seconds': 90})
            task_id = task['id']
            wait_for(lambda: all('serving on' in (work / f'node-{i}-initial.log').read_text() for i in range(2)),
                     [controller] + agents, 'both tiny stage listeners')
            plan = get(f'/v1/tasks/{task_id}')['assignments']
            assert len(plan) == 2
            first = min(plan, key=lambda a: a['layer_start'])
            first_node = next(n for n in get('/v1/nodes')['nodes'] if n['id'] == first['node_id'])
            from urllib.parse import urlparse
            endpoint = urlparse(first_node['endpoint'])
            tokens = np.asarray([[1, 2, 3, 4]], dtype=np.int64)
            logits = PipelineClient((endpoint.hostname, endpoint.port), model_id='lastcore/tiny-cpu',
                                    task_id=task_id).run(tokens, task_id=task_id)
            reference = forward_full(tokens, dict(np.load(work / 'node 0/tiny-model.npz')), tiny_config())
            assert np.array_equal(logits, reference), 'distributed logits differ'
            wait_for(lambda: get(f'/v1/tasks/{task_id}')['status'] == 'completed', [controller] + agents, 'completed')
            assert all(a['status'] == 'completed' for a in get(f'/v1/tasks/{task_id}')['assignments'])
            result['inference'] = {'task_id': task_id, 'shape': list(logits.shape), 'byte_identical': True,
                                   'sha256': hashlib.sha256(logits.tobytes()).hexdigest(),
                                   'layer_ranges': [[a['layer_start'], a['layer_end']] for a in plan]}
            before = [(work / f'node-{i}-initial.log').read_text().count('serving on') for i in range(2)]
            cancel_task = post('/v1/tasks', {'model_id': 'lastcore/tiny-cpu', 'num_layers': 4,
                                            'layer_memory_mb': 512, 'timeout_seconds': 90})['id']
            wait_for(lambda: all((work / f'node-{i}-initial.log').read_text().count('serving on') > before[i]
                                 for i in range(2)), [controller] + agents, 'fresh cancellation listeners')
            assert post(f'/v1/tasks/{cancel_task}/cancel')['status'] == 'cancelled'
            wait_for(lambda: all('stopped locally (task-terminal)' in (work / f'node-{i}-initial.log').read_text()
                                 for i in range(2)), [controller] + agents, 'both cancellation observers')
            for number in ports[1:]:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', number))
            result['cancel'] = {'task_id': cancel_task, 'status': 'cancelled', 'listeners_rebindable': True}
            assert all(not lock_available(work / f'node {i}') for i in range(2))
            assert [stop(p) for p in agents] == [0, 0]
            agents.clear()
            launch('restart')
            assert sorted(n['id'] for n in get('/v1/nodes')['nodes']) == node_ids
            result['restart'] = {'credential_reused': True, 'node_count': 2}
            assert [stop(p) for p in agents] == [0, 0]
            agents.clear()
            assert all(lock_available(work / f'node {i}') for i in range(2))
            captured = ''.join(p.read_text() for p in work.glob('*.log'))
            assert registration not in captured and admin not in captured
            for i in range(2):
                credential = json.loads((work / f'node {i}/node-credentials.json').read_text())
                assert credential['node_token'] not in captured
            assert 'Traceback' not in captured
            result['cleanup'] = {'agent_exit_codes': [0, 0], 'locks_reacquirable': True, 'tokens_absent': True}
            result['passed'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()
        result['all_started_processes_exited'] = all(p.poll() is not None for p in processes)
        (work / 'evidence.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
