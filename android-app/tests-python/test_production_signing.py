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

import pytest

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
            unsigned_fixture(self.root)
        elif stage=='zipalign':
            shutil.copyfile(command[-2],command[-1])
        elif stage=='apksigner' and command[1]=='sign':
            shutil.copyfile(command[-1],command[command.index('--out')+1])
        elif stage=='apksigner':
            return SimpleNamespace(returncode=0,stdout=self.certificate,stderr='')
        elif stage=='aapt' and command[2]=='badging':
            return SimpleNamespace(returncode=0,stdout="package: name='ai.lastcore.android' versionCode='2' versionName='0.1.1'\nsdkVersion:'29'\ntargetSdkVersion:'35'\nuses-permission: name='android.permission.INTERNET'\n"+
                                   ('application-debuggable\n' if self.debuggable else ''),stderr='')
        elif stage=='aapt':
            return SimpleNamespace(returncode=0,stdout='  A: android:allowBackup(0x01010280)=(type 0x12)0x0\n  A: android:usesCleartextTraffic(0x010104ec)=(type 0x12)0x0\n',stderr='')
        return SimpleNamespace(returncode=0,stdout='',stderr='')


def bound_fixture(root,sdk,monkeypatch):
    monkeypatch.setattr(binding,'ROOT',root)
    monkeypatch.setattr(binding.subprocess,'check_output',lambda command,**kw:sdk(command,**kw).stdout)
    return SimpleNamespace(source=lambda:dict(EXPECTED),verify_apk=binding.verify_apk)


@pytest.mark.parametrize('missing',sorted(signing.FIELDS))
def test_each_missing_owner_field_blocks_before_any_build(tmp_path,missing):
    repo,root,store,config,values=config_fixture(tmp_path)
    del values[missing];config.write_text('\n'.join(f'{k}={v}' for k,v in values.items()))
    calls=[]
    with pytest.raises(signing.ProductionError):
        signing.build_production(config,root/'verification/production',root=root,repo=repo,
            runner=lambda *a,**k:calls.append(a),binding=SimpleNamespace(source=lambda:dict(EXPECTED)))
    assert not calls and not (root/'verification').exists()


@pytest.mark.parametrize('value',['','not-hex','ab'*31,'aa:bb'])
def test_bad_expected_fingerprint_blocks_before_build(tmp_path,value):
    repo,root,store,config,_=config_fixture(tmp_path,expectedCertificateSha256=value)
    with pytest.raises(signing.ProductionError):signing.load_config(config,repo)


def test_private_config_and_key_path_policy_does_not_read_key_material(tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path)
    original=Path.read_bytes
    def forbidden(path):
        if path==store:pytest.fail('key material must not be inspected by config loader')
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',forbidden)
    loaded=signing.load_config(config,repo)
    assert loaded.store_password==SECRET and SECRET not in repr(loaded)
    config.chmod(0o644)
    with pytest.raises(signing.ProductionError):signing.load_config(config,repo)
    config.chmod(0o600)
    copied=repo/'private.properties';shutil.copyfile(config,copied);copied.chmod(0o600)
    with pytest.raises(signing.ProductionError):signing.load_config(copied,repo)


@pytest.mark.parametrize('updates',[{'keyAlias':'androiddebugkey'},{'versionCode':'0'},{'versionCode':'2147483648'},
                                    {'versionName':'../secret'},{'unknownSecret':SECRET}])
def test_invalid_owner_identity_or_version_is_fail_closed(tmp_path,updates):
    repo,root,store,config,_=config_fixture(tmp_path,**updates)
    with pytest.raises(signing.ProductionError):signing.load_config(config,repo)


@pytest.mark.parametrize('text',[certificate(digest='cd'*32),certificate(subject='CN=Android Debug, O=Android, C=US'),
                                 certificate(v2='false'),certificate(count='2'), 'Verifies'])
def test_signature_failure_never_leaves_production_labeled_outputs(tmp_path,monkeypatch,text):
    repo,root,store,config,_=config_fixture(tmp_path)
    sdk=FakeSDK(root,text);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    with pytest.raises(signing.ProductionError):
        signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={})
    assert not list(output.iterdir())
    assert (root/'app/build/outputs/apk/release/app-release-unsigned.apk').exists()


