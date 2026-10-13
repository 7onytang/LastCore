"""Real Git archives must bind detached metadata to their own exported bytes."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parent / 'prepare_release.py'
LOCK = 'ops/staging/requirements-linux-lock.txt'


class PrepareReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='lastcore-release-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'repo'
        self.root.mkdir()
        self.output = Path(self.directory.name) / 'artifacts'
        self.lock = self.root / LOCK
        self.lock.parent.mkdir(parents=True)
        self.lock.write_bytes(b'committed-lock\n')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Release Fixture')
        self.git('config', 'user.email', 'release-fixture@example.invalid')
        self.commit()
        spec = importlib.util.spec_from_file_location('prepare_release_under_test', SOURCE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.ROOT = self.root

    def git(self, *arguments):
        return subprocess.run(['git', *arguments], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def run_export(self, after_archive=None):
        original = subprocess.run

        def run(arguments, *args, **kwargs):
            result = original(arguments, *args, **kwargs)
            if arguments[:2] == ['git', 'archive'] and after_archive:
                after_archive()
            return result

        with patch.object(self.module.subprocess, 'run', side_effect=run), \
                patch.object(sys, 'argv', ['prepare_release', '--output-dir', str(self.output)]), \
                redirect_stdout(io.StringIO()) as text:
            self.module.main()
        return json.loads(text.getvalue())

    def assert_bound(self, manifest):
        archive = self.output / manifest['archive']
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), manifest['archive_sha256'])
        self.assertEqual(archive.stat().st_size, manifest['archive_bytes'])
        with tarfile.open(archive, 'r:gz') as bundle:
            member = bundle.getmember(manifest['source_commit'] + '/' + LOCK)
            self.assertTrue(member.isfile())
            with bundle.extractfile(member) as lock:
                archived = lock.read()
        self.assertEqual(hashlib.sha256(archived).hexdigest(), manifest['runtime_lock_sha256'])
        self.assertEqual(json.loads(archive.with_suffix('.manifest.json').read_text()), manifest)
        self.assertEqual(archive.with_suffix('.sha256').read_text(),
                         f"{manifest['archive_sha256']}  {archive.name}\n")
        self.assertEqual(datetime.fromisoformat(manifest['packaged_at']).utcoffset(), timedelta(hours=8))
        self.assertEqual(manifest['scope'], 'prepared-source-only; no deployment')
        return archived

    def test_normal_export_binds_archive_checksum_and_lock(self):
        revision = self.git('rev-parse', 'HEAD')
        manifest = self.run_export()
        self.assertEqual(manifest['source_commit'], revision)
        self.assertEqual(self.assert_bound(manifest), b'committed-lock\n')

    def test_worktree_lock_changes_during_export_do_not_change_manifest(self):
        manifest = self.run_export(lambda: self.lock.write_bytes(b'concurrent-worktree-lock\n'))
        self.assertEqual(self.assert_bound(manifest), b'committed-lock\n')
        self.assertNotEqual(hashlib.sha256(self.lock.read_bytes()).hexdigest(),
                            manifest['runtime_lock_sha256'])

    def test_head_moves_after_archive_still_reports_pinned_revision(self):
        revision = self.git('rev-parse', 'HEAD')

        def move_head():
            self.lock.write_bytes(b'new-head-lock\n')
            self.commit()

        manifest = self.run_export(move_head)
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), revision)
        self.assertEqual(manifest['source_commit'], revision)
        self.assertEqual(self.assert_bound(manifest), b'committed-lock\n')

    def test_hash_uses_exported_bytes_including_git_attributes(self):
        self.lock.write_bytes(b'exported=$Format:%H$\n')
        (self.root / '.gitattributes').write_text(LOCK + ' export-subst\n')
        revision = self.commit()
        manifest = self.run_export()
        self.assertEqual(self.assert_bound(manifest), f'exported={revision}\n'.encode())
        self.assertNotEqual(hashlib.sha256(self.lock.read_bytes()).hexdigest(),
                            manifest['runtime_lock_sha256'])

    def test_untracked_lock_cannot_replace_missing_committed_lock(self):
        self.git('rm', LOCK)
        (self.root / 'placeholder.txt').write_text('fixture\n')
        self.commit()
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_bytes(b'untracked-lock\n')
        with self.assertRaises(KeyError):
            self.run_export()
        self.assertEqual(list(self.output.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'requires a real POSIX symlink fixture')
    def test_symlink_lock_is_rejected_without_following_it(self):
        outside = Path(self.directory.name) / 'outside-lock'
        outside.write_bytes(b'outside-content\n')
        self.lock.unlink()
        self.lock.symlink_to(outside)
        self.commit()
        with self.assertRaisesRegex(ValueError, 'regular file'):
            self.run_export()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(outside.read_bytes(), b'outside-content\n')

    def test_existing_artifacts_are_not_overwritten(self):
        manifest = self.run_export()
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with self.assertRaises(FileExistsError):
            self.run_export()
        self.assertEqual({p.name: p.read_bytes() for p in self.output.iterdir()}, before)
        self.assert_bound(manifest)

    def test_archive_failure_cleans_only_the_exclusively_created_file(self):
        original = subprocess.run

        def fail(arguments, *args, **kwargs):
            if arguments[:2] == ['git', 'archive']:
                kwargs['stdout'].write(b'incomplete-archive')
                raise subprocess.CalledProcessError(1, arguments)
            return original(arguments, *args, **kwargs)

        with patch.object(self.module.subprocess, 'run', side_effect=fail), \
                patch.object(sys, 'argv', ['prepare_release', '--output-dir', str(self.output)]):
            with self.assertRaises(subprocess.CalledProcessError):
                self.module.main()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_dirty_tracked_source_is_rejected_before_artifact_creation(self):
        self.lock.write_bytes(b'dirty-lock\n')
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_export()
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
