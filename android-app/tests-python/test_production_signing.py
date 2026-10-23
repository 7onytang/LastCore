"""Synthetic owner configuration and Fake SDK subprocesses; no keys/signatures/devices."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import unittest
from unittest.mock import patch
from contextlib import contextmanager, ExitStack, redirect_stdout, redirect_stderr
import io
import inspect
import tempfile
from surface_fixture import PERMISSIONS, XML


def cases(name, values):
    def decorate(function):
        function.boundary_cases=[{name:value} for value in values]
        return function
    return decorate


class Patches:
    def __init__(self):self.stack=ExitStack()
    def setattr(self,owner,name,value):self.stack.enter_context(patch.object(owner,name,value))
    def close(self):self.stack.close()


@contextmanager
def synthetic_snapshot(repo,env,runner):
    with tempfile.TemporaryDirectory() as folder:
        target=Path(folder)/'snapshot'
        shutil.copytree(repo,target,ignore=shutil.ignore_patterns('build','verification'))
        yield EXPECTED['source_commit'],target/'android-app'

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lastcore_owner_signing', PROJECT/'production-signing.py')
signing = importlib.util.module_from_spec(spec); sys.modules[spec.name] = signing; spec.loader.exec_module(signing)
spec = importlib.util.spec_from_file_location('lastcore_binding_fixture', PROJECT/'artifact-binding.py')
binding = importlib.util.module_from_spec(spec); spec.loader.exec_module(binding)
FINGERPRINT = 'ab'*32
SECRET = 'synthetic !$(not-executed) "quoted" = value'
EXPECTED = {'source_commit':'1'*40,'source_tracked_clean':True,'android_source_files':{},'timezone':'Asia/Shanghai'}


def config_fixture(tmp_path, **updates):
    repo=tmp_path/'repo';root=repo/'android-app';root.mkdir(parents=True)
    store=tmp_path/'owner-placeholder.jks';store.write_bytes(b'not a keystore or private key');store.chmod(0o600)
    root.joinpath('source-fixture.txt').write_text('fixed synthetic source')
    values=dict(storeFile=str(store),keyAlias='owner-release',storePassword=SECRET,keyPassword=SECRET+' key',
                expectedCertificateSha256=FINGERPRINT,versionCode='2',versionName='0.1.1')
    values.update(updates)
    config=tmp_path/'owner.properties'
    config.write_text('\n'.join(f'{key}={value}' for key,value in values.items())+'\n');config.chmod(0o600)
    return repo,root,store,config,values


def unsigned_fixture(root):
    assets=root/'app/src/main/assets/licenses';assets.mkdir(parents=True)
    (assets/'LICENSE.txt').write_text('synthetic public license fixture')
    apk=root/'app/build/outputs/apk/release/app-release-unsigned.apk';apk.parent.mkdir(parents=True)
    with zipfile.ZipFile(apk,'w') as archive:
        archive.writestr('assets/source-binding.json',json.dumps(EXPECTED))
        archive.writestr('classes.dex',root.joinpath('source-fixture.txt').read_bytes())
        archive.writestr('assets/licenses/LICENSE.txt',(assets/'LICENSE.txt').read_bytes())
    return apk


def certificate(digest=FINGERPRINT,subject='CN=Owner Release',v2='true',count='1'):
    return f'Verifies\nVerified using v2 scheme (APK Signature Scheme v2): {v2}\nNumber of signers: {count}\nSigner #1 certificate DN: {subject}\nSigner #1 certificate SHA-256 digest: {digest}\n'


class FakeSDK:
    def __init__(self,root,certificate_text=None,fail=None,debuggable=False):
        self.root=root;self.certificate=certificate_text or certificate();self.fail=fail
        self.debuggable=debuggable;self.calls=[]
    def __call__(self,command,**kwargs):
        self.calls.append((list(command),dict(kwargs.get('env') or {})))
        stage=Path(command[0]).name
        if self.fail==stage:
            return SimpleNamespace(returncode=1,stdout=SECRET,stderr=SECRET+' private config content')
        if stage=='build-apk.sh':
            unsigned_fixture(Path(command[0]).parent)
        elif stage=='zipalign':
            shutil.copyfile(command[-2],command[-1])
        elif stage=='apksigner' and command[1]=='sign':
            shutil.copyfile(command[-1],command[command.index('--out')+1])
        elif stage=='apksigner':
            return SimpleNamespace(returncode=0,stdout=self.certificate,stderr='')
        elif stage=='aapt' and command[2]=='badging':
            return SimpleNamespace(returncode=0,stdout="package: name='ai.lastcore.android' versionCode='2' versionName='0.1.1'\nsdkVersion:'29'\ntargetSdkVersion:'35'\n"+PERMISSIONS+
                                   ('application-debuggable\n' if self.debuggable else ''),stderr='')
        elif stage=='aapt':
            return SimpleNamespace(returncode=0,stdout=XML,stderr='')
        return SimpleNamespace(returncode=0,stdout='',stderr='')


def bound_fixture(root,sdk,monkeypatch):
    monkeypatch.setattr(binding.subprocess,'check_output',lambda command,**kw:sdk(command,**kw).stdout)
    def factory(snapshot_root):
        binding.ROOT=snapshot_root
        return SimpleNamespace(source=lambda:dict(EXPECTED),verify_apk=binding.verify_apk)
    return factory


@cases('missing',sorted(signing.FIELDS))
def test_each_missing_owner_field_blocks_before_any_build(case,tmp_path,missing):
    repo,root,store,config,values=config_fixture(tmp_path)
    del values[missing];config.write_text('\n'.join(f'{k}={v}' for k,v in values.items()))
    calls=[]
    with case.assertRaises(signing.ProductionError):
        signing.build_production(config,root/'verification/production',root=root,repo=repo,
            runner=lambda *a,**k:calls.append(a),binding=lambda root:SimpleNamespace(source=lambda:dict(EXPECTED)))
    assert not calls and not (root/'verification').exists()


@cases('value',['','not-hex','ab'*31,'aa:bb'])
def test_bad_expected_fingerprint_blocks_before_build(case,tmp_path,value):
    repo,root,store,config,_=config_fixture(tmp_path,expectedCertificateSha256=value)
    with case.assertRaises(signing.ProductionError):signing.load_config(config,repo)


def test_private_config_and_key_path_policy_does_not_read_key_material(case,tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path)
    original=Path.read_bytes
    def forbidden(path):
        if path==store:case.fail('key material must not be inspected by config loader')
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',forbidden)
    loaded=signing.load_config(config,repo)
    assert loaded.store_password==SECRET and SECRET not in repr(loaded)
    config.chmod(0o644)
    with case.assertRaises(signing.ProductionError):signing.load_config(config,repo)
    config.chmod(0o600)
    copied=repo/'private.properties';shutil.copyfile(config,copied);copied.chmod(0o600)
    with case.assertRaises(signing.ProductionError):signing.load_config(copied,repo)


@cases('updates',[{'keyAlias':'androiddebugkey'},{'versionCode':'0'},{'versionCode':'2147483648'},
                                    {'versionName':'../secret'},{'unknownSecret':SECRET}])
def test_invalid_owner_identity_or_version_is_fail_closed(case,tmp_path,updates):
    repo,root,store,config,_=config_fixture(tmp_path,**updates)
    with case.assertRaises(signing.ProductionError):signing.load_config(config,repo)


@cases('text',[certificate(digest='cd'*32),certificate(subject='CN=Android Debug, O=Android, C=US'),
                                 certificate(v2='false'),certificate(count='2'), 'Verifies'])
def test_signature_failure_never_leaves_production_labeled_outputs(case,tmp_path,monkeypatch,text):
    repo,root,store,config,_=config_fixture(tmp_path)
    sdk=FakeSDK(root,text);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    with case.assertRaises(signing.ProductionError):
        signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={},snapshot_factory=synthetic_snapshot)
    assert not list(output.iterdir())
    assert not (root/'app/build/outputs/apk/release/app-release-unsigned.apk').exists()  # No live output overwritten.


def test_fake_signed_success_requires_alignment_fingerprint_full_policy_and_public_version(case,tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path)
    sdk=FakeSDK(root);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    report=signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={},snapshot_factory=synthetic_snapshot)
    names=[Path(command[0]).name for command,env in sdk.calls]
    assert names.index('zipalign')<names.index('apksigner')
    assert report['production_signed'] and report['signer']['certificate_sha256']==FINGERPRINT
    assert report['device_acceptance']=='unverified' and report['version_code']==2
    assert len(list(output.iterdir()))==3
    for command,env in sdk.calls:
        assert not any(SECRET in value for value in command)
        if Path(command[0]).name=='apksigner' and command[1]=='sign':
            assert env[signing.STORE_PASS_ENV]==SECRET
            assert 'env:'+signing.STORE_PASS_ENV in command and 'env:'+signing.KEY_PASS_ENV in command
        else:
            assert signing.STORE_PASS_ENV not in env and signing.KEY_PASS_ENV not in env
    assert not any(SECRET in path.read_text() for path in output.glob('*.json'))
    calls=len(sdk.calls)
    with case.assertRaises(signing.ProductionError):
        signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={},snapshot_factory=synthetic_snapshot)
    assert len(sdk.calls)==calls  # Existing bytes preserved; no silent replacement build.


@cases('failure',['build-apk.sh','zipalign','apksigner'])
def test_tool_errors_with_sensitive_placeholders_are_never_echoed_or_published(case,tmp_path,monkeypatch,capsys,failure):
    repo,root,store,config,_=config_fixture(tmp_path)
    sdk=FakeSDK(root,fail=failure);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    actual=signing.build_production
    monkeypatch.setattr(signing,'build_production',lambda *a,**kw:actual(config,output,root=root,repo=repo,
        runner=sdk,binding=helper,environment={},snapshot_factory=synthetic_snapshot))
    assert signing.main(['--signing-config',str(config)])==2
    captured=capsys.readouterr()
    assert SECRET not in captured.out+captured.err and str(config) not in captured.out+captured.err
    assert not list(output.iterdir())


def test_debuggable_or_compiled_policy_failure_never_publishes(case,tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path)
    sdk=FakeSDK(root,debuggable=True);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    with case.assertRaises(signing.ProductionError):
        signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={},snapshot_factory=synthetic_snapshot)
    assert not list(output.iterdir())


def test_cli_unknown_secret_argument_is_redacted(case,capsys):
    assert signing.main(['--store-password',SECRET])==2
    captured=capsys.readouterr()
    assert SECRET not in captured.out+captured.err


def test_missing_unsigned_cannot_fall_back_to_debug(case,tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path)
    helper=lambda root:SimpleNamespace(source=lambda:dict(EXPECTED))
    root.joinpath('app/build/outputs/apk/debug').mkdir(parents=True)
    root.joinpath('app/build/outputs/apk/debug/app-debug.apk').write_bytes(b'debug fixture')
    with case.assertRaisesRegex(signing.ProductionError,'Unsigned release input missing'):
        signing.build_production(config,root/'verification/production',root=root,repo=repo,
            runner=lambda *a,**kw:SimpleNamespace(returncode=0,stdout='',stderr=''),binding=helper,environment={},snapshot_factory=synthetic_snapshot)


def test_default_shell_path_and_gradle_unsigned_behavior_remain_intact(case,):
    text=(PROJECT/'build-apk.sh').read_text()
    assert 'assembleDebug assembleDebugAndroidTest assembleRelease lintDebug' in text
    assert 'if [ "$#" -gt 0 ]' in text and 'if [ "$1" != "--production" ]' in text
    assert 'exec python3 production-signing.py "$@"' in text
    gradle=(PROJECT/'app/build.gradle').read_text()
    assert 'signingConfig' not in gradle
    assert '.orElse(1).get()' in gradle and '.orElse("0.1.0-draft").get()' in gradle


def test_called_process_error_is_redacted_without_production_output(case,tmp_path,monkeypatch,capsys):
    repo,root,store,config,_=config_fixture(tmp_path)
    output=root/'verification/production'
    helper=lambda root:SimpleNamespace(source=lambda:dict(EXPECTED))
    def failed(command,**kwargs):
        raise subprocess.CalledProcessError(9,command,output=SECRET,stderr=SECRET)
    actual=signing.build_production
    monkeypatch.setattr(signing,'build_production',lambda *a,**kw:actual(config,output,root=root,repo=repo,
        runner=failed,binding=helper,environment={},snapshot_factory=synthetic_snapshot))
    assert signing.main(['--signing-config',str(config)])==2
    captured=capsys.readouterr()
    assert SECRET not in captured.out+captured.err and str(config) not in captured.out+captured.err
    assert not list(output.iterdir())


def test_publication_failure_rolls_back_created_apk_without_metadata(case,tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path)
    sdk=FakeSDK(root);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    original=signing.os.link
    calls=[]
    def link(source,destination):
        calls.append(destination)
        if len(calls)==2:raise OSError(SECRET)
        return original(source,destination)
    monkeypatch.setattr(signing.os,'link',link)
    with case.assertRaisesRegex(signing.ProductionError,'Publication failed'):
        signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={},snapshot_factory=synthetic_snapshot)
    assert not list(output.iterdir())


def test_wrong_compiled_owner_version_does_not_publish(case,tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path,versionName='0.1.2')
    sdk=FakeSDK(root);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    with case.assertRaisesRegex(signing.ProductionError,'identity/version'):
        signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={},snapshot_factory=synthetic_snapshot)
    assert not list(output.iterdir())




def test_fixed_git_snapshot_ignores_live_source_pollution_during_fake_build(case,tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path)
    for name in ('artifact-binding.py','.gitignore'):
        shutil.copyfile(PROJECT/name,root/name)
    assets=root/'app/src/main/assets/licenses';assets.mkdir(parents=True)
    (assets/'LICENSE.txt').write_text('synthetic public license fixture')
    git_env={**os.environ,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull}
    for command in (['git','init','-q',str(repo)],['git','-C',str(repo),'add','.'],
                    ['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                     'commit','--no-gpg-sign','-q','-m','fixed fixture']):
        subprocess.run(command,env=git_env,check=True,capture_output=True)
    commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],env=git_env,text=True).strip()
    public=FakeSDK(root)
    def runner(command,**kwargs):
        if command[0]=='git':return subprocess.run(command,**kwargs)
        if Path(command[0]).name=='build-apk.sh':
            snapshot_root=Path(command[0]).parent
            root.joinpath('source-fixture.txt').write_text('poisoned live source')
            expected=signing.binding_module(snapshot_root).source()
            output=snapshot_root/'app/build/outputs/apk/release/app-release-unsigned.apk';output.parent.mkdir(parents=True)
            with zipfile.ZipFile(output,'w') as archive:
                archive.writestr('assets/source-binding.json',json.dumps(expected))
                archive.writestr('assets/licenses/LICENSE.txt',(snapshot_root/'app/src/main/assets/licenses/LICENSE.txt').read_bytes())
                archive.writestr('classes.dex',snapshot_root.joinpath('source-fixture.txt').read_bytes())
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        return public(command,**kwargs)
    monkeypatch.setattr(binding.subprocess,'check_output',subprocess.check_output)
    def factory(snapshot_root):
        module=signing.binding_module(snapshot_root)
        # Its Git reads are real local metadata; SDK reads are Fake and never access a key.
        original=module.subprocess.check_output
        def command(args,**kwargs):
            return original(args,**kwargs) if args[0]=='git' else public(args,**kwargs).stdout
        monkeypatch.setattr(module.subprocess,'check_output',command)
        return module
    output=tmp_path/'published-fixture'
    with patch.dict(os.environ):
        os.environ.pop('GITHUB_SHA',None)
        report=signing.build_production(config,output,root=root,repo=repo,runner=runner,binding=factory,environment=git_env)
    assert report['source_commit']==commit and root.joinpath('source-fixture.txt').read_text()=='poisoned live source'
    apk=next(output.glob('*.apk'))
    with zipfile.ZipFile(apk) as archive:
        assert archive.read('classes.dex')==b'fixed synthetic source'
        assert json.loads(archive.read('assets/source-binding.json'))['source_commit']==commit
    assert report['android_source_files']['android-app/source-fixture.txt']==hashlib.sha256(b'fixed synthetic source').hexdigest()


class OwnerSigningBoundaries(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.tmp_path=Path(folder.name)
        self.patches=Patches();self.addCleanup(self.patches.close)
        self.stdout=io.StringIO();self.stderr=io.StringIO()
        out=redirect_stdout(self.stdout);err=redirect_stderr(self.stderr)
        out.__enter__();err.__enter__()
        self.addCleanup(out.__exit__,None,None,None);self.addCleanup(err.__exit__,None,None,None)

    def readouterr(self):
        return SimpleNamespace(out=self.stdout.getvalue(),err=self.stderr.getvalue())


def register(function,parameters,index):
    def run(self):
        available={'case':self,'tmp_path':self.tmp_path,'monkeypatch':self.patches,'capsys':self,**parameters}
        return function(**{name:available[name] for name in inspect.signature(function).parameters})
    setattr(OwnerSigningBoundaries,function.__name__+'_'+str(index),run)


for name,function in list(globals().items()):
    if name.startswith('test_') and callable(function):
        for index,parameters in enumerate(getattr(function,'boundary_cases',[{}])):
            register(function,parameters,index)


if __name__=='__main__':unittest.main()
