import struct

import pytest

from communication.errors import ProtocolError
from communication.protocol import (MAGIC, MAX_HEADER_BYTES, PREFIX, PREFIX_SIZE,
                                    VERSION, Frame, FrameType, decode_header,
                                    encode_frame, parse_prefix)


def test_frame_layout_matches_the_documented_format():
    raw = encode_frame(Frame(FrameType.HELLO, {"kind": "hello"}))
    magic, version, kind, header_len, payload_len = PREFIX.unpack(raw[:PREFIX_SIZE])
    assert (magic, version, kind) == (MAGIC, VERSION, FrameType.HELLO)
    assert header_len == len(raw) - PREFIX_SIZE
    assert payload_len == 0
    assert raw[PREFIX_SIZE:] == b'{"kind":"hello"}'


def test_payload_length_is_part_of_the_prefix():
    raw = encode_frame(Frame(FrameType.TENSOR, {"kind": "tensor"}, b"abcd"))
    assert PREFIX.unpack(raw[:PREFIX_SIZE])[4] == 4
    assert raw.endswith(b"abcd")


@pytest.mark.parametrize("frame_type", list(FrameType))
def test_every_frame_type_survives_parsing(frame_type):
    raw = encode_frame(Frame(frame_type, {"kind": frame_type.name.lower()}, b"x"))
    assert parse_prefix(raw[:PREFIX_SIZE])[0] is frame_type


def test_bad_magic_is_rejected():
    with pytest.raises(ProtocolError, match="magic"):
        parse_prefix(struct.pack(">2sBBII", b"XX", VERSION, 1, 0, 0))


def test_unsupported_version_is_rejected():
    with pytest.raises(ProtocolError, match="version"):
        parse_prefix(struct.pack(">2sBBII", MAGIC, VERSION + 1, 1, 0, 0))


def test_unknown_frame_type_is_rejected():
    with pytest.raises(ProtocolError, match="frame type"):
        parse_prefix(struct.pack(">2sBBII", MAGIC, VERSION, 99, 0, 0))


def test_oversized_header_is_rejected():
    with pytest.raises(ProtocolError, match="header"):
        parse_prefix(struct.pack(">2sBBII", MAGIC, VERSION, 1, MAX_HEADER_BYTES + 1, 0))
    with pytest.raises(ProtocolError, match="header"):
        encode_frame(Frame(FrameType.HELLO, {"blob": "x" * (MAX_HEADER_BYTES + 1)}))


def test_header_must_be_utf8_json_object():
    with pytest.raises(ProtocolError, match="JSON"):
        decode_header(b"{not json")
    with pytest.raises(ProtocolError, match="JSON object"):
        decode_header(b"[1, 2, 3]")
    with pytest.raises(ProtocolError, match="UTF-8"):
        decode_header(b"\xff\xfe")


def test_non_serialisable_header_is_rejected():
    with pytest.raises(ProtocolError, match="JSON serialisable"):
        encode_frame(Frame(FrameType.HELLO, {"kind": object()}))
    with pytest.raises(ProtocolError, match="JSON serialisable"):
        encode_frame(Frame(FrameType.HELLO, {"kind": "hello", "nan": float("nan")}))
