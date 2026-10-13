import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import runtime
import job_guardian
from worker import validate_url,WorkerFailure

class GuardianTests(unittest.TestCase):
    def exercise(self,mode):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'.cache').mkdir();(root/'.cache/installed.json').write_text('{"backend_sha256":"unused"}')
            pidfile=root/'native.pid';proof=root/'proof.json'
            command=[sys.executable,'-c','import os,time,sys;open(sys.argv[1],"w").write(str(os.getpid()));time.sleep(90)',str(pidfile)]
            class Engine:
                def generate(self,prompt,tokens,timeout,cancel):
                    runtime.run_process(command,timeout,cancel)
                    return {'text':'synthetic','elapsed_seconds':.1,'model_sha256':runtime.MODEL_SHA256}
            read,write=os.pipe();result=[]
            thread=threading.Thread(target=job_guardian.supervise,args=(read,result.append,lambda _:Engine()))
            thread.start()
            request={'runtime_root':str(root),'completion_path':str(proof),'prompt':'bounded synthetic guardian test','max_tokens':1,'execution_timeout_seconds':90,'lease_deadline_monotonic':time.monotonic()+(.3 if mode=='lease' else 10)}
            os.write(write,(json.dumps(request)+'\n').encode())
            end=time.monotonic()+3
            while not pidfile.exists():
                self.assertLess(time.monotonic(),end);time.sleep(.01)
            pid=int(pidfile.read_text())
            if mode=='eof':os.close(write)
            elif mode=='cancel':os.write(write,b'{"cancel":true}\n')
            thread.join(timeout=3);self.assertFalse(thread.is_alive())
            if mode!='eof':os.close(write)
            os.close(read)
            self.assertEqual(result[0]['status'],'cancelled')
            self.assertTrue(json.loads(proof.read_text())['backend_reaped'])
            self.assertEqual(proof.stat().st_mode & 0o777,0o600)
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
    def test_parent_pipe_eof_reaps_native(self):self.exercise('eof')
    def test_lease_expiry_reaps_native(self):self.exercise('lease')
    def test_explicit_cancel_reaps_native(self):self.exercise('cancel')
    def test_public_url_never_accepts_plaintext_or_secret_fields(self):
        for value in ['http://8.134.161.123','https://user:secret@example.com','https://example.com?token=secret','https://example.com#secret','https://example.com?','https://example.com\n']:
            with self.assertRaises(WorkerFailure) as error:validate_url(value,True)
            self.assertNotIn('secret',str(error.exception))
        self.assertEqual(validate_url('https://8.134.161.123'),'https://8.134.161.123')
        self.assertEqual(validate_url('http://127.0.0.1:8000',True),'http://127.0.0.1:8000')
        with self.assertRaises(WorkerFailure):validate_url('http://127.0.0.1:8000')

if __name__=='__main__':unittest.main()
