import io
import os
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest

from model_manager.cache import ModelCache, resolve_within, safe_component, safe_model_path
from model_manager.errors import (IntegrityError, InsufficientDiskError, LockTimeoutError,
                                  PathTraversalError, SourceError)
from model_manager.materialize import materialize
from model_manager.sources import LocalArtifactSource
from tests.helpers import FlakyStream, OversizedStream, TamperedStream, build_assets

MODULE_DIR = Path(__file__).resolve().parents[1]
Usage = namedtuple("Usage", "total used free")


class InterruptingSource:
    """在写入某个 artifact 的过程中抛错，模拟进程中断。"""

    def __init__(self, assets, *, limit: int, role: str = "checkpoint"):
        self.assets = Path(assets)
        self.limit = limit
        self.role = role

    def open(self, artifact):
        data = (self.assets / (artifact.source_path or artifact.path)).read_bytes()
        if artifact.role == self.role:
            return FlakyStream(data, self.limit)
        return io.BytesIO(data)

    def close(self):
        return None


class TamperedSource:
    """内容被改写但大小不变，用来验证哈希校验。"""

    def __init__(self, assets):
        self.assets = Path(assets)

    def open(self, artifact):
        data = (self.assets / (artifact.source_path or artifact.path)).read_bytes()
        return TamperedStream(data)

    def close(self):
        return None


class ShortSource:
    """比声明的 size_bytes 更短，用来验证大小校验。"""

    def __init__(self, assets):
        self.assets = Path(assets)

    def open(self, artifact):
        data = (self.assets / (artifact.source_path or artifact.path)).read_bytes()
        return io.BytesIO(data[:-10])

    def close(self):
        return None


class OversizedSource:
    def __init__(self, assets):
        self.assets = Path(assets)

    def open(self, artifact):
        data = (self.assets / (artifact.source_path or artifact.path)).read_bytes()
        return OversizedStream(data + b"trailing-bytes")

    def close(self):
        return None


class CountingSource(LocalArtifactSource):
    def __init__(self, root, counter, lock):
        super().__init__(root)
        self.counter = counter
        self.counter_lock = lock

    def open(self, artifact):
        with self.counter_lock:
            self.counter.append(artifact.path)
        time.sleep(0.05)
        return super().open(artifact)


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "assets"
    manifest = build_assets(root)
    return root, manifest


