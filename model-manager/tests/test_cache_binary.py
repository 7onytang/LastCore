"""Windows 字节保真回归：os.open 缺 O_BINARY 曾把 0x0A 写成 0x0D 0x0A。

根因（LC-260 有界复现）：Windows 下 ``os.open`` 缺省文本模式，
``put_stream``/``write_atomic`` 写入任意二进制时每个 0x0A 膨胀一字节
（tiny.npz 169190→169740，+550 = 550 个 0x0A）。Linux 无此翻译。
本文件只验字节保真，不碰模型契约。
"""

import hashlib
import io

from model_manager.cache import ModelCache
from model_manager.manifest import Artifact


def _artifact(payload: bytes) -> Artifact:
    return Artifact(path="blob.bin", role="checkpoint",
                    sha256=hashlib.sha256(payload).hexdigest(),
                    size_bytes=len(payload))


def test_put_stream_preserves_lf_bytes(tmp_path):
    payload = bytes(range(256)) * 4 + b"\n" * 64  # 含 0x00-0xFF 全字节与密集 0x0A
    cache = ModelCache(tmp_path / "cache")
    final = cache.put_stream(tmp_path / "rev", _artifact(payload), io.BytesIO(payload))
    assert final.read_bytes() == payload
    assert final.stat().st_size == len(payload)


def test_write_atomic_preserves_lf_bytes(tmp_path):
    payload = b'{"a": 1,\n"bb": [1,\n2,\n3]\n}\n' * 16  # 清单/标记类多换行载荷
    cache = ModelCache(tmp_path / "cache")
    target = tmp_path / "rev" / "model-manifest.json"
    cache.write_atomic(target, payload)
    assert target.read_bytes() == payload
    assert target.stat().st_size == len(payload)
