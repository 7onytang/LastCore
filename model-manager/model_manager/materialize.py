"""把清单物化成本地校验过的 revision。

顺序固定，且每一步失败都以明确错误终止：

1. 启动前能力检查（架构白名单已在解析清单时完成，这里查后端 / dtype / 序列长度）；
2. 磁盘空间检查（清单声明的 ``min_free_disk_bytes`` 与容器大小取较大者）；
3. 取 ``(model_id, revision)`` 锁，串行化同模型并发下载，不同 revision 互不阻塞；
4. 逐个 artifact：成品已存在且哈希一致则跳过（可续传、并发只取一次），否则流式写入
   同目录临时文件，边写边算 sha256，大小或哈希不符即丢弃；
5. 全部就绪后写清单副本，再写完成标记（最后落盘）；
6. 重新全量校验一次，只有校验通过才返回。

任何一步失败都不会留下可被误当作成品的半成品：要么拿到 ``VerifiedModel``，要么抛错。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from inference_engine.stage import segment_plan

from .cache import VerifiedModel, ModelCache, sha256_file
from .cost import cost_report, disk_requirement, segment_cost
from .errors import IntegrityError
from .manifest import ModelManifest, check_backend_capability
from .sources import ArtifactSource


@dataclass(frozen=True)
class MaterializeResult:
    verified: VerifiedModel
    fetched: tuple[str, ...]
    reused: tuple[str, ...]
    bytes_written: int
    backend: str | None = None
    dtype: str | None = None

    def as_dict(self) -> dict:
        manifest = self.verified.manifest
        report = cost_report(manifest)
        return {
            "model_id": manifest.model_id,
            "revision": manifest.revision,
            "architecture": manifest.architecture,
            "backend": self.backend,
            "dtype": self.dtype or manifest.dtype,
            "cache_root": str(self.verified.root),
            "fetched": list(self.fetched),
            "reused": list(self.reused),
            "bytes_written": self.bytes_written,
            "license": manifest.license.to_payload(),
            "resources": manifest.resources.to_payload(),
            "cost": report.as_dict(),
            "artifacts": [item.as_dict() for item in self.verified.artifacts],
        }


def materialize(manifest: ModelManifest, source: ArtifactSource, cache: ModelCache, *,
                backend: str | None = None, dtype: str | None = None,
                force: bool = False) -> MaterializeResult:
    check_backend_capability(manifest, backend, dtype)
    cache.check_disk(disk_requirement(manifest))
    target = cache.revision_dir(manifest.model_id, manifest.revision)
    fetched: list[str] = []
    reused: list[str] = []
    written = 0
    with cache.lock(manifest.model_id, manifest.revision):
        cache.cleanup_partials(manifest.model_id, manifest.revision)
        if not force and cache.is_complete(manifest.model_id, manifest.revision):
            try:
                verified = cache.verify(manifest)
            except IntegrityError:
                verified = None
            if verified is not None:
                return MaterializeResult(verified=verified, fetched=(), reused=tuple(
                    artifact.path for artifact in manifest.artifacts), bytes_written=0,
                    backend=backend, dtype=dtype or manifest.dtype)
        for artifact in manifest.artifacts:
            destination = cache.artifact_path(manifest, artifact)
            if not force and destination.is_file():
                digest, size = sha256_file(destination)
                if digest == artifact.sha256 and size == artifact.size_bytes:
                    reused.append(artifact.path)
                    continue
            stream = source.open(artifact)
            try:
                cache.put_stream(target, artifact, stream)
            finally:
                stream.close()
            fetched.append(artifact.path)
            written += artifact.size_bytes
        cache.finalize(target, manifest)
    verified = cache.verify(manifest)
    return MaterializeResult(verified=verified, fetched=tuple(fetched), reused=tuple(reused),
                             bytes_written=written, backend=backend,
                             dtype=dtype or manifest.dtype)


def verify_materialized(manifest: ModelManifest, cache: ModelCache, *,
                        backend: str | None = None, num_stages: int = 1) -> dict:
    """只校验本地已物化的 revision，并回报许可、资源与分片成本。"""
    check_backend_capability(manifest, backend, None)
    verified = cache.verify(manifest)
    report = cost_report(manifest)
    layout = manifest.layout
    return {"model_id": manifest.model_id, "revision": manifest.revision,
            "cache_root": str(verified.root), "backend": backend,
            "license": manifest.license.to_payload(),
            "resources": manifest.resources.to_payload(), "cost": report.as_dict(),
            "embedding_bytes": layout.embedding_bytes, "head_bytes": layout.head_bytes,
            "segments": segment_costs(manifest, num_stages),
            "artifacts": [item.as_dict() for item in verified.artifacts]}


def summary(manifest: ModelManifest, *, backend: str | None = None, num_stages: int = 1) -> dict:
    """不接触磁盘的清单摘要：成本、许可、后端与分片容量。"""
    check_backend_capability(manifest, backend, None)
    return {"model_id": manifest.model_id, "revision": manifest.revision,
            "architecture": manifest.architecture, "dtype": manifest.dtype,
            "backend": backend, "license": manifest.license.to_payload(),
            "resources": manifest.resources.to_payload(),
            "cost": cost_report(manifest).as_dict(),
            "backends": {name: backend_.to_payload() for name, backend_ in manifest.backends.items()},
            "segments": segment_costs(manifest, num_stages)}


def segment_costs(manifest: ModelManifest, num_stages: int = 1) -> list[dict]:
    """按引擎的均分切层方案给出每一段的权重与运行时内存估算，供调度侧估容量。"""
    return [segment_cost(manifest, start, end).as_dict()
            for start, end in segment_plan(manifest.n_layers, num_stages)]


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
