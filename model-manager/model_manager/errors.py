"""model-manager 的错误类型。

错误按可恢复性分层：

* 清单与能力类错误（``ManifestError`` 及子类）在下载前、按声明即抛出，做到 fail closed；
* 路径类错误（``PathTraversalError``）在把清单里的相对路径落到磁盘之前抛出；
* 完整性类错误（``IntegrityError``）只在读取或校验本地文件时抛出；
* 环境类错误（``InsufficientDiskError`` / ``LockTimeoutError``）说明运行前提不满足；
* 源类错误（``SourceError``）说明产物源不可用或协议未被授权。

所有错误都携带结构化字段，便于上层如实上报，不需要解析错误消息。
"""

from __future__ import annotations

from typing import Sequence


class ModelManagerError(Exception):
    """model-manager 全部错误的基类。"""


class ManifestError(ModelManagerError):
    """清单不符合 schema，缺少必需字段、出现未声明字段或声明值与实际布局矛盾。"""


class UnsupportedArchitectureError(ManifestError):
    """清单声明的架构不在本引擎已实现的算子集合内，启动前拒绝。"""

    def __init__(self, architecture: str, supported: Sequence[str]):
        self.architecture = architecture
        self.supported = tuple(supported)
        super().__init__(
            f"unsupported architecture {architecture!r}; the engine implements only "
            f"{', '.join(self.supported)}; refusing to start before any download"
        )


class BackendCapabilityError(ManifestError):
    """请求的后端 / dtype / 序列长度不在清单声明的兼容范围内，启动前拒绝。"""

    def __init__(self, reason: str, *, backend: str | None = None, dtype: str | None = None,
                 available: Sequence[str] | None = None):
        self.backend = backend
        self.dtype = dtype
        self.available = tuple(available) if available is not None else None
        super().__init__(reason)


class PathTraversalError(ModelManagerError):
    """清单中的相对路径会逃逸出目标目录（绝对路径、``..``、盘符或符号链接），拒绝落盘。"""

    def __init__(self, path: object, reason: str, *, root: object | None = None):
        self.path = path
        self.reason = reason
        self.root = root
        where = f" under {root}" if root is not None else ""
        super().__init__(f"unsafe artifact path {path!r}{where}: {reason}")


class IntegrityError(ModelManagerError):
    """本地文件缺失、损坏、大小或哈希不符，或 revision 尚未完成落盘。"""


class InsufficientDiskError(ModelManagerError):
    """目标文件系统可用空间不足以放下本次落盘。"""

    def __init__(self, root: object, required_bytes: int, available_bytes: int):
        self.root = root
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        super().__init__(
            f"insufficient disk space at {root}: need {required_bytes} bytes "
            f"({required_bytes / 1024 / 1024:.2f} MiB), available {available_bytes} bytes "
            f"({available_bytes / 1024 / 1024:.2f} MiB)"
        )


class LockTimeoutError(ModelManagerError):
    """同一 (model_id, revision) 的锁在超时时间内没有释放。"""

    def __init__(self, lock_path: object, timeout: float, holder: str | None = None):
        self.lock_path = lock_path
        self.timeout = timeout
        self.holder = holder
        detail = f", held by {holder!r}" if holder else ""
        super().__init__(f"timed out after {timeout:.1f}s waiting for {lock_path}{detail}")


class SourceError(ModelManagerError):
    """产物源不可用：文件不存在、读取失败，或 URL 协议不在允许范围内。"""


class EngineAdapterError(ModelManagerError):
    """校验后的产物无法交给引擎 loader：checkpoint 缺失参数、形状不符或不是合法 npz。"""
