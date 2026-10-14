"""Synthetic subprocess checks only: no macOS tools, identities or network."""
import contextlib
import importlib.util
import io
import hashlib
import traceback
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('build_dmg', Path(__file__).with_name('build_dmg.py'))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
TEAM = 'ABCDE12345'
IDENTITY = f'Developer ID Application: Synthetic Owner ({TEAM})'
CERTIFICATE = b'synthetic DER public certificate'
FINGERPRINT = hashlib.sha256(CERTIFICATE).hexdigest()
OPTIONS = ['--production', '--signing-identity', IDENTITY, '--team-id', TEAM,
           '--notary-profile', 'synthetic-profile', '--certificate-sha256', FINGERPRINT]


class BuildTests(unittest.TestCase):
    def fake_build(self, flags=(), failure=None, detail=None, notary=None, certificate=CERTIFICATE, mutate_live=False):
        self.calls = []
        self.embedded = None
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        (root/'macos-client').mkdir()
        (root/'macos-client/source.swift').write_text('synthetic source')
        (root/'macos-client/untracked-secret.txt').write_text('do not bind untracked inputs')
        (root/'LICENSE').write_text('synthetic license')
        fixed={'macos-client/source.swift':b'synthetic source', 'macos-client/Package.swift':b'synthetic package', 'LICENSE':b'synthetic license'}
        blob_map={hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest():data for data in fixed.values()}
        entries={name:hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest() for name,data in fixed.items()}
        self.output = root/'output'

        def record(args):
            self.calls.append(list(args))
            if failure and failure(list(args)):
                raise RuntimeError('synthetic tool failed')

        def command_bytes(*args, **kwargs):
            record(args)
            if args[:2]==('git','ls-tree'):
                self.assertEqual(args[4], 'f'*40)
                return b''.join(f'100644 blob {oid}\t{name}\0'.encode() for name,oid in entries.items())
            if args[:3]==('git','cat-file','blob'): return blob_map[args[3]]
            raise AssertionError(args)

        def command(*args, **kwargs):
            record(args)
            if args[:2] == ('uname', '-m'): return 'arm64'
            if args[:2] == ('git', 'status'): return ''
            if args[:2] == ('git', 'rev-parse'): return 'f'*40
            if args[:2] == ('git', 'ls-files'): return 'macos-client/source.swift\0LICENSE\0'
            if args[:2] == ('swift', '--version'): return 'synthetic Swift'
            if args[:2] == ('codesign', '--display'):
                return detail if detail is not None else f'Authority={IDENTITY}\nTeamIdentifier={TEAM}\nTimestamp=synthetic timestamp\nflags=0x10000(runtime)'
            if args[:3] == ('xcrun', 'notarytool', 'submit'):
                return json.dumps(notary if notary is not None else {'status':'Accepted', 'id':'11111111-1111-4111-8111-111111111111'})
            raise AssertionError(args)

        def run(args, **kwargs):
            record(args)
            if args[:2] == ['swift', 'build']:
                if mutate_live: (root/'macos-client/source.swift').write_text('concurrent changed working tree')
                package=Path(args[args.index('--package-path')+1])
                self.assertNotEqual(package, root/'macos-client')
                self.assertEqual((package/'source.swift').read_bytes(),fixed['macos-client/source.swift'])
                self.assertEqual(Path(kwargs['cwd'])/'macos-client', package)
                target = Path(args[args.index('--scratch-path')+1])/'arm64-apple-macosx/release/LastCoreClient'
                target.parent.mkdir(parents=True)
                target.write_bytes(b'synthetic executable, never launched')
            if '--extract-certificates' in args:
                Path(args[args.index('--extract-certificates')+1]+'0').write_bytes(certificate)
            if args[:2] == ['hdiutil', 'create']:
                stage = Path(args[args.index('-srcfolder')+1])
                self.embedded = json.loads((stage/'LastCoreClient.app/Contents/Resources/source-binding.json').read_text())
                Path(args[-1]).write_bytes(b'synthetic DMG, never mounted')
            return subprocess.CompletedProcess(args, 0)

        with patch.object(build, 'ROOT', root), patch.object(build, 'command', command), patch.object(build, 'command_bytes', command_bytes), patch.object(build.subprocess, 'run', run), patch.object(sys, 'argv', ['build_dmg', '--output-dir', str(self.output), *flags]), contextlib.redirect_stdout(io.StringIO()):
            build.main()
        return json.loads(next(self.output.glob('*.json')).read_text())

    def test_development_remains_adhoc_without_apple_submission(self):
        receipt = self.fake_build()
        self.assertEqual(receipt['signing'], 'ad-hoc')
        self.assertFalse(receipt['notarized'])
        self.assertEqual(receipt['release_channel'], 'development')
        self.assertFalse(any(c[0] == 'xcrun' for c in self.calls))
        self.assertTrue(any(c[:4] == ['codesign','--force','--sign','-'] for c in self.calls))

    def test_production_order_and_manifest_only_after_verified_staple(self):
        receipt = self.fake_build(OPTIONS)
        self.assertEqual(receipt['release_channel'], 'production')
        for name in ['notarized','app_signature_verified','hardened_runtime_verified','dmg_signature_verified','staple_validated']:
            self.assertTrue(receipt[name])
        self.assertEqual(receipt['source_commit'], 'f'*40)
        self.assertEqual(receipt['dmg_sha256'], build.digest(next(self.output.glob('*.dmg'))))
        self.assertEqual(set(receipt['source_files']), {'macos-client/source.swift', 'macos-client/Package.swift', 'LICENSE'})
        self.assertFalse(self.embedded['notarized'])
        self.assertEqual(self.embedded['signing'], 'developer-id-requested')
        self.assertTrue(any('--configuration' in c and c[c.index('--configuration')+1]=='release' for c in self.calls))
        stages = [c[:3] for c in self.calls if c[0]=='xcrun']
        self.assertEqual(stages, [['xcrun','notarytool','submit'],['xcrun','stapler','staple'],['xcrun','stapler','validate']])
        self.assertEqual(self.calls[-1][:2], ['hdiutil','verify'])

    def test_missing_or_invalid_configuration_fails_before_any_command(self):
        for flags in [['--production'], ['--signing-identity',IDENTITY], OPTIONS[:4]+['WRONG']+OPTIONS[5:], ['--production','--signing-identity','-','--team-id',TEAM,'--notary-profile','profile']]:
            with self.subTest(flags=flags), patch.object(build, 'command') as command, patch.object(build.subprocess,'run') as run, patch.object(sys,'argv',['build_dmg','--output-dir','/unused',*flags]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): build.main()
                command.assert_not_called();run.assert_not_called()

    def test_wrong_actual_signature_cannot_publish(self):
        for detail in ['Signature=adhoc', f'Authority={IDENTITY}\nTeamIdentifier=OTHER\nTimestamp=t\nflags=runtime', f'Authority={IDENTITY}\nTeamIdentifier={TEAM}\nTimestamp=t', f'Authority={IDENTITY}\nTeamIdentifier={TEAM}\nflags=runtime']:
            with self.subTest(detail=detail):
                with self.assertRaises(RuntimeError): self.fake_build(OPTIONS, detail=detail)
                self.assertEqual(list(self.output.iterdir()), [])
                self.assertFalse(any(c[:3]==['xcrun','notarytool','submit'] for c in self.calls))

    def test_notary_rejection_pending_or_missing_id_cannot_publish(self):
        for result in [{'status':'Invalid'}, {'status':'In Progress'}, {'status':'Accepted'}, {'status':'Accepted','id':'bad-id'}]:
            with self.subTest(result=result):
                with self.assertRaises((RuntimeError,KeyError,ValueError)): self.fake_build(OPTIONS, notary=result)
                self.assertEqual(list(self.output.iterdir()), [])
                self.assertFalse(any(c[:2]==['xcrun','stapler'] for c in self.calls))

    def test_subprocess_failures_leave_no_artifact(self):
        failures = [lambda c:c[:2]==['swift','build'], lambda c:c[:2]==['codesign','--force'], lambda c:c[:2]==['codesign','--verify'], lambda c:c[:2]==['hdiutil','create'], lambda c:c[:3]==['xcrun','notarytool','submit'], lambda c:c[:3]==['xcrun','stapler','staple'], lambda c:c[:3]==['xcrun','stapler','validate']]
        for failure in failures:
            with self.subTest(failure=failure):
                with self.assertRaises(RuntimeError): self.fake_build(OPTIONS, failure=failure)
                self.assertEqual(list(self.output.iterdir()), [])

    def test_live_checkout_mutation_cannot_change_fixed_build_or_manifest(self):
        receipt=self.fake_build(OPTIONS, mutate_live=True)
        self.assertEqual(receipt['source_commit'], 'f'*40)
        self.assertEqual(receipt['source_files']['macos-client/source.swift'], hashlib.sha256(b'synthetic source').hexdigest())
        self.assertNotEqual(receipt['source_files']['macos-client/source.swift'], hashlib.sha256(b'concurrent changed working tree').hexdigest())
        self.assertEqual(receipt['source_git_blobs'], self.embedded['source_git_blobs'])
        self.assertEqual(receipt['source_files'], self.embedded['source_files'])

    def test_final_post_staple_signature_failure_cannot_publish(self):
        count=0
        def fail_final(command):
            nonlocal count
            if command[:2]==['codesign','--verify'] and command[-1].endswith('.dmg'):
                count+=1
                return count==2
            return False
        with self.assertRaises(RuntimeError): self.fake_build(OPTIONS, failure=fail_final)
        self.assertTrue(any(c[:3]==['xcrun','stapler','validate'] for c in self.calls))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_wrong_certificate_cannot_publish_or_submit(self):
        with self.assertRaises(RuntimeError): self.fake_build(OPTIONS, certificate=b'wrong synthetic certificate')
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(any(c[:3]==['xcrun','notarytool','submit'] for c in self.calls))

    def test_captured_tool_errors_do_not_reveal_secret_placeholders(self):
        secret='SECRET_PLACEHOLDER_DO_NOT_PRINT'
        error=subprocess.CalledProcessError(1, ['xcrun',secret], output=secret.encode(), stderr=secret.encode())
        for invoke, target in [(lambda:build.command('xcrun',secret), 'check_output'), (lambda:build.run_command(['xcrun',secret]), 'run')]:
            with patch.object(build.subprocess,target,side_effect=error):
                try: invoke()
                except RuntimeError:
                    rendered=traceback.format_exc()
                    self.assertNotIn(secret,rendered)
                    self.assertIn('output is withheld',rendered)
                else: self.fail('tool error swallowed')

    def test_publish_race_preserves_existing_file_and_cleans_own_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';source.write_bytes(b'new');first=root/'first';existing=root/'existing';existing.write_bytes(b'old')
            with self.assertRaises(FileExistsError): build.publish_files([(source,first),(source,existing)])
            self.assertFalse(first.exists());self.assertEqual(existing.read_bytes(),b'old')


if __name__ == '__main__': unittest.main()
