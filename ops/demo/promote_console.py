"""Promote only the HTTPS root entry; retain the immutable historical Demo."""
import argparse,hashlib,json,os,urllib.request
from pathlib import Path
from deploy_static import ALIAS,CADDY,ROOT,SERVICE,ORIGIN,capture,run,link,certificate,verify,verify_http_redirect,request
OLD='\t@demo_entry {\n\t\tpath / /demo\n\t\tmethod GET HEAD\n\t}\n\tredir @demo_entry /demo/ 302'
NEW='\t@console_entry {\n\t\tpath /\n\t\tmethod GET HEAD\n\t}\n\tredir @console_entry /console 302\n\t@demo_entry {\n\t\tpath /demo\n\t\tmethod GET HEAD\n\t}\n\tredir @demo_entry /demo/ 302'
def render(base,expected_sha):
 if hashlib.sha256(base).hexdigest()!=expected_sha:raise ValueError('old Caddy configuration SHA mismatch')
 text=base.decode('utf-8')
 if text.count(OLD)!=1:raise ValueError('expected exact historical root matcher')
 return text.replace(OLD,NEW,1).encode('utf-8')
def switch(old,new,reload,accept):
 try:link(new,ALIAS);reload();accept()
 except BaseException:link(old,ALIAS);reload();verify();raise

def main():
 p=argparse.ArgumentParser();p.add_argument('--revision',required=True);p.add_argument('--old-config-sha256',required=True);a=p.parse_args()
 assert os.geteuid()==0 and len(a.revision)==40 and all(c in '0123456789abcdef' for c in a.revision)
 assert ALIAS.is_symlink();old=ALIAS.resolve();base=ALIAS.read_bytes();new=render(base,a.old_config_sha256)
 pid=capture(['systemctl','show',SERVICE,'-p','MainPID']);cert=certificate();verify()
 # Verify the fixed Controller is healthy and actually serves the new console.
 info=json.loads(capture(['docker','inspect','lastcore-staging-'+a.revision[:12]+'-controller-1']))[0]
 assert info['State']['Health']['Status']=='healthy' and info['Config']['Labels']['org.opencontainers.image.revision']==a.revision
 console=request('/console');assert b'console.js' in console
 demo=request('/demo/');kit=hashlib.sha256(request('/demo/lastcore-demo-kit.zip')).hexdigest()
 config=ROOT/'configs'/('console-'+a.revision+'.Caddyfile')
 with config.open('xb') as f:f.write(new)
 os.chmod(str(config),0o644)
 run(['runuser','-u','lastcore-caddy','--',CADDY,'validate','--config',str(config),'--adapter','caddyfile'])
 def reload():
  import time
  run(['systemctl','kill','--kill-who=main','--signal=SIGUSR1',SERVICE]);time.sleep(.3)
 def accept():
  assert capture(['systemctl','show',SERVICE,'-p','MainPID'])==pid and certificate()==cert
  verify();verify_http_redirect()
  for method in ['GET','HEAD']:
   with urllib.request.urlopen(urllib.request.Request(ORIGIN+'/',method=method),timeout=8) as r:assert r.status==200 and r.geturl()==ORIGIN+'/console'
  assert request('/')==console and request('/demo/')==demo
  assert hashlib.sha256(request('/demo/lastcore-demo-kit.zip')).hexdigest()==kit
  request('/demo/does-not-exist',404);request('/demo/',405,'POST')
 switch(old,config,reload,accept)
 result={'controller_source_commit':a.revision,'root_get_head_console':True,'default_tls_verified':True,'caddy_pid_and_certificate_unchanged':True,'demo_and_canonical_zip_unchanged':True,'api_auth_and_http_acme_behavior_preserved':True,'old_configuration':str(old),'new_configuration_sha256':hashlib.sha256(new).hexdigest(),'passed':True}
 (ROOT/'console-entry-evidence.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':main()