def test_fake_signed_success_requires_alignment_fingerprint_full_policy_and_public_version(tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path)
    sdk=FakeSDK(root);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    report=signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={})
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
    with pytest.raises(signing.ProductionError):
        signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={})
    assert len(sdk.calls)==calls  # Existing bytes preserved; no silent replacement build.


@pytest.mark.parametrize('failure',['build-apk.sh','zipalign','apksigner'])
def test_tool_errors_with_sensitive_placeholders_are_never_echoed_or_published(tmp_path,monkeypatch,capsys,failure):
    repo,root,store,config,_=config_fixture(tmp_path)
    sdk=FakeSDK(root,fail=failure);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    actual=signing.build_production
    monkeypatch.setattr(signing,'build_production',lambda *a,**kw:actual(config,output,root=root,repo=repo,
        runner=sdk,binding=helper,environment={}))
    assert signing.main(['--signing-config',str(config)])==2
    captured=capsys.readouterr()
    assert SECRET not in captured.out+captured.err and str(config) not in captured.out+captured.err
    assert not list(output.iterdir())


def test_debuggable_or_compiled_policy_failure_never_publishes(tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path)
    sdk=FakeSDK(root,debuggable=True);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    with pytest.raises(signing.ProductionError):
        signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={})
    assert not list(output.iterdir())


def test_cli_unknown_secret_argument_is_redacted(capsys):
    assert signing.main(['--store-password',SECRET])==2
    captured=capsys.readouterr()
    assert SECRET not in captured.out+captured.err


def test_missing_unsigned_cannot_fall_back_to_debug(tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path)
    helper=SimpleNamespace(source=lambda:dict(EXPECTED))
    root.joinpath('app/build/outputs/apk/debug').mkdir(parents=True)
    root.joinpath('app/build/outputs/apk/debug/app-debug.apk').write_bytes(b'debug fixture')
    with pytest.raises(signing.ProductionError,match='Unsigned release input missing'):
        signing.build_production(config,root/'verification/production',root=root,repo=repo,
            runner=lambda *a,**kw:SimpleNamespace(returncode=0,stdout='',stderr=''),binding=helper,environment={})


def test_default_shell_path_and_gradle_unsigned_behavior_remain_intact():
    text=(PROJECT/'build-apk.sh').read_text()
    assert 'assembleDebug assembleDebugAndroidTest assembleRelease lintDebug' in text
    assert 'if [ "$#" -gt 0 ]' in text and 'if [ "$1" != "--production" ]' in text
    assert 'exec python3 production-signing.py "$@"' in text
    gradle=(PROJECT/'app/build.gradle').read_text()
    assert 'signingConfig' not in gradle
    assert '.orElse(1).get()' in gradle and '.orElse("0.1.0-draft").get()' in gradle


def test_called_process_error_is_redacted_without_production_output(tmp_path,monkeypatch,capsys):
    repo,root,store,config,_=config_fixture(tmp_path)
    output=root/'verification/production'
    helper=SimpleNamespace(source=lambda:dict(EXPECTED))
    def failed(command,**kwargs):
        raise subprocess.CalledProcessError(9,command,output=SECRET,stderr=SECRET)
    actual=signing.build_production
    monkeypatch.setattr(signing,'build_production',lambda *a,**kw:actual(config,output,root=root,repo=repo,
        runner=failed,binding=helper,environment={}))
    assert signing.main(['--signing-config',str(config)])==2
    captured=capsys.readouterr()
    assert SECRET not in captured.out+captured.err and str(config) not in captured.out+captured.err
    assert not list(output.iterdir())


def test_publication_failure_rolls_back_created_apk_without_metadata(tmp_path,monkeypatch):
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
    with pytest.raises(signing.ProductionError,match='Publication failed'):
        signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={})
    assert not list(output.iterdir())


def test_wrong_compiled_owner_version_does_not_publish(tmp_path,monkeypatch):
    repo,root,store,config,_=config_fixture(tmp_path,versionName='0.1.2')
    sdk=FakeSDK(root);helper=bound_fixture(root,sdk,monkeypatch)
    output=root/'verification/production'
    with pytest.raises(signing.ProductionError,match='identity/version'):
        signing.build_production(config,output,root=root,repo=repo,runner=sdk,binding=helper,environment={})
    assert not list(output.iterdir())
