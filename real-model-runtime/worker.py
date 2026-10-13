"""Minimal HTTPS polling worker for one fixed whole-model job at a time."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import httpx
import runtime

ROOT = Path(__file__).resolve().parent
BACKEND_COMMIT = '8345f333951c661d166b00e6f9362e553768f292'
SOURCE_SHA256 = '84050e783dbd0c4a4678f90f253b0881df04e90a98c20f73ae3709751c4a4850'
MODEL_ID = 'SmolLM2-135M-Instruct-Q4_K_M'


class WorkerFailure(RuntimeError):
    pass


def validate_url(value, allow_loopback_http=False):
    try:
        p = urlsplit(value)
        allowed = p.scheme == 'https' or (allow_loopback_http and p.scheme == 'http' and p.hostname in ('127.0.0.1','localhost','::1'))
        if not allowed or not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment or '?' in value or '#' in value or any(c.isspace() or ord(c)<32 for c in value):
            raise ValueError()
        p.port
    except (ValueError, TypeError):
        raise WorkerFailure('Controller requires an absolute HTTPS URL without userinfo/query/fragment') from None
    return value.rstrip('/')


def private_json(path):
    metadata=path.stat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 16384:
        raise WorkerFailure('configuration/state must be a private regular JSON file')
    return json.loads(path.read_text())


def save(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.worker-', dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(value,f); f.flush(); os.fsync(f.fileno())
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Worker:
    def __init__(self, config, stop=None, allow_loopback_http=False):
        self.url = validate_url(config['controller_url'], allow_loopback_http)
        self.stop = stop or threading.Event()
        self.root = Path(config['runtime_root']).resolve()
        self.state = Path(config['state_dir']).expanduser()
        self.state.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.state.is_symlink() or self.state.stat().st_mode & 0o077:
            raise WorkerFailure('worker state directory must be private')
        self.lock = (self.state/'worker.lock').open('a+')
        try: fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise WorkerFailure('this worker state is already running') from None
        self.http = httpx.Client(base_url=self.url, timeout=3, trust_env=False, follow_redirects=False)
        installed=json.loads((self.root/'.cache/installed.json').read_text())
        if installed['backend_commit'] != BACKEND_COMMIT or installed['source_sha256'] != SOURCE_SHA256 or installed['model_sha256'] != runtime.MODEL_SHA256 or runtime.sha256(self.root/'.cache/backend.tar.gz') != SOURCE_SHA256:
            raise WorkerFailure('installed model/backend provenance mismatch')
        runtime.LocalRuntime(self.root/'.cache/backend/build/bin/llama-completion',self.root/'.cache/model.gguf',installed['backend_sha256'])
        self.credential_path=self.state/'node-credentials.json'
        if self.credential_path.exists(): self.node=private_json(self.credential_path)
        else:
            marker=self.state/'registration.pending'
            if marker.exists(): raise WorkerFailure('registration outcome is unknown; do not register again blindly')
            marker.touch(mode=0o600,exist_ok=False)
            self.node=self.call('/v1/nodes/register',config['registration_token'],{'name':config.get('name','macos-real-model-worker'),'platform':'macOS-whole-model-worker','endpoint':'http://127.0.0.1:19199','available_memory_mb':int(config.get('available_memory_mb',512)),'compute_weight':1,'accepting_tasks':False}, expected=201)
            save(self.credential_path,self.node);marker.unlink()
        self.token=self.node['node_token'];self.node_id=self.node['node_id']
        self.capability={'model_id':MODEL_ID,'model_sha256':runtime.MODEL_SHA256,'backend_commit':BACKEND_COMMIT,'backend_sha256':installed['backend_sha256']}
        self.call('/v1/inference/workers/'+self.node_id+'/capability',self.token,self.capability)
        self.ledger=self.state/'current-job.json'

    def call(self,path,token,body=None,expected=200):
        try:
            response=self.http.post(path,headers={'Authorization':'Bearer '+token},json=body or {})
            if response.status_code != expected:
                raise WorkerFailure('Controller returned HTTP '+str(response.status_code))
            return response.json()
        except httpx.HTTPError:
            raise WorkerFailure('Controller connection failed') from None

    def lease_body(self,job):return {'node_id':self.node_id,'lease_epoch':job['lease_epoch'],'execution_key':job.get('_execution_key')}

    def ack(self,job):
        return self.call('/v1/inference/jobs/'+job['id']+'/stop-ack',self.token,self.lease_body(job))

    def deliver(self,job,outcome):
        try:
            self.call('/v1/inference/jobs/'+job['id']+'/report',self.token,{**self.lease_body(job),**outcome})
        except WorkerFailure:
            # A terminal lease or a racing cancellation must never accept a late result.
            self.ack(job)

    def execute(self,job):
        if job['status'] != 'leased':
            raise WorkerFailure('existing execution has no local ledger; refusing duplicate launch')
        job['_execution_key']=secrets.token_hex(32)
        save(self.ledger,{'job':job,'phase':'claimed'})
        self.call('/v1/inference/jobs/'+job['id']+'/report',self.token,{**self.lease_body(job),'status':'running'})
        sent=time.monotonic()
        renewed=self.call('/v1/inference/jobs/'+job['id']+'/renew',self.token,self.lease_body(job))
        if renewed['cancel_requested']:
            self.ack(job);self.ledger.unlink();return
        lease_deadline=sent+renewed['lease_expires']-renewed['server_time']-.5
        remaining=max(.01,sent+renewed['deadline']-renewed['server_time']-.5-time.monotonic())
        completion=self.state/('guardian-'+job['id']+'-'+str(job['lease_epoch'])+'.json')
        initial={**job['request'],'runtime_root':str(self.root),'completion_path':str(completion),'execution_timeout_seconds':min(remaining,job['request']['execution_timeout_seconds']),'lease_deadline_monotonic':lease_deadline}
        with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
            process=subprocess.Popen([sys.executable,str(ROOT/'job_guardian.py')],stdin=subprocess.PIPE,stdout=out,stderr=err,start_new_session=True)
            save(self.ledger,{'job':job,'phase':'launched','guardian_pid':process.pid,'completion_path':str(completion)})
            process.stdin.write((json.dumps(initial)+'\n').encode());process.stdin.flush()
            cancelled=False;next_renew=time.monotonic()
            def send(message):
                if process.poll() is not None:return
                try:
                    process.stdin.write((json.dumps(message)+'\n').encode());process.stdin.flush()
                except BrokenPipeError:
                    pass
            try:
                while process.poll() is None:
                    if self.stop.is_set() or time.monotonic()>=lease_deadline:
                        send({'cancel':True});process.stdin.close()
                        process.wait(timeout=5);break
                    if time.monotonic()>=next_renew:
                        sent=time.monotonic()
                        try:
                            renewed=self.call('/v1/inference/jobs/'+job['id']+'/renew',self.token,self.lease_body(job))
                            if renewed['cancel_requested']:
                                cancelled=True;send({'cancel':True})
                            else:
                                lease_deadline=sent+renewed['lease_expires']-renewed['server_time']-.5
                                send({'lease_deadline_monotonic':lease_deadline})
                        except WorkerFailure:
                            # Do not extend the local deadline from an unconfirmed renewal.
                            pass
                        next_renew=time.monotonic()+.25
                    if out.tell()>131072 or err.tell()>131072:
                        raise WorkerFailure('guardian output budget exceeded')
                    time.sleep(.02)
                process.wait(timeout=5)
                out.seek(0);raw=out.read(131073)
                if len(raw)>131072 or process.returncode!=0:
                    raise WorkerFailure('guardian failed without verified cleanup')
                full=json.loads(raw)
                if not full.get('backend_reaped'):raise WorkerFailure('native cleanup was not confirmed')
                outcome={k:v for k,v in full.items() if k in ('status','result','error_code')}
                if cancelled: outcome={'status':'cancelled'}
                elif outcome['status']=='cancelled': outcome={'status':'failed','error_code':'lease_lost'}
                save(self.ledger,{'job':job,'phase':'stopped','outcome':outcome})
                self.deliver(job,outcome)
                self.ledger.unlink()
                completion.unlink(missing_ok=True)
                print(json.dumps({'job_id':job['id'],'event':'backend_reaped','status':outcome['status']}),flush=True)
            finally:
                if process.stdin and not process.stdin.closed:process.stdin.close()
                # EOF is independently enforced by the guardian, even when this parent is killed.
                process.wait(timeout=5)

    def recover(self):
        if not self.ledger.exists():return
        saved=private_json(self.ledger);job=saved['job']
        if saved.get('completion_path') and Path(saved['completion_path']).exists():
            proof=private_json(Path(saved['completion_path']))
            if not proof.get('backend_reaped'):raise WorkerFailure('previous native cleanup is unknown')
            saved['outcome']={k:v for k,v in proof.items() if k in ('status','result','error_code')}
            if saved['outcome']['status']=='cancelled':saved['outcome']={'status':'failed','error_code':'worker_restart'}
        if saved.get('outcome'):
            self.deliver(job,saved['outcome']);self.ledger.unlink();
            if saved.get('completion_path'):Path(saved['completion_path']).unlink(missing_ok=True)
            return
        if saved['phase']=='claimed':
            # Initial native parameters are sent only after durable "launched" state.
            self.deliver(job,{'status':'failed','error_code':'worker_restart'});self.ledger.unlink();return
        raise WorkerFailure('previous native stop is unconfirmed; no new job will launch')

    def run(self,max_jobs=0):
        self.recover();count=0
        print(json.dumps({'event':'ready','node_id':self.node_id,'scope':'one whole fixed GGUF model; no distributed layers'}),flush=True)
        try:
            while not self.stop.is_set():
                response=self.call('/v1/inference/workers/'+self.node_id+'/claim',self.token)
                if response.get('cleanup_required'):raise WorkerFailure('previous execution requires confirmed local cleanup')
                job=response['job']
                if job:
                    self.execute(job);count+=1
                    if max_jobs and count>=max_jobs:return
                else:self.stop.wait(.5)
        finally:
            self.http.close();self.lock.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--max-jobs',type=int,default=0)
    parser.add_argument('--allow-loopback-http',action='store_true',help='Only for bounded local verification; public HTTP is always rejected')
    args=parser.parse_args();stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    try:
        Worker(private_json(args.config),stop,args.allow_loopback_http).run(args.max_jobs)
    except Exception as error:
        print(json.dumps({'event':'worker_stopped','reason':str(error) if isinstance(error,WorkerFailure) else type(error).__name__}),flush=True)
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
