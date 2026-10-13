"""内容寻址的本地模型缓存：原子落盘、哈希校验、磁盘检查与并发锁。

设计要点：

* **原子性**：每个 artifact 先写同目录下的 ``*.part-<pid>-<uuid>``，边写边算 sha256，
  校验大小与哈希后 ``os.fsync`` 再 ``os.replace`` 覆盖到最终路径；目录也 ``fsync``。
  进程中断最多留下 ``*.part-*`` 临时文件，最终路径要么是上一个完整版本，要么不存在。
* **完成标记**：所有 artifact 就绪后，先原子写入清单副本，再原子写入 ``.complete``
  标记（内容含清单规范形式的 sha256）。没有完成标记的 revision 一律视为未落盘完成，
  ``verify`` 会明确报错而不是把半成品当成品。
* **并发**：同一 ``(model_id, revision)`` 用 ``O_CREAT|O_EXCL`` 锁文件串行化，
  不同 revision 互不阻塞；持锁进程消失时按 pid 判活清理陈旧锁。
* **路径安全**：清单里的相对路径做规范化 + 目标目录包含性检查（含符号链接逃逸）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping

from .errors import (IntegrityError, InsufficientDiskError, LockTimeoutError,
                     ModelManagerError, PathTraversalError, SourceError)
from .manifest import Artifact, ModelManifest, normalize_artifact_path

COMPLETE_MARKER = ".complete"
MANIFEST_COPY = "model-manifest.json"
PART_PREFIX = ".part-"
DEFAULT_LOCK_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 0.05
DEFAULT_STALE_AFTER = 120.0
_HASH_BUFFER = 1 << 20
# Windows 下 os.open 缺省为文本模式，会把写入的 0x0A 翻译为 0x0D 0x0A 导致
# 二进制缓存膨胀；POSIX 无 O_BINARY，用 getattr 回退为 0（无操作）。
_O_BINARY = getattr(os, "O_BINARY", 0)


def safe_component(value: object, where: str) -> str:
    """目录名片段：只允许字母数字与 ``._-``，拒绝分隔符与 ``..``。"""
    if not isinstance(value, str) or not value:
        raise PathTraversalError(value, f"{where} must be a non-empty string")
    if value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise PathTraversalError(value, f"{where} must not contain path separators or '..'")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not set(value) <= allowed:
        raise PathTraversalError(value, f"{where} must use only [A-Za-z0-9._-] characters")
    return value


def safe_model_path(model_id: object) -> str:
    """模型标识可以带 ``/``（如 ``lastcore/tiny-cpu``），但每一段都必须是安全目录名。"""
    if not isinstance(model_id, str) or not model_id.strip():
        raise PathTraversalError(model_id, "model_id must be a non-empty string")
    normalized = model_id.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        raise PathTraversalError(model_id, "model_id must not be empty")
    return "/".join(safe_component(part, "model_id") for part in parts)


def resolve_within(root: str | Path, relative: str, *, label: str = "artifact") -> Path:
    """把清单里的相对路径解析到 ``root`` 之内，拒绝绝对路径与符号链接逃逸。"""
    base = Path(root)
    normalized = normalize_artifact_path(relative, label)
    try:
        base_resolved = base.resolve(strict=False)
    except OSError as exc:  # pragma: no cover - 依赖文件系统行为
        raise PathTraversalError(relative, f"cannot resolve cache root {base}: {exc}") from exc
    target = base_resolved / normalized
    resolved = target.resolve(strict=False)
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise PathTraversalError(relative, "resolves outside the target directory", root=base)
    return resolved


def sha256_file(path: str | Path, *, chunk: int = _HASH_BUFFER) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - 平台不支持目录 fd 时退化为 best effort
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def _pids_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - 存在但属于他人
        return True
    except OSError:  # pragma: no cover
        return True
    return True


@dataclass(frozen=True)
class VerifiedArtifact:
    relative: str
    role: str
    sha256: str
    size_bytes: int
    path: Path

    def as_dict(self) -> dict:
        return {"path": self.relative, "role": self.role, "sha256": self.sha256,
                "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class VerifiedModel:
    """已完成校验的本地 revision：所有成品路径都在缓存目录内。"""

    manifest: ModelManifest
    root: Path
    artifacts: tuple[VerifiedArtifact, ...]

    def path_for(self, role: str) -> Path:
        matches = [item for item in self.artifacts if item.role == role]
        if not matches:
            raise ModelManagerError(f"revision {self.manifest.revision!r} has no {role!r} artifact")
        if len(matches) > 1:
            raise ModelManagerError(f"revision {self.manifest.revision!r} has {len(matches)} "
                                    f"{role!r} artifacts; request one by path")
        return matches[0].path

    def path_of(self, relative: str) -> Path:
        normalized = normalize_artifact_path(relative)
        for item in self.artifacts:
            if item.relative == normalized:
                return item.path
        raise ModelManagerError(f"revision {self.manifest.revision!r} has no artifact {normalized!r}")

    @property
    def checkpoint(self) -> Path:
        return self.path_for("checkpoint")

    def as_dict(self) -> dict:
        return {"model_id": self.manifest.model_id, "revision": self.manifest.revision,
                "root": str(self.root), "dtype": self.manifest.dtype,
                "artifacts": [item.as_dict() for item in self.artifacts]}


class ModelCache:
    """本地模型缓存根目录。所有写入都是原子的，所有读取都先校验。"""

    def __init__(self, root: str | Path, *, lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 stale_after: float = DEFAULT_STALE_AFTER,
                 disk_usage: Callable[[str | Path], object] | None = None):
        self.root = Path(root)
        self.lock_timeout = float(lock_timeout)
        self.poll_interval = float(poll_interval)
        self.stale_after = float(stale_after)
        self._disk_usage = disk_usage or shutil.disk_usage

    # ---- 路径 ----------------------------------------------------------------

    @property
    def locks_dir(self) -> Path:
        return self.root / "locks"

    def revision_dir(self, model_id: str, revision: str) -> Path:
        return (self.root / "models" / Path(safe_model_path(model_id))
                / safe_component(revision, "revision"))

    def artifact_path(self, manifest: ModelManifest, artifact: Artifact) -> Path:
        return resolve_within(self.revision_dir(manifest.model_id, manifest.revision),
                              artifact.path, label=f"{artifact.role} artifact path")

    def _complete_path(self, model_id: str, revision: str) -> Path:
        return self.revision_dir(model_id, revision) / COMPLETE_MARKER

    def is_complete(self, model_id: str, revision: str) -> bool:
        return self._complete_path(model_id, revision).is_file()

    # ---- 磁盘 ----------------------------------------------------------------

    def check_disk(self, required_bytes: int) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        usage = self._disk_usage(self.root)
        available = getattr(usage, "free", None)
        if available is None:
            available = usage[2]
        if available < required_bytes:
            raise InsufficientDiskError(self.root, required_bytes, int(available))

    def available_bytes(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        usage = self._disk_usage(self.root)
        return int(getattr(usage, "free", usage[2]))

    # ---- 锁 ------------------------------------------------------------------

    def lock_path(self, model_id: str, revision: str) -> Path:
        name = f"{safe_model_path(model_id).replace('/', '__')}__" \
               f"{safe_component(revision, 'revision')}.lock"
        return self.locks_dir / name

    @contextmanager
    def lock(self, model_id: str, revision: str) -> Iterator[Path]:
        path = self.lock_path(model_id, revision)
        path.parent.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}:{uuid.uuid4().hex}"
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if self._break_stale_lock(path):
                    continue
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(path, self.lock_timeout, self._holder(path))
                time.sleep(self.poll_interval)
                continue
            try:
                _write_all(fd, token.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            break
        try:
            yield path
        finally:
            try:
                if path.read_text(encoding="utf-8").strip() == token:
                    path.unlink()
            except OSError:  # pragma: no cover - 已被清理
                pass

    def _holder(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:  # pragma: no cover
            return None

    def _break_stale_lock(self, path: Path) -> bool:
        holder = self._holder(path)
        if holder and ":" in holder:
            pid_text = holder.split(":", 1)[0]
            if pid_text.isdigit() and not _pids_alive(int(pid_text)):
                return self._unlink_if_unchanged(path, holder)
            return False
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:  # pragma: no cover
            return False
        if age > self.stale_after:
            return self._unlink_if_unchanged(path, holder)
        return False

    @staticmethod
    def _unlink_if_unchanged(path: Path, expected: str | None) -> bool:
        current = None
        try:
            current = path.read_text(encoding="utf-8").strip() or None
        except OSError:
            pass
        if current != expected:
            return False
        try:
            path.unlink()
        except OSError:  # pragma: no cover - 竞争失败，交给下一轮重试
            return False
        return True

    # ---- 写入 ----------------------------------------------------------------

    def put_stream(self, revision_dir: str | Path, artifact: Artifact, stream) -> Path:
        """把一个 artifact 原子落盘，返回最终路径；大小或哈希不符则不留成品。"""
        root = Path(revision_dir)
        final = resolve_within(root, artifact.path, label=f"{artifact.role} artifact path")
        final.parent.mkdir(parents=True, exist_ok=True)
        part = final.with_name(f"{final.name}{PART_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}")
        digest = hashlib.sha256()
        written = 0
        try:
            fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o644)
            try:
                while True:
                    try:
                        chunk = stream.read(_HASH_BUFFER)
                    except OSError as exc:
                        raise SourceError(
                            f"failed while reading artifact {artifact.path!r}: {exc}") from exc
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > artifact.size_bytes:
                        raise IntegrityError(
                            f"artifact {artifact.path!r} produced more than the declared "
                            f"{artifact.size_bytes} bytes; refusing to keep the partial file")
                    digest.update(chunk)
                    _write_all(fd, chunk)
                os.fsync(fd)
            finally:
                os.close(fd)
            if written != artifact.size_bytes:
                raise IntegrityError(
                    f"artifact {artifact.path!r} produced {written} bytes but the manifest "
                    f"declares {artifact.size_bytes}")
            actual = digest.hexdigest()
            if actual != artifact.sha256:
                raise IntegrityError(
                    f"artifact {artifact.path!r} sha256 is {actual} but the manifest declares "
                    f"{artifact.sha256}")
            os.replace(part, final)
        finally:
            if part.exists():
                try:
                    part.unlink()
                except OSError:  # pragma: no cover
                    pass
        _fsync_directory(final.parent)
        return final

    def write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(f"{path.name}{PART_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}")
        fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o644)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(part, path)
        _fsync_directory(path.parent)

    def finalize(self, revision_dir: str | Path, manifest: ModelManifest) -> Path:
        """写入清单副本与完成标记；标记必须最后落盘。"""
        root = Path(revision_dir)
        root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(manifest.to_payload(), indent=2, ensure_ascii=False,
                             sort_keys=True).encode("utf-8")
        self.write_atomic(root / MANIFEST_COPY, payload + b"\n")
        marker = {"model_id": manifest.model_id, "revision": manifest.revision,
                  "manifest_sha256": hashlib.sha256(payload).hexdigest(),
                  "artifacts": len(manifest.artifacts)}
        self.write_atomic(root / COMPLETE_MARKER,
                          (json.dumps(marker, sort_keys=True) + "\n").encode("utf-8"))
        return root / COMPLETE_MARKER

    # ---- 校验 ----------------------------------------------------------------

    def marker(self, model_id: str, revision: str) -> Mapping | None:
        path = self._complete_path(model_id, revision)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - 半写状态
            return None

    def verify(self, manifest: ModelManifest, *, require_complete: bool = True) -> VerifiedModel:
        root = self.revision_dir(manifest.model_id, manifest.revision)
        if not root.is_dir():
            raise IntegrityError(f"revision {manifest.model_id}@{manifest.revision} is not "
                                 f"materialized at {root}")
        if require_complete:
            marker = self.marker(manifest.model_id, manifest.revision)
            if marker is None:
                raise IntegrityError(
                    f"revision {manifest.model_id}@{manifest.revision} has no complete marker at "
                    f"{root / COMPLETE_MARKER}; it is incomplete or partially written")
            expected = hashlib.sha256(json.dumps(manifest.to_payload(), indent=2,
                                                 ensure_ascii=False,
                                                 sort_keys=True).encode("utf-8")).hexdigest()
            if marker.get("manifest_sha256") != expected:
                raise IntegrityError(
                    f"revision {manifest.model_id}@{manifest.revision} was materialized from a "
                    f"different manifest (marker={marker.get('manifest_sha256')}, "
                    f"requested={expected}); re-materialize with force=True")
        verified: list[VerifiedArtifact] = []
        for artifact in manifest.artifacts:
            path = self.artifact_path(manifest, artifact)
            if not path.is_file():
                raise IntegrityError(f"artifact {artifact.path!r} is missing at {path}")
            actual_size = path.stat().st_size
            if actual_size != artifact.size_bytes:
                raise IntegrityError(
                    f"artifact {artifact.path!r} is {actual_size} bytes but the manifest declares "
                    f"{artifact.size_bytes}")
            actual_hash, _ = sha256_file(path)
            if actual_hash != artifact.sha256:
                raise IntegrityError(
                    f"artifact {artifact.path!r} sha256 is {actual_hash} but the manifest declares "
                    f"{artifact.sha256}")
            verified.append(VerifiedArtifact(relative=artifact.path, role=artifact.role,
                                             sha256=actual_hash, size_bytes=actual_size, path=path))
        return VerifiedModel(manifest=manifest, root=root, artifacts=tuple(verified))

    # ---- 维护 ----------------------------------------------------------------

    def partials(self, model_id: str, revision: str) -> list[Path]:
        root = self.revision_dir(model_id, revision)
        if not root.is_dir():
            return []
        return sorted(path for path in root.rglob(f"*{PART_PREFIX}*") if path.is_file())

    def cleanup_partials(self, model_id: str, revision: str) -> int:
        removed = 0
        for path in self.partials(model_id, revision):
            try:
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover
                pass
        return removed

    def remove_revision(self, model_id: str, revision: str) -> bool:
        root = self.revision_dir(model_id, revision)
        if not root.is_dir():
            return False
        shutil.rmtree(root)
        return True
