import numpy as np
import pytest

from communication.errors import ProtocolError
from communication.protocol import TENSOR_DTYPES, decode_tensor, encode_tensor


@pytest.mark.parametrize("dtype", list(TENSOR_DTYPES))
def test_roundtrip_preserves_bytes_and_shape(dtype):
    array = np.arange(24, dtype=np.int64).reshape(2, 3, 4).astype(TENSOR_DTYPES[dtype])
    header, payload = encode_tensor("hidden", array)
    restored = decode_tensor(header, payload)
    assert restored.dtype == array.dtype
    assert restored.shape == array.shape
    assert np.array_equal(restored, array)
    assert header["bytes"] == array.nbytes


def test_decode_returns_a_writable_copy():
    header, payload = encode_tensor("hidden", np.ones((2, 2), dtype=np.float32))
    restored = decode_tensor(header, payload)
    restored[0, 0] = 5.0
    assert restored[0, 0] == 5.0


def test_non_contiguous_input_is_compacted():
    base = np.arange(12, dtype=np.float32).reshape(3, 4)
    header, payload = encode_tensor("hidden", base[:, ::2])
    assert header["shape"] == [3, 2]
    assert np.array_equal(decode_tensor(header, payload), base[:, ::2])


def test_truncated_payload_is_rejected():
    header, payload = encode_tensor("hidden", np.zeros((4, 4), dtype=np.float32))
    with pytest.raises(ProtocolError, match="declared"):
        decode_tensor(header, payload[:-4])


def test_corrupted_payload_is_rejected():
    header, payload = encode_tensor("hidden", np.zeros((4, 4), dtype=np.float32))
    corrupted = bytearray(payload)
    corrupted[0] = 1
    with pytest.raises(ProtocolError, match="checksum"):
        decode_tensor(header, bytes(corrupted))


def test_missing_checksum_is_rejected():
    """A peer may not opt out of integrity checking by omitting sha256."""
    header, payload = encode_tensor("hidden", np.zeros((4, 4), dtype=np.float32))
    for stripped in (None, ""):
        broken = {key: value for key, value in header.items() if key != "sha256"}
        if stripped is not None:
            broken["sha256"] = stripped
        with pytest.raises(ProtocolError, match="sha256"):
            decode_tensor(broken, payload)


def test_shape_and_payload_length_must_agree():
    header, payload = encode_tensor("hidden", np.zeros((4, 4), dtype=np.float32))
    header = {**header, "shape": [2, 2]}
    with pytest.raises(ProtocolError, match="shape"):
        decode_tensor(header, payload)


def test_unknown_dtype_is_rejected():
    with pytest.raises(ProtocolError, match="unsupported tensor dtype"):
        encode_tensor("hidden", np.zeros((2, 2), dtype=np.float64))


def test_header_that_is_not_a_tensor_is_rejected():
    with pytest.raises(ProtocolError, match="tensor header"):
        decode_tensor({"kind": "logits"}, b"")
