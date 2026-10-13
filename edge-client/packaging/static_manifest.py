"""Inspect native artifacts without executing either packaged entry point."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[2]


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_binary(path):
    data=path.read_bytes()
    if sys.platform=='win32':
        if data[:2]!=b'MZ':raise RuntimeError('Missing PE header')
        offset=struct.unpack_from('<I',data,0x3c)[0]
        if data[offset:offset+4]!=b'PE\0\0':raise RuntimeError('Invalid PE header')
        machine=struct.unpack_from('<H',data,offset+4)[0]
        if machine!=0x8664:raise RuntimeError('Expected AMD64 PE')
        optional=offset+24;magic=struct.unpack_from('<H',data,optional)[0]
        if magic!=0x20b:raise RuntimeError('Expected PE32+')
        certificate_size=struct.unpack_from('<I',data,optional+112+4*8+4)[0]
        return {'format':'PE32+','machine':'AMD64','authenticode_certificate_present':certificate_size!=0}
    if data[:4]!=b'\x7fELF' or data[4]!=2 or struct.unpack_from('<H',data,18)[0]!=62:
        raise RuntimeError('Expected x86_64 ELF64')
    return {'format':'ELF64','machine':'x86_64'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();bundle=Path(args.bundle).resolve();output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    commit=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    if os.environ.get('EDGE_SOURCE_SHA')!=commit:
        raise RuntimeError('Actual checkout does not match the explicitly reviewed CI source head')
    target='windows-x86_64' if sys.platform=='win32' else 'linux-x86_64'
    extension='.exe' if sys.platform=='win32' else ''
    binaries={name:inspect_binary(bundle/(name+extension)) for name in ('LastCoreEdge','LastCoreLinearEdge')}
    license_root=bundle/'_internal/licenses';license_data=json.loads((license_root/'manifest.json').read_text())
    for name,digest in license_data['files'].items():
        if sha(license_root/name)!=digest:raise RuntimeError('License transport mismatch: '+name)
    import certifi
    ca=bundle/'_internal/certifi/cacert.pem'
    if not ca.is_file() or sha(ca)!=sha(Path(certifi.where())):
        raise RuntimeError('Official certifi CA bundle hook did not transport the exact trust bundle')
    files={}
    glibc=set();external=set()
    for path in sorted(bundle.rglob('*')):
        if path.is_symlink():raise RuntimeError('Unexpected portable symlink')
        if not path.is_file():continue
        relative=path.relative_to(bundle).as_posix();files[relative]={'bytes':path.stat().st_size,'sha256':sha(path)}
        if sys.platform.startswith('linux') and path.read_bytes()[:4]==b'\x7fELF':
            versions=subprocess.check_output(['readelf','--version-info',str(path)],text=True)
            glibc.update(re.findall(r'GLIBC_(\d+\.\d+)',versions))
            dynamic=subprocess.check_output(['readelf','--dynamic',str(path)],text=True)
            external.update(re.findall(r'\(NEEDED\).*?\[(.*?)\]',dynamic))
    runtime_inputs=['edge-client/linear_edge.py','edge-client/gui.py','edge-client/view_state.py',
                    'node-agent/desktop/env_file.py']+[
                    'node-agent/lastcore_node_agent/'+name for name in
                    ('__init__.py','agent.py','config.py','controller.py','credentials.py','executor.py')]
    build_inputs=['LICENSE','.github/workflows/edge-portable-build.yml',
                  'node-agent/desktop/collect_licenses.py',
                  'edge-client/packaging/prepare_licenses.py','edge-client/packaging/portable.spec',
                  'edge-client/packaging/static_manifest.py',
                  f'edge-client/packaging/locks/{target}.txt',f'edge-client/packaging/locks/{target}.json',
                  'edge-client/packaging/vendor-licenses/sources.json',
                  'edge-client/packaging/vendor-licenses/tcl-8.6.15-license.terms',
                  'edge-client/packaging/vendor-licenses/tk-8.6.15-license.terms']
    sources={};git_blobs={}
    for relative in runtime_inputs+build_inputs:
        path=ROOT/relative
        if not path.is_file() or path.is_symlink():raise RuntimeError('Missing regular required input: '+relative)
        try:
            original=subprocess.check_output(['git','-C',str(ROOT),'show',commit+':'+relative],stderr=subprocess.DEVNULL)
            blob=subprocess.check_output(['git','-C',str(ROOT),'rev-parse',commit+':'+relative],text=True).strip()
        except subprocess.CalledProcessError:raise RuntimeError('Untracked required input: '+relative) from None
        if path.read_bytes()!=original:raise RuntimeError('Required input differs from fixed Git: '+relative)
        sources[relative]=sha(path);git_blobs[relative]=blob
    if sys.platform=='win32':
        import pefile
        for component in ('tcl','tk'):
            dll=bundle/'_internal'/(component+'86t.dll')
            pe=pefile.PE(str(dll));info=pe.VS_FIXEDFILEINFO[0]
            version=(info.FileVersionMS>>16,info.FileVersionMS&65535,info.FileVersionLS>>16,info.FileVersionLS&65535)
            if version[:3]!=(8,6,15):raise RuntimeError('Native Tcl/Tk does not match pinned vendor license version')
            pe.close()
    maximum=max(glibc,key=lambda v:tuple(map(int,v.split('.')))) if glibc else None
    manifest={'schema_version':1,'source_commit':commit,'target':target,'python_version':platform.python_version(),
              'python_abi':sys.implementation.cache_tag,'os':platform.system(),'architecture':platform.machine(),
              'distribution':'portable development preview','signed':False,'runtime_acceptance_performed':False,
              'windows_support_target':'Windows 10/11 x64 preview' if sys.platform=='win32' else None,
              'gui_launched':False,'controller_registration_or_jobs_created':0,
              'binary_headers':binaries,'linux_glibc_required_at_least':maximum,
              'linux_required_shared_libraries':sorted(external),'source_files':sources,'files':files,
              'source_git_blobs':git_blobs,'runtime_inputs':runtime_inputs,'build_inputs':build_inputs,
              'license_manifest':license_data,'lock_sha256':sha(ROOT/'edge-client/packaging/locks'/f'{target}.txt')}
    manifest['ca_bundle']={'relative_path':'_internal/certifi/cacert.pem','sha256':sha(ca)}
    if any(v.get('authenticode_certificate_present') for v in binaries.values()):
        raise RuntimeError('Unexpected executable signature; review separately')
    stem=f'LastCoreEdge-0.2.0-preview-{target}-{commit[:8]}'
    archive=output/(stem+'.zip')
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for path in sorted(bundle.rglob('*')):
            if path.is_file():z.write(path,'LastCoreEdge/'+path.relative_to(bundle).as_posix())
        z.writestr('LastCoreEdge/BUILD-MANIFEST.json',json.dumps(manifest,indent=2)+'\n')
        z.writestr('LastCoreEdge/PREVIEW-README.txt',
                    'LastCore Edge 0.2.0 portable development preview\n'
                    'Build-only: no GUI/client/node/model/task runtime acceptance performed.\n'
                    'Unsigned; use your OS normal source/trust review. No installer or auto-start changes.\n'
                    'GUI: LastCoreEdge(.exe); console: LastCoreLinearEdge(.exe) --env-file <existing private file>\n'
                    'Keep the original node state and independently confirm its original HTTPS issuing origin.\n'
                    'Exit the old client first. No registration credential, node token or private config is included.\n'
                    'Linux external shared libraries and measured GLIBC symbols are in BUILD-MANIFEST.json.\n'
                    'Full LastCore/Python/Tcl/Tk/OpenSSL/dependency notices are under _internal/licenses.\n')
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:raise RuntimeError('Corrupt ZIP')
    manifest['archive_sha256']=sha(archive);manifest['archive_bytes']=archive.stat().st_size
    (output/(stem+'.json')).write_text(json.dumps(manifest,indent=2)+'\n')
    (output/(stem+'.zip.sha256')).write_text(f"{manifest['archive_sha256']}  {archive.name}\n")
    print(json.dumps({'source_commit':commit,'target':target,'archive':archive.name,'sha256':manifest['archive_sha256'],
                      'glibc_required_at_least':maximum,'runtime_acceptance_performed':False},indent=2))


if __name__=='__main__':main()
