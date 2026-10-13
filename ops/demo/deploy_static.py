"""One reviewed static-only Caddy switch with original configuration rollback."""
import argparse,hashlib,json,os,shutil,socket,ssl,subprocess,time,urllib.request,urllib.error
from pathlib import Path
from prepare_static import files,render_caddy,sha
ROOT=Path('/opt/lastcore/demo');ALIAS=Path('/opt/lastcore/tls/Caddyfile');CADDY='/opt/lastcore/tls/current/caddy';SERVICE='lastcore-caddy.service';ORIGIN='https://8.134.161.123'
def run(args):return subprocess.run(args,check=True,timeout=30)
def capture(args):return subprocess.check_output(args,universal_newlines=True,timeout=10).strip()
def link(target,alias):
 temporary=alias.with_name(alias.name+'.demo-next')
 if os.path.lexists(str(temporary)):raise FileExistsError('temporary alias already exists')
 os.symlink(str(target),str(temporary));os.replace(str(temporary),str(alias))
def request(path,expected=200,method='GET'):
 q=urllib.request.Request(ORIGIN+path,method=method)
 try:
  with urllib.request.urlopen(q,timeout=8) as r:status=r.status;data=r.read(2*1024*1024)
 except urllib.error.HTTPError as e:status=e.code;data=e.read(65536)
 if status!=expected:raise ValueError('HTTP mismatch '+path+' '+str(status))
 return data
def certificate():
 with socket.create_connection(('8.134.161.123',443),timeout=8) as raw:
  with ssl.create_default_context().wrap_socket(raw,server_hostname='8.134.161.123') as s:return hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest()
def verify():
 assert json.loads(request('/healthz'))['status']=='ok';request('/docs');request('/openapi.json');request('/v1/nodes',401)
def switch(config,release,current,old_config,old_current,reload,accept):
 try:
  link(release,current);link(config,ALIAS);reload();accept()
 except BaseException:
  link(old_config,ALIAS)
  if old_current is None:current.unlink()
  else:link(old_current,current)
  reload();verify();raise
def main():
 p=argparse.ArgumentParser();p.add_argument('--packet',type=Path,required=True);p.add_argument('--manifest-sha256',required=True);a=p.parse_args()
 assert os.geteuid()==0,'root operator required';assert sha(a.packet/'manifest.json')==a.manifest_sha256
 m=json.loads((a.packet/'manifest.json').read_text());assert files(a.packet/'site')==m['files'];commit=m['source_commit']
 assert len(commit)==40 and all(c in '0123456789abcdef' for c in commit)
 assert ALIAS.is_symlink();old_config=ALIAS.resolve();base=ALIAS.read_bytes();new_config=render_caddy(base)
 pid=capture(['systemctl','show',SERVICE,'-p','MainPID']).split('=',1)[1];assert pid!='0'
 start=capture(['systemctl','show',SERVICE,'-p','ExecStart']);assert ' run ' in start and '--resume' not in start and '--config /opt/lastcore/tls/Caddyfile' in start
 verify();cert=certificate();current=ROOT/'current';
 for alias in [ALIAS,current]:assert not os.path.lexists(str(alias.with_name(alias.name+'.demo-next')))
 assert not current.exists() or current.is_symlink();old_current=current.resolve() if current.exists() else None
 assert not ROOT.is_symlink()
 ROOT.mkdir(mode=0o755,exist_ok=True);os.chmod(str(ROOT),0o755)
 release=ROOT/'releases'/commit;release.parent.mkdir(mode=0o755,parents=True,exist_ok=True);shutil.copytree(str(a.packet/'site'),str(release))
 for item in [release]+list(release.rglob('*')):os.chmod(str(item),0o755 if item.is_dir() else 0o644)
 config=ROOT/'configs'/(commit+'.Caddyfile');config.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
 with config.open('xb') as f:f.write(new_config)
 os.chmod(str(config),0o644)
 run(['runuser','-u','lastcore-caddy','--',CADDY,'validate','--config',str(config),'--adapter','caddyfile'])
 def reload():
  run(['systemctl','kill','--kill-who=main','--signal=SIGUSR1',SERVICE]);time.sleep(.3)
 def accept():
  assert capture(['systemctl','show',SERVICE,'-p','MainPID']).split('=',1)[1]==pid
  assert capture(['systemctl','is-active',SERVICE])=='active';verify();assert certificate()==cert
  with urllib.request.urlopen(ORIGIN+'/',timeout=8) as r:assert r.geturl()==ORIGIN+'/demo/'
  assert request('/demo/index.html')==(release/'index.html').read_bytes();request('/demo/');request('/')
  request('/demo/does-not-exist',404);request('/demo/.env',404);request('/demo/',405,'POST')
  # Stream the whole small demo ZIP to verify the served file, not just a URL.
  h=hashlib.sha256()
  with urllib.request.urlopen(ORIGIN+'/demo/lastcore-demo-kit.zip',timeout=10) as response:
   while True:
    block=response.read(1024*1024)
    if not block:break
    h.update(block)
  assert h.hexdigest()==m['files']['lastcore-demo-kit.zip']['sha256']
 switch(config,release,current,old_config,old_current,reload,accept)
 result={'source_commit':commit,'frontend_commit':m['frontend_commit'],'caddy_pid_unchanged':True,'public_default_trust':True,'certificate_fingerprint_unchanged':True,'demo_root_and_kit_verified':True,'api_health_docs_and_401_preserved':True,'old_config':str(old_config),'static_release':str(release),'controller85_not_redeployed':True,'passed':True}
 (ROOT/'deployment-evidence.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':main()
