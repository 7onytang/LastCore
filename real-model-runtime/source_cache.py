"""Bind reusable backend source files to one verified archive, never to a marker alone."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile

from runtime import sha256

BINDING_FILE = '.lastcore-source-binding.json'
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 100_000


class SourceBindingError(RuntimeError):
    pass


def _binding_bytes(commit, digest):
    return (json.dumps({'version': 1, 'backend_commit': commit, 'source_sha256': digest},
                       sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def check_existing_binding(backend, commit, digest):
    """Reject known unbound caches before any download; never adopt or overwrite them."""
    backend = Path(backend)
    try:
        info = backend.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise SourceBindingError('cached backend must be a real directory, not a link')
    marker = backend / BINDING_FILE
    try:
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
            raise SourceBindingError('cached backend completion binding is not a regular small file')
        if marker.read_bytes() != _binding_bytes(commit, digest):
            raise SourceBindingError('cached backend completion binding does not match pinned source')
    except FileNotFoundError as exc:
        raise SourceBindingError('cached backend is unbound or partial; preserve it and create a fresh cache') from exc
    return True


def _inventory(archive, prefix):
    members = []
    files = {}
    directories = set()
    seen = set()
    total = 0
    for member in archive:
        if len(seen) >= MAX_MEMBERS:
            raise SourceBindingError('source archive has too many members')
        name = member.name.rstrip('/') if member.isdir() else member.name
        parts = name.split('/')
        if (not name or '\\' in name or '\0' in name or len(name) > 4096
                or any(part in ('', '.', '..') or ':' in part for part in parts)
                or parts[0] != prefix):
            raise SourceBindingError('source archive member has an unsafe path or wrong root')
        relative = '/'.join(parts[1:])
        if relative in seen:
            raise SourceBindingError('source archive has a duplicate path')
        seen.add(relative)
        if not member.isdir() and not member.isfile() or member.issparse():
            raise SourceBindingError('source archive permits only ordinary files and directories')
        if not relative:
            if not member.isdir():
                raise SourceBindingError('source archive root must be a directory')
            continue
        if parts[1] in ('build', BINDING_FILE):
            raise SourceBindingError('source archive conflicts with reserved local metadata or build output')
        for parent in PurePosixPath(relative).parents:
            if str(parent) != '.':
                directories.add(str(parent))
        if member.isdir():
            directories.add(relative)
        else:
            if not 0 <= member.size <= MAX_MEMBER_BYTES:
                raise SourceBindingError('source archive member exceeds size limit')
            total += member.size
            if total > MAX_SOURCE_BYTES:
                raise SourceBindingError('source archive exceeds unpacked size limit')
            with archive.extractfile(member) as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            files[relative] = (member.size, digest)
        members.append(member.replace(name=relative, deep=False))
    if not files or files.keys() & directories:
        raise SourceBindingError('source archive is empty or has conflicting file/directory paths')
    return members, files, directories


def _verify_tree(backend, files, directories, *, bound):
    actual_files = set()
    actual_directories = set()
    for folder, dirs, names in os.walk(backend, followlinks=False):
        relative_folder = Path(folder).relative_to(backend)
        for name in list(dirs):
            path = Path(folder) / name
            relative = str((relative_folder / name).as_posix())
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise SourceBindingError('cached source contains a linked or non-directory path')
            if relative == 'build':
                dirs.remove(name)  # Generated build state is not source-provenance evidence.
                continue
            actual_directories.add(relative)
        for name in names:
            path = Path(folder) / name
            relative = str((relative_folder / name).as_posix())
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise SourceBindingError('cached source contains a linked or non-regular file')
            if bound and relative == BINDING_FILE:
                continue
            actual_files.add(relative)
            expected = files.get(relative)
            if expected is None or info.st_size != expected[0] or sha256(path) != expected[1]:
                raise SourceBindingError('cached backend source differs from verified archive')
    if actual_files != files.keys() or actual_directories != directories:
        raise SourceBindingError('cached backend source has missing or unexpected paths')


def ensure_source_tree(source, backend, commit, digest):
    """Verify every archived source file on each reuse; publish completion only after success."""
    source, backend = Path(source), Path(backend)
    bound = check_existing_binding(backend, commit, digest)
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARCHIVE_BYTES:
        raise SourceBindingError('source archive must be a regular bounded file')
    with source.open('rb') as raw:
        # Hash and parse the same open archive rather than trusting a downloader or sidecar.
        if hashlib.file_digest(raw, 'sha256').hexdigest() != digest:
            raise SourceBindingError('source archive checksum mismatch')
        raw.seek(0)
        with tarfile.open(fileobj=raw, mode='r:*') as archive:
            members, files, directories = _inventory(archive, f'llama.cpp-{commit}')
            if not bound:
                # Exclusive creation cannot overwrite an old tree. Interrupted extraction
                # leaves an explicitly unbound tree for manual preservation/recovery.
                backend.mkdir()
                archive.extractall(backend, members=members, filter='data')
            _verify_tree(backend, files, directories, bound=bound)
            if not bound:
                with (backend / BINDING_FILE).open('xb') as marker:
                    marker.write(_binding_bytes(commit, digest))
    return backend
