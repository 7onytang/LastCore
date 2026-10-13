"""model-manager 命令行：check / cost / verify / materialize。

只做本地操作：默认不联网；``materialize --url`` 需要显式 ``--authorized``，
本轮没有下载与存储金额授权，因此不会发起任何未经批准的下载。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .cache import ModelCache
from .cost import cost_report
from .errors import ModelManagerError
from .manifest import ModelManifest
from .materialize import materialize, summary, verify_materialized
from .sources import HttpArtifactSource, LocalArtifactSource

DEFAULT_CACHE = os.environ.get("LASTCORE_MODEL_CACHE", "data/models")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="model_manager",
                                     description="模型 revision、层边界、后端能力与缓存校验")
    parser.add_argument("--cache", default=DEFAULT_CACHE,
                        help=f"缓存根目录（默认 {DEFAULT_CACHE}，也可用 LASTCORE_MODEL_CACHE）")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="只读清单做启动前检查，不接触磁盘")
    check.add_argument("--manifest", required=True)
    check.add_argument("--backend", default=None)
    check.add_argument("--dtype", default=None)
    check.add_argument("--stages", type=int, default=1)

    cost = sub.add_parser("cost", help="打印层 / embedding / head / dtype / 哈希成本")
    cost.add_argument("--manifest", required=True)
    cost.add_argument("--json", action="store_true")

    verify = sub.add_parser("verify", help="校验缓存中已物化的 revision")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--backend", default=None)
    verify.add_argument("--stages", type=int, default=1)

    fetch = sub.add_parser("materialize", help="从源原子落盘并校验")
    fetch.add_argument("--manifest", required=True)
    fetch.add_argument("--source", default=None, help="本地源目录")
    fetch.add_argument("--url", default=None, help="HTTP(S) 基地址")
    fetch.add_argument("--authorized", action="store_true",
                       help="显式声明已获得下载授权；缺少时 HTTP 源拒绝构造")
    fetch.add_argument("--backend", default=None)
    fetch.add_argument("--dtype", default=None)
    fetch.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except ModelManagerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run(args) -> int:
    manifest = ModelManifest.load(args.manifest)
    if args.command == "check":
        print(json.dumps(summary(manifest, backend=args.backend, num_stages=args.stages),
                         indent=2, ensure_ascii=False))
        return 0
    if args.command == "cost":
        report = cost_report(manifest)
        if args.json:
            print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
        else:
            _print_cost(report)
        return 0
    cache = ModelCache(args.cache)
    if args.command == "verify":
        print(json.dumps(verify_materialized(manifest, cache, backend=args.backend,
                                             num_stages=args.stages),
                         indent=2, ensure_ascii=False))
        return 0
    if args.command == "materialize":
        source = _source(args)
        result = materialize(manifest, source, cache, backend=args.backend, dtype=args.dtype,
                             force=args.force)
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return 0
    raise ValueError(f"unknown command {args.command!r}")


def _source(args):
    if args.url:
        return HttpArtifactSource(args.url, authorized=args.authorized)
    if args.source:
        return LocalArtifactSource(Path(args.source))
    raise ValueError("materialize needs --source <dir> or --url <base> [--authorized]")


def _print_cost(report) -> None:
    print(f"model: {report.model_id}@{report.revision}  arch={report.architecture}  "
          f"dtype={report.dtype} ({report.dtype_bytes} B/element)")
    print(f"tensors: {report.n_tensors}   layers: {report.n_layers}")
    print(f"embedding: {report.embedding_bytes} B")
    for layer, size in enumerate(report.per_layer_bytes):
        print(f"  layer {layer}: {size} B")
    print(f"layers total: {report.layers_bytes} B")
    print(f"head: {report.head_bytes} B")
    print(f"parameters: {report.parameter_bytes} B   container: {report.container_bytes} B "
          f"(overhead {report.container_overhead_bytes} B)")
    print(f"runtime memory: {report.runtime_memory_bytes} B   "
          f"min free disk: {report.min_free_disk_bytes} B")
    print(f"hash: sha256 ({report.sha256_hex_chars} hex chars per artifact)")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
