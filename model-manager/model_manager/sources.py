"""产物源：把清单里的 artifact 变成字节流。

源只提供字节，不提供可执行内容：model-manager 从不 import、eval 或执行取回的
任何东西，只对其做哈希与落盘。

* :class:`LocalArtifactSource` 从本地目录（例如模型仓库的已导出目录）读取，
  路径同样经过包含性检查，符号链接不能逃出根目录。
* :class:`HttpArtifactSource` 只接受 ``http`` / ``https``，并且必须由调用方显式
  传入 ``authorized=True`` 才能构造——本轮没有存储与下载金额授权，默认 fail closed，
  避免任何未经批准的下载。
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from pathlib import Path
from typing import BinaryIO, Callable, Protocol, runtime_checkable

from .cache import resolve_within
from .errors import SourceError
from .manifest import Artifact

ALLOWED_HTTP_SCHEMES = ("http", "https")


@runtime_checkable
class ArtifactSource(Protocol):
    def open(self, artifact: Artifact) -> BinaryIO:  # pragma: no cover - 协议声明
        ...

    def close(self) -> None:  # pragma: no cover - 协议声明
        ...


class LocalArtifactSource:
    """从本地目录按 ``source_path``（缺省时用 ``path``）读取 artifact。"""

    def __init__(self, root: str | Path, *, on_fetch: Callable[[Artifact], None] | None = None):
        self.root = Path(root)
        self.on_fetch = on_fetch
        if not self.root.is_dir():
            raise SourceError(f"local artifact source {self.root} is not a directory")

    def relative_path(self, artifact: Artifact) -> str:
        return artifact.source_path or artifact.path

    def open(self, artifact: Artifact) -> BinaryIO:
        relative = self.relative_path(artifact)
        try:
            path = resolve_within(self.root, relative, label="source_path")
        except Exception as exc:  # noqa: BLE001 - 统一转成 SourceError 上报
            raise SourceError(f"unsafe source path {relative!r}: {exc}") from exc
        if not path.is_file():
            raise SourceError(f"source artifact {relative!r} is missing at {path}")
        if self.on_fetch is not None:
            self.on_fetch(artifact)
        return path.open("rb")

    def close(self) -> None:
        return None

    def __repr__(self) -> str:  # pragma: no cover - 便于排错
        return f"LocalArtifactSource({self.root})"


class HttpArtifactSource:
    """从 HTTP(S) 基地址按 artifact 相对路径下载。默认未授权，必须显式开启。"""

    def __init__(self, base_url: str, *, authorized: bool = False, timeout: float = 30.0,
                 headers: dict[str, str] | None = None,
                 on_fetch: Callable[[Artifact], None] | None = None):
        scheme = urllib.parse.urlsplit(base_url).scheme.lower()
        if scheme not in ALLOWED_HTTP_SCHEMES:
            raise SourceError(f"url scheme {scheme!r} is not allowed; use one of "
                              f"{list(ALLOWED_HTTP_SCHEMES)}")
        if not authorized:
            raise SourceError(
                "http artifact source requires authorized=True; this run has no download "
                "budget authorisation, so remote fetching fails closed")
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        self.headers = dict(headers or {})
        self.on_fetch = on_fetch

    def url_for(self, artifact: Artifact) -> str:
        relative = artifact.source_path or artifact.path
        return urllib.parse.urljoin(self.base_url, urllib.parse.quote(relative))

    def open(self, artifact: Artifact) -> BinaryIO:
        url = self.url_for(artifact)
        if self.on_fetch is not None:
            self.on_fetch(artifact)
        request = urllib.request.Request(url, headers={"User-Agent": "lastcore-model-manager/1",
                                                       **self.headers})
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except OSError as exc:
            raise SourceError(f"cannot fetch {url}: {exc}") from exc

    def close(self) -> None:
        return None

    def __repr__(self) -> str:  # pragma: no cover - 便于排错
        return f"HttpArtifactSource({self.base_url})"


def fetch_bytes(source: ArtifactSource, artifact: Artifact) -> bytes:
    """小文件取值助手（tokenizer 等）；大文件仍走流式落盘。"""
    stream = source.open(artifact)
    try:
        return stream.read()
    finally:
        stream.close()
