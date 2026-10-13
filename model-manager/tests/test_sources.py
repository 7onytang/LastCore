import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from model_manager.errors import SourceError
from model_manager.manifest import Artifact
from model_manager.materialize import materialize
from model_manager.cache import ModelCache
from model_manager.sources import HttpArtifactSource, LocalArtifactSource, fetch_bytes
from tests.helpers import build_assets


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # pragma: no cover - 测试里不需要访问日志
        return


def serve_directory(directory):
    handler = partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}/"


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "assets"
    manifest = build_assets(root)
    return root, manifest


def test_local_source_reads_the_artifact(assets):
    root, manifest = assets
    source = LocalArtifactSource(root)
    assert fetch_bytes(source, manifest.artifacts[1]) == (root / "tokenizer.json").read_bytes()


def test_local_source_reports_a_missing_file(assets):
    root, manifest = assets
    missing = Artifact(path="nope.bin", role="tokenizer", sha256="0" * 64, size_bytes=1)
    with pytest.raises(SourceError):
        LocalArtifactSource(root).open(missing)


def test_local_source_rejects_a_traversing_source_path(assets):
    root, _ = assets
    escaped = Artifact(path="ok.bin", role="tokenizer", sha256="0" * 64, size_bytes=1,
                       source_path="../../etc/passwd")
    with pytest.raises(SourceError):
        LocalArtifactSource(root).open(escaped)


def test_local_source_requires_an_existing_directory(tmp_path):
    with pytest.raises(SourceError):
        LocalArtifactSource(tmp_path / "nope")


@pytest.mark.parametrize("url", ["file:///etc", "ftp://host/x", "hf://org/model", "s3://bucket/x"])
def test_http_source_only_accepts_http_schemes(url):
    with pytest.raises(SourceError) as excinfo:
        HttpArtifactSource(url, authorized=True)
    assert "scheme" in str(excinfo.value)


def test_http_source_fails_closed_without_authorization():
    with pytest.raises(SourceError) as excinfo:
        HttpArtifactSource("https://example.invalid/models/")
    assert "authorized=True" in str(excinfo.value)


def test_http_source_quotes_the_artifact_path():
    source = HttpArtifactSource("https://example.invalid/base", authorized=True)
    artifact = Artifact(path="sub/tiny model.npz", role="checkpoint", sha256="0" * 64,
                        size_bytes=1)
    assert source.url_for(artifact) == "https://example.invalid/base/sub/tiny%20model.npz"


def test_materialize_over_http_from_a_localhost_server(assets, tmp_path):
    """本地 127.0.0.1 测试服务器验证真实下载路径；不涉及任何外部网络。"""
    root, manifest = assets
    server, thread, base_url = serve_directory(root)
    try:
        source = HttpArtifactSource(base_url, authorized=True, timeout=5.0)
        cache = ModelCache(tmp_path / "cache")
        result = materialize(manifest, source, cache)
        assert set(result.fetched) == {"tiny.npz", "tokenizer.json"}
        assert result.verified.checkpoint.is_file()
        assert cache.verify(manifest).path_for("tokenizer").read_bytes() == \
            (root / "tokenizer.json").read_bytes()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_source_reports_a_missing_object(assets):
    root, manifest = assets
    server, thread, base_url = serve_directory(root)
    try:
        source = HttpArtifactSource(base_url, authorized=True, timeout=5.0)
        missing = Artifact(path="nope.bin", role="tokenizer", sha256="0" * 64, size_bytes=1)
        with pytest.raises(SourceError):
            source.open(missing)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
