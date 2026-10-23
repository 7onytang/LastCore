"""Synthetic tar/temp fixtures only; download and subprocess are always mocked."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap
import source_cache


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='lastcore-source-test-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cache = self.root / '.cache'
        self.cache.mkdir()
        self.source = self.cache / 'backend.tar.gz'
        self.backend = self.cache / 'backend'
        self.commit = 'f' * 40
        self.prefix = 'llama.cpp-' + self.commit
        self.model = b'fake model fixture, never executed'
        self._archive()
        cmake = self.root / '.venv/bin/cmake'
        cmake.parent.mkdir(parents=True)
        cmake.write_bytes(b'not an executable; subprocess is mocked')
        self.receipt = self.cache / 'installed.json'
        self.prior_receipt = b'previous installed receipt must remain unchanged\n'
        self.receipt.write_bytes(self.prior_receipt)
        self.patcher = patch.multiple(bootstrap, ROOT=self.root, CACHE=self.cache,
            COMMIT=self.commit, SOURCE_SHA256=self.digest,
            MODEL_SHA256=hashlib.sha256(self.model).hexdigest(),
            SOURCE_URL='https://fixture.invalid/source', MODEL_URL='https://fixture.invalid/model')
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.downloads = []
        def download(url, path, digest, maximum):
            self.downloads.append(url)
            if url == bootstrap.MODEL_URL:
                path.write_bytes(self.model)
            else:
                self.assertEqual(url, bootstrap.SOURCE_URL)
                self.assertEqual(path, self.source)
        self.mock_download = patch.object(bootstrap, 'download', side_effect=download).start()
        self.addCleanup(patch.stopall)
        def build(argv, check):
            self.assertTrue(check)
            if '--build' in argv:
                executable = self.backend / 'build/bin/llama-completion'
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_bytes(b'fake executable bytes, never launched')
        self.mock_build = patch.object(bootstrap.subprocess, 'run', side_effect=build).start()
        self.no_network = patch.object(bootstrap.urllib.request, 'urlopen',
            side_effect=AssertionError('no real download is allowed')).start()

    def _archive(self, entries=None):
        # Entries are explicit headers so dangerous types can be tested without extracting them.
        if entries is None:
            entries = [(self.prefix, None), (self.prefix + '/src', None),
                       (self.prefix + '/CMakeLists.txt', b'project(fake)\n'),
                       (self.prefix + '/src/core.c', b'fake source\n')]
        with tarfile.open(self.source, 'w:gz') as archive:
            for entry in entries:
                if isinstance(entry, tarfile.TarInfo):
                    archive.addfile(entry)
                    continue
                name, data = entry
                member = tarfile.TarInfo(name)
                member.mode = 0o755 if data is None else 0o644
                if data is None:
                    member.type = tarfile.DIRTYPE
                    archive.addfile(member)
                else:
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        if hasattr(self, 'patcher'):
            bootstrap.SOURCE_SHA256 = self.digest

    def _bind(self):
        source_cache.ensure_source_tree(self.source, self.backend, self.commit, self.digest)

    def _snapshot(self):
        if not self.backend.exists() and not self.backend.is_symlink():
            return None
        if self.backend.is_symlink():
            return ('link', str(self.backend.readlink()))
        result = {}
        def visit(folder):
            for path in folder.iterdir():
                name = path.relative_to(self.backend).as_posix()
                if path.is_symlink(): result[name] = ('link', str(path.readlink()))
                elif path.is_dir(): result[name] = ('dir',); visit(path)
                else: result[name] = ('file', path.read_bytes())
        visit(self.backend)
        return result

    def _reject(self, exception=source_cache.SourceBindingError):
        before = self._snapshot()
        with self.assertRaises(exception):
            bootstrap.main()
        self.assertEqual(self._snapshot(), before)
        self.mock_build.assert_not_called()
        self.assertEqual(self.receipt.read_bytes(), self.prior_receipt)
        self.assertNotIn(bootstrap.MODEL_URL, self.downloads)
        self.no_network.assert_not_called()

    def test_first_install_and_reuse_validate_source_before_mocked_build(self):
        with patch('builtins.print'):
            bootstrap.main()
        manifest = json.loads(self.receipt.read_text())
        self.assertEqual(manifest['backend_commit'], self.commit)
        self.assertEqual(manifest['source_sha256'], self.digest)
        self.assertEqual(self.mock_build.call_count, 2)
        self.assertEqual((self.backend / 'src/core.c').read_bytes(), b'fake source\n')
        marker = (self.backend / source_cache.BINDING_FILE).read_bytes()
        before = (self.backend / 'src/core.c').stat().st_mtime_ns
        # Generated build output is allowed and is not claimed as immutable source.
        (self.backend / 'build/CMakeCache.txt').write_text('trusted local generated state')
        (self.backend / 'build/bin/libfixture.so').symlink_to('llama-completion')
        self.mock_build.reset_mock()
        with patch('builtins.print'):
            bootstrap.main()
        self.assertEqual(self.mock_build.call_count, 2)
        self.assertEqual((self.backend / source_cache.BINDING_FILE).read_bytes(), marker)
        self.assertEqual((self.backend / 'src/core.c').stat().st_mtime_ns, before)

    def test_unbound_empty_cache_rejects_before_any_download(self):
        self.backend.mkdir()
        self._reject()
        self.mock_download.assert_not_called()

    def test_unbound_partial_cache_rejects_before_any_download(self):
        self.backend.mkdir()
        (self.backend / 'partial.c').write_bytes(b'preserve this old tree')
        self._reject()
        self.mock_download.assert_not_called()

    def test_changed_source_cannot_be_certified_by_a_valid_completion_marker(self):
        self._bind()
        (self.backend / 'src/core.c').write_bytes(b'evil source\n')
        self._reject()

    def test_missing_source_cannot_be_certified_by_a_valid_completion_marker(self):
        self._bind()
        (self.backend / 'src/core.c').unlink()
        self._reject()

    def test_unexpected_source_file_is_not_ignored(self):
        self._bind()
        (self.backend / 'CMakeCache.txt').write_text('not under the designated build directory')
        self._reject()

    def test_unexpected_empty_source_directory_is_not_ignored(self):
        self._bind()
        (self.backend / 'unexpected').mkdir()
        self._reject()

    def test_file_link_is_rejected_even_when_its_target_bytes_match(self):
        self._bind()
        target = self.root / 'outside.c'
        target.write_bytes(b'fake source\n')
        path = self.backend / 'src/core.c'
        path.unlink(); path.symlink_to(target)
        self._reject()
        self.assertEqual(target.read_bytes(), b'fake source\n')

    def test_directory_link_is_rejected_without_following_it(self):
        self._bind()
        target = self.root / 'outside-dir'
        (self.backend / 'src').rename(target)
        (self.backend / 'src').symlink_to(target, target_is_directory=True)
        self._reject()
        self.assertEqual((target / 'core.c').read_bytes(), b'fake source\n')

    def test_backend_root_link_is_rejected_before_any_download(self):
        self._bind()
        target = self.root / 'old-backend'
        self.backend.rename(target)
        self.backend.symlink_to(target, target_is_directory=True)
        self._reject()
        self.mock_download.assert_not_called()
        self.assertTrue((target / source_cache.BINDING_FILE).is_file())

    def test_build_root_link_is_not_a_trusted_generated_directory(self):
        self._bind()
        target = self.root / 'outside-build'; target.mkdir()
        (target / 'sentinel').write_text('preserve')
        (self.backend / 'build').symlink_to(target, target_is_directory=True)
        self._reject()
        self.assertEqual((target / 'sentinel').read_text(), 'preserve')

    def test_bad_completion_binding_rejects_before_any_download(self):
        self._bind()
        (self.backend / source_cache.BINDING_FILE).write_text('{"version":1,"backend_commit":"other"}')
        self._reject()
        self.mock_download.assert_not_called()

    def test_linked_completion_binding_is_rejected(self):
        self._bind()
        path = self.backend / source_cache.BINDING_FILE
        target = self.root / 'marker'; path.rename(target); path.symlink_to(target)
        self._reject()
        self.mock_download.assert_not_called()

    def test_downloader_cannot_bypass_archive_checksum_verification(self):
        self._bind()
        self.source.write_bytes(self.source.read_bytes() + b'changed archive')
        self._reject()

    def test_linked_archive_is_rejected_even_with_matching_hash(self):
        target = self.root / 'source.tar.gz'; self.source.rename(target); self.source.symlink_to(target)
        self._reject()

    def test_unsafe_archive_paths_and_reserved_names_reject_before_extraction(self):
        for name in ('../escape', '/absolute', self.prefix + '/../escape',
                     self.prefix + '/src/../../escape', self.prefix + '/back\\slash',
                     self.prefix + '/C:/escape', 'another-root/file',
                     self.prefix + '/build/file', self.prefix + '/' + source_cache.BINDING_FILE):
            with self.subTest(name=name):
                self._archive([(self.prefix, None), (name, b'bad')])
                self._reject()
                self.assertFalse(self.backend.exists())

    def test_links_and_special_archive_members_reject_before_extraction(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo(self.prefix + '/unsafe')
                member.type = kind; member.linkname = '../outside'
                self._archive([(self.prefix, None), member])
                self._reject()
                self.assertFalse(self.backend.exists())

    def test_duplicate_and_conflicting_archive_paths_reject_before_extraction(self):
        for entries in ([(self.prefix + '/same', b'a'), (self.prefix + '/same', b'b')],
                        [(self.prefix + '/parent', b'a'), (self.prefix + '/parent/child', b'b')]):
            with self.subTest(entries=entries):
                self._archive([(self.prefix, None)] + entries)
                self._reject()
                self.assertFalse(self.backend.exists())

    def test_archive_and_unpacked_limits_reject_before_build(self):
        for key in ('MAX_ARCHIVE_BYTES', 'MAX_MEMBER_BYTES', 'MAX_SOURCE_BYTES', 'MAX_MEMBERS'):
            with self.subTest(limit=key), patch.object(source_cache, key, 1):
                self._reject()
                self.assertFalse(self.backend.exists())

    def test_extraction_failure_preserves_unbound_partial_and_previous_receipt(self):
        def partial(archive, destination, **kwargs):
            (destination / 'partial.c').write_bytes(b'partially extracted fixture')
            raise OSError('synthetic extraction failure')
        with patch.object(tarfile.TarFile, 'extractall', partial):
            with self.assertRaisesRegex(OSError, 'synthetic'):
                bootstrap.main()
        self.assertEqual((self.backend / 'partial.c').read_bytes(), b'partially extracted fixture')
        self.assertFalse((self.backend / source_cache.BINDING_FILE).exists())
        self.mock_build.assert_not_called()
        self.assertEqual(self.receipt.read_bytes(), self.prior_receipt)
        self.mock_download.reset_mock(); self.downloads.clear()
        self._reject()
        self.mock_download.assert_not_called()

    def test_rejection_does_not_create_a_new_installed_receipt(self):
        self.receipt.unlink()
        self.backend.mkdir()
        with self.assertRaises(source_cache.SourceBindingError):
            bootstrap.main()
        self.assertFalse(self.receipt.exists())
        self.mock_download.assert_not_called()
        self.mock_build.assert_not_called()

    def test_source_modified_during_mocked_build_is_not_given_a_new_receipt(self):
        build = self.mock_build.side_effect
        def changing_build(argv, check):
            build(argv, check)
            if '--build' in argv:
                (self.backend / 'src/core.c').write_bytes(b'modified by synthetic build')
        self.mock_build.side_effect = changing_build
        with self.assertRaises(source_cache.SourceBindingError):
            bootstrap.main()
        self.assertEqual(self.mock_build.call_count, 2)
        self.assertEqual(self.receipt.read_bytes(), self.prior_receipt)
        self.assertEqual((self.backend / 'src/core.c').read_bytes(), b'modified by synthetic build')

    def test_mocked_build_failure_does_not_update_installed_receipt(self):
        self.mock_build.side_effect = RuntimeError('synthetic build failure')
        with self.assertRaisesRegex(RuntimeError, 'synthetic build failure'):
            bootstrap.main()
        self.assertEqual(self.receipt.read_bytes(), self.prior_receipt)
        self.assertTrue((self.backend / source_cache.BINDING_FILE).is_file())


if __name__ == '__main__':
    unittest.main()
