import hashlib,importlib.util,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import prepare_static as p
import deploy_static as d
class StaticOps(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.site=self.root/'site';self.site.mkdir();(self.site/'index.html').write_text('<html>public</html>');self.zip=self.site/'lastcore-demo-kit.zip'
  with zipfile.ZipFile(self.zip,'w') as z:z.writestr('offline.html','<html>historical public replay</html>')
 def test_complete_packet(self):self.assertEqual(set(p.files(self.site)),{'index.html','lastcore-demo-kit.zip'})
 def test_missing_kit(self):
  self.zip.unlink()
  with self.assertRaises(ValueError):p.files(self.site)
 def test_no_symlink(self):
  (self.site/'link.txt').symlink_to('/etc/passwd')
  with self.assertRaises(ValueError):p.files(self.site)
 def test_private_file(self):
  (self.site/'OWNER-ACCESS.txt').write_text('path')
  with self.assertRaises(ValueError):p.files(self.site)
 def test_private_payload(self):
  (self.site/'public.json').write_text('{"node_token":"fake-test-secret"}')
  with self.assertRaises(ValueError):p.files(self.site)
 def test_zip_escape_and_self(self):
  for name in ['../secret.txt','lastcore-demo-kit.zip','weights.gguf','state.sqlite3']:
   with zipfile.ZipFile(self.zip,'w') as z:z.writestr(name,'bad')
   with self.assertRaises(ValueError):p.files(self.site)
 def test_exact_caddy_route(self):
  base=(ROOT.parent/'public-ip-https/Caddyfile').read_bytes();self.assertEqual(hashlib.sha256(base).hexdigest(),p.BASE_CADDY_SHA);new=p.render_caddy(base)
  self.assertIn(b'handle_path /demo/*',new);self.assertIn(b'admin off',new);self.assertIn(b'reverse_proxy 127.0.0.1:8000',new);self.assertEqual(new.replace(p.ROUTE.encode(),p.PROXY.encode()),base)
  with self.assertRaises(ValueError):p.render_caddy(base+b'\n')
 def test_accept_failure_rolls_config_back(self):
  old=self.root/'old';old.write_text('old');new=self.root/'new';new.write_text('new');alias=self.root/'Caddyfile';alias.symlink_to(old);current=self.root/'current';release=self.root/'release';release.mkdir();calls=[]
  original_alias,verify=d.ALIAS,d.verify;d.ALIAS=alias;d.verify=lambda:calls.append('verified')
  try:
   def fail():raise RuntimeError('injected acceptance failure')
   with self.assertRaises(RuntimeError):d.switch(new,release,current,old,None,lambda:calls.append('reload'),fail)
   self.assertEqual(alias.resolve(),old.resolve());self.assertFalse(current.exists());self.assertEqual(calls,['reload','reload','verified']);self.assertTrue(release.exists())
  finally:d.ALIAS=original_alias;d.verify=verify
if __name__=='__main__':unittest.main()
