"""Actual local Controller + macOS whole-model worker + pinned real GGUF, bounded acceptance."""
import argparse
from datetime import datetime
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
from zoneinfo import ZoneInfo

import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root',type=Path,required=True)
    parser.add_argument('--controller-python',default=sys.executable)
    parser.add_argument('--worker-python',default=sys.executable)
    args=parser.parse_args();work=Path(tempfile.mkdtemp(prefix='lastcore-real-jobs-'));work.chmod(0o700)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    url='http://127.0.0.1:'+str(port)
    admin,registration=secrets.token_hex(32),secrets.token_hex(32)
    env=dict(os.environ,TZ='Asia/Shanghai',CONTROLLER_ADMIN_TOKEN=admin,CONTROLLER_REGISTRATION_TOKEN=registration,CONTROLLER_DATABASE_PATH=str(work/'controller.sqlite3'),CONTROLLER_REAL_JOBS_ENABLED='true')
    logs=[];processes=[];result={'scope':'actual local HTTP Controller and real pinned whole-model macOS worker; not cloud/device/layer-distributed evidence','workdir':str(work),'passed':False}
    try:
        log=(work/'controller.log').open('w');logs.append(log)
        controller=subprocess.Popen([args.controller_python,'-m','uvicorn','app.main:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT/'controller-server',env=env,stdout=log,stderr=log);processes.append(controller)
        with httpx.Client(base_url=url,trust_env=False,timeout=3) as c:
            adminh={'Authorization':'Bearer '+admin}
            def wait(predicate,budget=20):
                end=time.monotonic()+budget
                while time.monotonic()<end:
                    assert controller.poll() is None,'Controller exited'
                    try:
                        value=predicate()
                        if value:return value
                    except httpx.ConnectError:pass
                    time.sleep(.02)
                raise TimeoutError('bounded real model job acceptance')
            wait(lambda:c.get('/healthz').status_code==200)
            config=work/'worker-config.json';config.write_text(json.dumps({'controller_url':url,'registration_token':registration,'state_dir':str(work/'worker-state'),'runtime_root':str(args.runtime_root.resolve()),'name':'local-real-model-acceptance'}));config.chmod(0o600)
            log=(work/'worker.log').open('w');logs.append(log)
            worker=subprocess.Popen([args.worker_python,str(ROOT/'real-model-runtime/worker.py'),'--config',str(config),'--allow-loopback-http','--max-jobs','3'],cwd=ROOT,env=dict(os.environ,TZ='Asia/Shanghai'),stdout=log,stderr=log);processes.append(worker)
            def ready():
                assert worker.poll() is None,(work/'worker.log').read_text()
                r=c.get('/v1/inference/workers',headers=adminh);r.raise_for_status();workers=r.json()['workers']
                return workers[0] if workers else None
            node=wait(ready)['node_id']
            def create(key,prompt='What is the capital of France?',**kw):
                r=c.post('/v1/inference/jobs',headers=adminh,json={'worker_node_id':node,'prompt':prompt,'max_tokens':32,'idempotency_key':key,**kw});r.raise_for_status();return r.json()
            def get(job):
                r=c.get('/v1/inference/jobs/'+job['id'],headers=adminh);r.raise_for_status();return r.json()
            job=create('actual_local_paris_0001');assert create('actual_local_paris_0001')['id']==job['id']
            completed=wait(lambda:(v if (v:=get(job))['status']=='succeeded' else None))
            assert 'Paris' in completed['result']['text'] and completed['stop_acknowledged']
            timed=create('actual_local_timeout_0002',prompt='Write a long story about Paris.',max_tokens=64,execution_timeout_seconds=.02)
            failed=wait(lambda:(v if (v:=get(timed))['status']=='failed' else None))
            assert failed['error_code']=='deadline' and failed['stop_acknowledged']
            cancelled=create('actual_local_cancel_0003',prompt='Write a long story about Paris. '+('Continue the story. '*100),max_tokens=64)
            wait(lambda:get(cancelled)['status']=='running')
            r=c.post('/v1/inference/jobs/'+cancelled['id']+'/cancel',headers=adminh);r.raise_for_status();assert r.json()['status']=='cancelling'
            stopped=wait(lambda:(v if (v:=get(cancelled))['status']=='cancelled' and v['stop_acknowledged'] else None))
            assert stopped['result'] is None
            assert worker.wait(timeout=10)==0,(work/'worker.log').read_text()
            workers=c.get('/v1/inference/workers',headers=adminh).json()['workers'];assert workers[0]['ready'] and workers[0]['current_job_id'] is None
            result.update(passed=True,source_hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'controller-server/app/inference_jobs.py',ROOT/'real-model-runtime/worker.py',ROOT/'real-model-runtime/job_guardian.py']},paris_result=completed['result'],job_idempotency=True,real_backend_timeout={'status':failed['status'],'error_code':failed['error_code'],'stop_acknowledged':True},cancel={'status':'cancelled','stop_acknowledged':True,'result_discarded':True},worker_exit=0,worker_ready_after_cleanup=True)
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:p.wait(timeout=10)
                except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)
        for log in logs:log.close()
        captured=''.join(p.read_text() for p in work.glob('*.log'))
        assert admin not in captured and registration not in captured
        credentials=work/'worker-state/node-credentials.json'
        if credentials.exists():assert json.loads(credentials.read_text())['node_token'] not in captured
        with socket.socket() as s:s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',port))
        result.update(all_started_processes_exited=all(p.poll() is not None for p in processes),tokens_absent=True,checked_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
        (work/'evidence.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))

if __name__=='__main__':main()
