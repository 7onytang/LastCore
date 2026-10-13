import pytest

from inference_engine.config import tiny_config
from model_manager.cost import (cost_report, disk_requirement, measure_hash_throughput,
                                segment_cost, sha256_file)
from model_manager.errors import ManifestError
from model_manager.layout import dtype_bytes, layout_for
from tests.helpers import build_assets


@pytest.fixture
def manifest(tmp_path):
    return build_assets(tmp_path / "assets")


def test_layout_matches_the_engine_parameter_layout():
    layout = layout_for(tiny_config(), "float32")
    assert layout.n_layers == 4
    assert layout.n_tensors == 53
    assert layout.embedding_bytes == 10240
    assert layout.head_bytes == 8448
    assert layout.per_layer_bytes == (34176,) * 4
    assert layout.layers_bytes == 136704
    assert layout.parameter_bytes == 155392
    assert layout.module_bytes == {"embedding": 10240, "head": 8448, "layers": 136704}


def test_float16_halves_every_byte_claim():
    layout = layout_for(tiny_config(), "float16")
    assert layout.dtype_bytes == 2
    assert layout.parameter_bytes == 155392 // 2
    assert layout.per_layer_bytes == (34176 // 2,) * 4
    assert layout.embedding_bytes == 10240 // 2
    assert layout.head_bytes == 8448 // 2


def test_unknown_dtype_is_rejected():
    with pytest.raises(ManifestError):
        dtype_bytes("bfloat16")
    with pytest.raises(ManifestError):
        layout_for(tiny_config(), "int8")


def test_segment_bytes_match_the_engine_readme_numbers():
    layout = layout_for(tiny_config(), "float32")
    assert layout.segment_bytes(0, 1) == 44416
    assert layout.segment_bytes(1, 3) == 68352
    assert layout.segment_bytes(2, 4) == 76800
    assert layout.segment_bytes(0, 2) == 78592
    assert layout.segment_bytes(3, 4) == 42624
    total = sum(layout.segment_bytes(*segment) for segment in
                ((0, 1), (1, 3), (3, 4)))
    assert total == layout.parameter_bytes


def test_segment_bytes_out_of_range_is_rejected():
    layout = layout_for(tiny_config(), "float32")
    with pytest.raises(Exception):
        layout.segment_bytes(3, 3)
    with pytest.raises(Exception):
        layout.segment_bytes(0, 5)


def test_cost_report_exposes_every_cost_dimension(manifest):
    report = cost_report(manifest)
    assert report.sha256_hex_chars == 64
    assert report.container_overhead_bytes == report.container_bytes - report.parameter_bytes
    payload = report.as_dict()
    for key in ("embedding_bytes", "head_bytes", "per_layer_bytes", "parameter_bytes",
                "container_bytes", "min_free_disk_bytes", "runtime_memory_bytes", "dtype_bytes"):
        assert key in payload
    assert payload["per_layer_bytes"] == [34176] * 4


def test_segment_cost_marks_embedding_and_head(manifest):
    first = segment_cost(manifest, 0, 2)
    assert first.includes_embedding is True
    assert first.includes_head is False
    assert first.parameter_bytes == 78592
    assert first.parameter_names == len(manifest.layout.segment_names(0, 2))
    assert first.runtime_memory_bytes == first.parameter_bytes + first.activation_bytes

    last = segment_cost(manifest, 2, 4)
    assert last.includes_embedding is False
    assert last.includes_head is True
    assert last.parameter_bytes == 76800
    assert last.activation_bytes > first.activation_bytes

    middle = segment_cost(manifest, 1, 2)
    assert middle.includes_embedding is False
    assert middle.includes_head is False
    assert middle.parameter_bytes == 34176


def test_segment_cost_rejects_bad_ranges(manifest):
    with pytest.raises(ValueError):
        segment_cost(manifest, 2, 2)
    with pytest.raises(ValueError):
        segment_cost(manifest, 0, 9)


def test_disk_requirement_uses_the_larger_declaration(manifest):
    assert disk_requirement(manifest) == max(manifest.resources.min_free_disk_bytes,
                                             manifest.resources.container_bytes)
    assert disk_requirement(manifest) >= manifest.resources.container_bytes


def test_sha256_file_is_streaming_and_size_accurate(manifest, tmp_path):
    """同一文件无论缓冲大小都应得到相同的摘要与字节数。"""
    checkpoint = tmp_path / "assets" / "tiny.npz"
    big_digest, big_size = sha256_file(checkpoint)
    small_digest, small_size = sha256_file(checkpoint, buffer=1024)
    assert big_digest == small_digest == manifest.artifacts[0].sha256
    assert big_size == small_size == manifest.artifacts[0].size_bytes


def test_hash_throughput_is_measured(tmp_path):
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"x" * 4096)
    throughput = measure_hash_throughput(blob, repeats=2)
    assert throughput > 0