def test_materialize_writes_an_atomic_verified_revision(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    result = materialize(manifest, LocalArtifactSource(root), cache)
    assert set(result.fetched) == {"tiny.npz", "tokenizer.json"}
    assert result.reused == ()
    assert result.bytes_written == manifest.resources.container_bytes
    assert cache.is_complete(manifest.model_id, manifest.revision)
    verified = result.verified
    assert verified.root == cache.revision_dir(manifest.model_id, manifest.revision)
    assert verified.checkpoint == verified.root / "tiny.npz"
    assert verified.path_for("tokenizer") == verified.root / "tokenizer.json"
    assert verified.path_of("tiny.npz").is_file()
    assert cache.partials(manifest.model_id, manifest.revision) == []


def test_model_id_with_slash_is_stored_in_nested_directories(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    materialize(manifest, LocalArtifactSource(root), cache)
    expected = tmp_path / "cache" / "models" / "lastcore" / "tiny-cpu" / manifest.revision
    assert expected.is_dir()
    assert cache.revision_dir(manifest.model_id, manifest.revision) == expected
    assert cache.lock_path(manifest.model_id, manifest.revision).name.startswith("lastcore__tiny-cpu__")


@pytest.mark.parametrize("value", ["../../etc", "a/../../b", "", ".", "..", "a b", "a/../.."])
def test_unsafe_model_paths_are_rejected(value):
    with pytest.raises(PathTraversalError):
        safe_model_path(value)


@pytest.mark.parametrize("value,expected", [
    ("m", "m"),
    ("lastcore/tiny-cpu", "lastcore/tiny-cpu"),
    ("a/b/c", "a/b/c"),
    ("/leading/slash/", "leading/slash"),
    ("a//b", "a/b"),
    ("back\\slash/model", "back/slash/model"),
])
def test_model_paths_with_slashes_are_accepted(value, expected):
    assert safe_model_path(value) == expected


@pytest.mark.parametrize("value", ["a/b", "..", ".", "", "a b", "a\x00b"])
def test_unsafe_directory_components_are_rejected(value):
    with pytest.raises(PathTraversalError):
        safe_component(value, "revision")


def test_second_materialize_reuses_everything(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    materialize(manifest, LocalArtifactSource(root), cache)
    second = materialize(manifest, LocalArtifactSource(root), cache)
    assert second.fetched == ()
    assert set(second.reused) == {"tiny.npz", "tokenizer.json"}
    assert second.bytes_written == 0


def test_interrupted_write_leaves_no_partial_and_no_complete_marker(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    source = InterruptingSource(root, limit=manifest.artifacts[0].size_bytes // 2)
    with pytest.raises(SourceError) as excinfo:
        materialize(manifest, source, cache)
    assert "simulated interrupted read" in str(excinfo.value)
    assert not cache.is_complete(manifest.model_id, manifest.revision)
    assert cache.partials(manifest.model_id, manifest.revision) == []
    assert not (cache.revision_dir(manifest.model_id, manifest.revision) / "tiny.npz").exists()
    with pytest.raises(IntegrityError):
        cache.verify(manifest)


def test_interrupted_materialize_can_be_resumed(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    with pytest.raises(SourceError):
        materialize(manifest, InterruptingSource(root, limit=100), cache)
    result = materialize(manifest, LocalArtifactSource(root), cache)
    assert result.verified.checkpoint.is_file()
    assert cache.is_complete(manifest.model_id, manifest.revision)


def test_tampered_bytes_of_the_declared_size_are_rejected_by_hash(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    with pytest.raises(IntegrityError) as excinfo:
        materialize(manifest, TamperedSource(root), cache)
    assert "sha256" in str(excinfo.value)
    assert not cache.is_complete(manifest.model_id, manifest.revision)
    assert cache.partials(manifest.model_id, manifest.revision) == []


def test_short_source_is_rejected_by_size(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    with pytest.raises(IntegrityError) as excinfo:
        materialize(manifest, ShortSource(root), cache)
    assert "but the manifest declares" in str(excinfo.value)
    assert cache.partials(manifest.model_id, manifest.revision) == []


def test_oversized_source_is_rejected(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    with pytest.raises(IntegrityError) as excinfo:
        materialize(manifest, OversizedSource(root), cache)
    assert "more than the declared" in str(excinfo.value)
    assert cache.partials(manifest.model_id, manifest.revision) == []


def test_corrupted_artifact_is_detected_and_repaired(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    result = materialize(manifest, LocalArtifactSource(root), cache)
    checkpoint = result.verified.checkpoint
    data = bytearray(checkpoint.read_bytes())
    data[10] ^= 0xFF
    checkpoint.write_bytes(bytes(data))
    with pytest.raises(IntegrityError) as excinfo:
        cache.verify(manifest)
    assert "sha256" in str(excinfo.value)
    repaired = materialize(manifest, LocalArtifactSource(root), cache)
    assert repaired.fetched == ("tiny.npz",)
    assert repaired.verified.checkpoint.read_bytes() == (root / "tiny.npz").read_bytes()


def test_missing_artifact_is_reported(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    result = materialize(manifest, LocalArtifactSource(root), cache)
    (result.verified.root / "tokenizer.json").unlink()
    with pytest.raises(IntegrityError) as excinfo:
        cache.verify(manifest)
    assert "missing" in str(excinfo.value)


def test_truncated_cached_file_is_reported(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    result = materialize(manifest, LocalArtifactSource(root), cache)
    checkpoint = result.verified.checkpoint
    checkpoint.write_bytes(checkpoint.read_bytes()[:100])
    with pytest.raises(IntegrityError) as excinfo:
        cache.verify(manifest)
    assert "bytes but the manifest declares" in str(excinfo.value)


def test_unfinished_revision_is_reported(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    materialize(manifest, LocalArtifactSource(root), cache)
    (cache.revision_dir(manifest.model_id, manifest.revision) / ".complete").unlink()
    with pytest.raises(IntegrityError) as excinfo:
        cache.verify(manifest)
    assert "no complete marker" in str(excinfo.value)


def test_verify_before_materialize_is_reported(assets, tmp_path):
    _, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    with pytest.raises(IntegrityError) as excinfo:
        cache.verify(manifest)
    assert "not materialized" in str(excinfo.value)


def test_manifest_drift_is_detected(tmp_path):
    first_assets = tmp_path / "first"
    manifest_a = build_assets(first_assets, revision="shared")
    cache = ModelCache(tmp_path / "cache")
    materialize(manifest_a, LocalArtifactSource(first_assets), cache)

    other_assets = tmp_path / "other"
    manifest_b = build_assets(other_assets, revision="shared", include_tokenizer=False)
    assert manifest_b.resources.container_bytes != manifest_a.resources.container_bytes
    assert manifest_b.artifacts != manifest_a.artifacts
    with pytest.raises(IntegrityError) as excinfo:
        cache.verify(manifest_b)
    assert "different manifest" in str(excinfo.value)
    repaired = materialize(manifest_b, LocalArtifactSource(other_assets), cache, force=True)
    assert repaired.verified.manifest.revision == "shared"


def test_symlinked_artifact_escaping_the_cache_is_rejected(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    result = materialize(manifest, LocalArtifactSource(root), cache)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.npz").write_bytes(b"not a checkpoint")
    link = result.verified.checkpoint
    link.unlink()
    os.symlink(outside / "secret.npz", link)
    with pytest.raises(PathTraversalError):
        cache.verify(manifest)


@pytest.mark.parametrize("relative", ["../x", "/etc/passwd", "a/../../b", "C:/x"])
def test_resolve_within_rejects_escapes(tmp_path, relative):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathTraversalError):
        resolve_within(root, relative)


def test_resolve_within_accepts_nested_paths(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert resolve_within(root, "a/b.bin") == (root.resolve() / "a/b.bin")


def test_insufficient_disk_is_reported_before_any_write(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache", disk_usage=lambda _root: Usage(1000, 990, 10))
    with pytest.raises(InsufficientDiskError) as excinfo:
        materialize(manifest, LocalArtifactSource(root), cache)
    assert excinfo.value.available_bytes == 10
    assert excinfo.value.required_bytes >= manifest.resources.container_bytes
    assert not cache.revision_dir(manifest.model_id, manifest.revision).exists()


def test_available_bytes_is_reported(assets, tmp_path):
    _, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    assert cache.available_bytes() > manifest.resources.container_bytes


def test_concurrent_materialize_of_the_same_revision_fetches_once(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    counter: list[str] = []
    guard = threading.Lock()
    barrier = threading.Barrier(4)
    results = []
    errors = []

    def worker():
        source = CountingSource(root, counter, guard)
        try:
            barrier.wait(timeout=10)
            results.append(materialize(manifest, source, cache))
        except Exception as exc:  # noqa: BLE001 - 收集后统一断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert len(results) == 4
    assert sorted(counter) == ["tiny.npz", "tokenizer.json"]
    assert cache.verify(manifest).checkpoint.is_file()


def test_lock_is_held_across_processes(tmp_path):
    cache = ModelCache(tmp_path / "cache", lock_timeout=0.4, poll_interval=0.02)
    script = (
        "import sys, time\n"
        "sys.path[:0] = ['.', '../inference-engine', '../communication']\n"
        "from model_manager.cache import ModelCache\n"
        "c = ModelCache(sys.argv[1])\n"
        "with c.lock('m', 'r'):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(float(sys.argv[2]))\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script, str(cache.root), "1.2"],
                               cwd=MODULE_DIR, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(LockTimeoutError) as excinfo:
            with cache.lock("m", "r"):
                pass
        assert excinfo.value.holder is not None
        assert excinfo.value.holder.split(":")[0] == str(process.pid)
    finally:
        process.wait(timeout=15)
    with cache.lock("m", "r"):
        pass
    assert not cache.lock_path("m", "r").exists()


def test_stale_lock_from_a_dead_process_is_broken(tmp_path):
    cache = ModelCache(tmp_path / "cache", lock_timeout=1.0, poll_interval=0.02)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    lock_path = cache.lock_path("m", "r")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"{dead.pid}:stale", encoding="utf-8")
    started = time.monotonic()
    with cache.lock("m", "r"):
        pass
    assert time.monotonic() - started < 1.0
    assert not lock_path.exists()


def test_lock_held_by_a_live_pid_is_not_broken(tmp_path):
    cache = ModelCache(tmp_path / "cache", lock_timeout=0.3, poll_interval=0.02)
    lock_path = cache.lock_path("m", "r")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"{os.getpid()}:live", encoding="utf-8")
    with pytest.raises(LockTimeoutError):
        with cache.lock("m", "r"):
            pass
    assert lock_path.exists()
    lock_path.unlink()


def test_remove_revision_and_cleanup_partials(assets, tmp_path):
    root, manifest = assets
    cache = ModelCache(tmp_path / "cache")
    result = materialize(manifest, LocalArtifactSource(root), cache)
    stray = result.verified.root / "junk.part-123-abc"
    stray.write_bytes(b"partial")
    assert cache.partials(manifest.model_id, manifest.revision) == [stray]
    assert cache.cleanup_partials(manifest.model_id, manifest.revision) == 1
    assert cache.partials(manifest.model_id, manifest.revision) == []
    assert cache.remove_revision(manifest.model_id, manifest.revision) is True
    assert cache.remove_revision(manifest.model_id, manifest.revision) is False
