"""Bounded public-only static packet and exact Caddy route for LastCore."""
import argparse,hashlib,json,re,shutil,stat,zipfile
from pathlib import Path
BASE_CADDY_SHA='68ae1fa219c70a9b4675457bbdefda5084607d451ad3e3f9057278b7785db484'
PROXY='\treverse_proxy 127.0.0.1:8000 {\n\t\ttransport http {\n\t\t\tdial_timeout 3s\n\t\t\tresponse_header_timeout 30s\n\t\t}\n\t}'
ROUTE='''\t@demo_entry {
\t\tpath / /demo
\t\tmethod GET HEAD
\t}
\tredir @demo_entry /demo/ 302
\thandle_path /demo/* {
\t\t@demo_write not method GET HEAD
\t\trespond @demo_write "Method not allowed." 405
\t\troot * /opt/lastcore/demo/current
\t\tfile_server
\t}
\thandle {
'''+PROXY+'\n\t}'
ALLOWED={'.html','.css','.js','.json','.svg','.png','.jpg','.jpeg','.webp','.woff2','.txt','.md'}
DENY={'owner-access.txt','node-credentials.json','current-job.json','worker.json','.env','.git','registration.pending'}
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def safe_name(name):
 p=Path(name)
 if p.is_absolute() or '..' in p.parts or any(part.startswith('.') or part.lower() in DENY for part in p.parts):raise ValueError('private or unsafe public path')
 if p.suffix.lower() not in ALLOWED:raise ValueError('unsupported public file type')
def safe_content(name,data):
 if Path(name).suffix.lower() in {'.html','.js','.json','.svg','.txt','.md','.css'}:
  text=data.decode('utf-8')
  if 'PRIVATE KEY-----' in text or re.search(r'(node_token|registration_token|controller_admin_token|controller_registration_token)[\"\']?\s*[:=]',text,re.I):raise ValueError('credential material in public content')
def check_zip(path):
 total=0
 with zipfile.ZipFile(path) as archive:
  for info in archive.infolist():
   if info.is_dir():continue
   parts=Path(info.filename).parts
   if Path(info.filename).is_absolute() or '..' in parts or any(v.lower() in DENY or v in {'.git','.cache','.native-cache','.toolchain'} for v in parts):raise ValueError('private/unsafe unified ZIP path')
   if Path(info.filename).suffix.lower() in {'.apk','.gguf','.key','.pem','.zip','.sqlite','.sqlite3','.db','.jks','.keystore'}:raise ValueError('private/model/recursive ZIP content')
   if stat.S_ISLNK(info.external_attr>>16) or info.flag_bits&1:raise ValueError('encrypted/symlink zip entry')
   total+=info.file_size
   if total>50*1024*1024:raise ValueError('demo zip exceeds bound')
   archive.read(info)  # Verify CRC; source/log redaction belongs to the unique independently reviewed material ZIP.
def files(root):
 result={};total=0
 if root.is_symlink():raise ValueError('site root must not be a symlink')
 for p in sorted(root.rglob('*')):
  if p.is_symlink():raise ValueError('symlink public entry')
  if not p.is_file():continue
  name=p.relative_to(root).as_posix()
  if name=='export-standalone.py':continue  # Source helper is not public static content.
  if name=='lastcore-demo-kit.zip':check_zip(p)
  else:
   safe_name(name);safe_content(name,p.read_bytes())
  total+=p.stat().st_size
  if total>70*1024*1024:raise ValueError('static packet exceeds bound')
  result[name]={'sha256':sha(p),'bytes':p.stat().st_size}
 if not {'index.html','lastcore-demo-kit.zip'}<=set(result):raise ValueError('complete index and demo kit required')
 return result
def render_caddy(base):
 if hashlib.sha256(base).hexdigest()!=BASE_CADDY_SHA:raise ValueError('Caddy baseline changed; do not overwrite')
 text=base.decode()
 if text.count(PROXY)!=1:raise ValueError('unexpected original proxy')
 return text.replace(PROXY,ROUTE).encode()
def main():
 p=argparse.ArgumentParser();p.add_argument('--site-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--source-commit',required=True);p.add_argument('--frontend-commit',required=True);p.add_argument('--canonical-zip-name',required=True);a=p.parse_args()
 if any(len(v)!=40 or any(c not in '0123456789abcdef' for c in v) for v in [a.source_commit,a.frontend_commit]):raise ValueError('fixed full SHA required')
 members=files(a.site_dir);a.output.mkdir(exist_ok=False);site=a.output/'site';site.mkdir()
 for name in members:
  target=site/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(a.site_dir/name,target)
 assert files(site)==members
 operator_files={name:sha(Path(__file__).parent/name) for name in ['prepare_static.py','deploy_static.py']}
 manifest={'canonical_zip_name':a.canonical_zip_name,'operator_files':operator_files,'source_commit':a.source_commit,'frontend_commit':a.frontend_commit,'files':members,'baseline_caddy_sha256':BASE_CADDY_SHA,'scope':'public static demo only; Controller85/TLS storage unchanged'}
 (a.output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
 for name in ['prepare_static.py','deploy_static.py']:shutil.copyfile(Path(__file__).parent/name,a.output/name)
 print(json.dumps({'packet':str(a.output),'files':len(members),'manifest_sha256':sha(a.output/'manifest.json')}))
if __name__=='__main__':main()
