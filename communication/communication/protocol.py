"""LastCore node-to-node tensor transport protocol, version 1.

Every message is a frame::

    +--------+---------+-------+-------------+--------------+---------+---------------+
    | "LC"   | version | type  | header_len  | payload_len  | header  | payload       |
    | 2 byte | 1 byte  | 1 byte| 4 byte BE   | 4 byte BE    | JSON    | tensor bytes  |
    +--------+---------+-------+-------------+--------------+---------+---------------+

The header is UTF-8 JSON. Tensors are raw little-endian, C-contiguous bytes; the
header carries dtype, shape, byte length and a SHA-256 checksum so a receiver can
reject truncated or corrupted payloads before allocating the array.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Mapping

import numpy as np

from .errors import ProtocolError

MAGIC = b"LC"
VERSION = 1
PREFIX = struct.Struct(">2sBBII")
PREFIX_SIZE = PREFIX.size
MAX_HEADER_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 512 * 1024 * 1024

TENSOR_DTYPES = {
    "float32": np.dtype("<f4"),
    "float16": np.dtype("<f2"),
    "int32": np.dtype("<i4"),
    "int64": np.dtype("<i8"),
}
_DTYPE_BY_STR = {dtype.str: name for name, dtype in TENSOR_DTYPES.items()}


class FrameType(IntEnum):
    HELLO = 1
    TENSOR = 2
    RESULT = 3
    ERROR = 4
    CANCEL = 5
    BYE = 6


@dataclass(frozen=True)
class Frame:
    type: FrameType
    header: Mapping[str, Any] = field(default_factory=dict)
    payload: bytes = b""


def encode_frame(frame: Frame) -> bytes:
    try:
        header = json.dumps(frame.header, separators=(",", ":"), ensure_ascii=False,
                            allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"header is not JSON serialisable: {exc}") from exc
    if len(header) > MAX_HEADER_BYTES:
        raise ProtocolError(f"header of {len(header)} bytes exceeds {MAX_HEADER_BYTES}")
    if len(frame.payload) > MAX_PAYLOAD_BYTES:
        raise ProtocolError(f"payload of {len(frame.payload)} bytes exceeds {MAX_PAYLOAD_BYTES}")
    return PREFIX.pack(MAGIC, VERSION, int(frame.type), len(header), len(frame.payload)) + header + frame.payload


def parse_prefix(prefix: bytes) -> tuple[FrameType, int, int]:
    if len(prefix) != PREFIX_SIZE:
        raise ProtocolError(f"frame prefix must be {PREFIX_SIZE} bytes")
    magic, version, frame_type, header_len, payload_len = PREFIX.unpack(prefix)
    if magic != MAGIC:
        raise ProtocolError(f"bad frame magic {magic!r}")
    if version != VERSION:
        raise ProtocolError(f"unsupported protocol version {version}")
    try:
        kind = FrameType(frame_type)
    except ValueError as exc:
        raise ProtocolError(f"unknown frame type {frame_type}") from exc
    if header_len > MAX_HEADER_BYTES:
        raise ProtocolError(f"declared header of {header_len} bytes exceeds {MAX_HEADER_BYTES}")
    if payload_len > MAX_PAYLOAD_BYTES:
        raise ProtocolError(f"declared payload of {payload_len} bytes exceeds {MAX_PAYLOAD_BYTES}")
    return kind, header_len, payload_len


def decode_header(header: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"header is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("header must be a JSON object")
    return value


def encode_tensor(name: str, array, dtype: str | None = None) -> tuple[dict[str, Any], bytes]:
    target = TENSOR_DTYPES.get(dtype or np.asarray(array).dtype.name)
    if target is None:
        raise ProtocolError(f"unsupported tensor dtype {dtype!r}")
    data = np.ascontiguousarray(array)
    if data.dtype != target:
        if not np.can_cast(data.dtype, target, casting="same_kind"):
            raise ProtocolError(f"cannot represent {data.dtype} as {target}")
        data = data.astype(target)
    payload = data.tobytes(order="C")
    header = {
        "kind": "tensor",
        "name": name,
        "dtype": _DTYPE_BY_STR[target.str],
        "shape": list(data.shape),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    return header, payload


def decode_tensor(header: Mapping[str, Any], payload: bytes) -> np.ndarray:
    if header.get("kind") != "tensor":
        raise ProtocolError(f"expected a tensor header, got {header.get('kind')!r}")
    name = header.get("dtype")
    dtype = TENSOR_DTYPES.get(name)
    if dtype is None:
        raise ProtocolError(f"unsupported tensor dtype {name!r}")
    shape = header.get("shape")
    if not isinstance(shape, list) or not all(isinstance(dim, int) and dim >= 0 for dim in shape):
        raise ProtocolError(f"invalid tensor shape {shape!r}")
    if header.get("bytes") != len(payload):
        raise ProtocolError(f"declared {header.get('bytes')} bytes but received {len(payload)}")
    expected = header.get("sha256")
    if expected and expected != hashlib.sha256(payload).hexdigest():
        raise ProtocolError("tensor checksum mismatch")
    itemsize = dtype.itemsize
    expected_items = 1
    for dim in shape:
        expected_items *= dim
    if expected_items * itemsize != len(payload):
        raise ProtocolError(f"shape {shape} does not match {len(payload)} payload bytes")
    return np.frombuffer(payload, dtype=dtype).reshape(shape).copy()
