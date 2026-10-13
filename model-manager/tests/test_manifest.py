import copy
import json

import pytest

from inference_engine.config import tiny_config
from model_manager.errors import (BackendCapabilityError, ManifestError, PathTraversalError,
                                  UnsupportedArchitectureError)
from model_manager.manifest import (MANIFEST_VERSION, ModelManifest, check_backend_capability,
                                    normalize_artifact_path)
from model_manager.publish import describe_artifact, ArtifactInput
from tests.helpers import MODEL_ID, build_assets


@pytest.fixture
def manifest(tmp_path):
    return build_assets(tmp_path / "assets")


def payload_of(manifest):
    return copy.deepcopy(manifest.to_payload())


def refresh_container(payload):
    """追加 artifact 之后同步 container 声明，好让测试命中目标校验点。"""
    container = sum(item["size_bytes"] for item in payload["artifacts"])
    payload["resources"]["container_bytes"] = container
    payload["resources"]["min_free_disk_bytes"] = max(
        payload["resources"]["min_free_disk_bytes"], container)
    return payload


def test_manifest_roundtrips_through_json(manifest, tmp_path):
    path = tmp_path / "manifest.json"
    manifest.save(path)
    reloaded = ModelManifest.load(path)
    assert reloaded == manifest
    assert reloaded.to_payload() == json.loads(path.read_text(encoding="utf-8"))


def test_declared_bytes_match_the_engine_layout(manifest):
    assert manifest.resources.parameter_bytes == 155392
    assert manifest.layer_bytes == (34176,) * 4
    assert manifest.module_bytes == {"embedding": 10240, "head": 8448, "layers": 136704}
    assert manifest.layout.segment_bytes(0, 2) == 78592
    assert manifest.layout.segment_bytes(2, 4) == 76800
    assert manifest.resources.container_bytes == sum(
        artifact.size_bytes for artifact in manifest.artifacts)
    assert manifest.n_layers == 4
    assert manifest.manifest_version == MANIFEST_VERSION


def test_license_is_recorded_without_inventing_a_repository_license(manifest):
    assert manifest.license.identifier is None
    assert manifest.license.redistributable is True
    assert "第三方" in manifest.license.notes


def test_unsupported_architecture_is_rejected_before_any_download(manifest):
    payload = payload_of(manifest)
    payload["architecture"] = "llama"
    with pytest.raises(UnsupportedArchitectureError) as excinfo:
        ModelManifest.from_payload(payload)
    assert "before any download" in str(excinfo.value)
    assert excinfo.value.architecture == "llama"
    assert "lastcore.tiny-decoder" in excinfo.value.supported


@pytest.mark.parametrize("key", ["entrypoint", "python", "command", "script", "post_install",
                                 "hook", "exec", "download", "unknown_field"])
def test_unknown_top_level_field_is_rejected(manifest, key):
    payload = payload_of(manifest)
    payload[key] = "anything"
    with pytest.raises(ManifestError) as excinfo:
        ModelManifest.from_payload(payload)
    assert key in str(excinfo.value)


def test_executable_looking_field_inside_artifact_is_rejected(manifest):
    payload = payload_of(manifest)
    payload["artifacts"][0]["post_install"] = "rm -rf /"
    with pytest.raises(ManifestError) as excinfo:
        ModelManifest.from_payload(payload)
    assert "artifacts[0]" in str(excinfo.value)


def test_unknown_nested_license_field_is_rejected(manifest):
    payload = payload_of(manifest)
    payload["license"]["code"] = "evil"
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


@pytest.mark.parametrize("field", ["manifest_version", "architecture", "dtype", "model",
                                   "layer_bytes", "module_bytes", "license", "resources",
                                   "artifacts", "backends"])
def test_required_field_is_mandatory(manifest, field):
    payload = payload_of(manifest)
    del payload[field]
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_unsupported_manifest_version_is_rejected(manifest):
    payload = payload_of(manifest)
    payload["manifest_version"] = "2"
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


@pytest.mark.parametrize("dtype", ["float64", "int8", "", None])
def test_invalid_dtype_is_rejected(manifest, dtype):
    payload = payload_of(manifest)
    payload["dtype"] = dtype
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_model_config_must_match_the_manifest_model_id(manifest):
    payload = payload_of(manifest)
    payload["model"]["model_id"] = "other/model"
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_invalid_model_shape_is_rejected(manifest):
    payload = payload_of(manifest)
    payload["model"]["d_model"] = 10
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_wrong_layer_bytes_are_detected(manifest):
    payload = payload_of(manifest)
    payload["layer_bytes"] = [1, 1, 1, 1]
    with pytest.raises(ManifestError) as excinfo:
        ModelManifest.from_payload(payload)
    assert "layer_bytes" in str(excinfo.value)


def test_wrong_layer_count_is_detected(manifest):
    payload = payload_of(manifest)
    payload["layer_bytes"] = [34176, 34176]
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_wrong_parameter_bytes_are_detected(manifest):
    payload = payload_of(manifest)
    payload["resources"]["parameter_bytes"] = 999
    with pytest.raises(ManifestError) as excinfo:
        ModelManifest.from_payload(payload)
    assert "parameter_bytes" in str(excinfo.value)


def test_wrong_container_bytes_are_detected(manifest):
    payload = payload_of(manifest)
    payload["resources"]["container_bytes"] = 1
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_container_bytes_smaller_than_min_free_disk_is_rejected(manifest):
    payload = payload_of(manifest)
    payload["resources"]["min_free_disk_bytes"] = 10
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_runtime_memory_below_parameter_bytes_is_rejected(manifest):
    payload = payload_of(manifest)
    payload["resources"]["runtime_memory_bytes"] = 1024
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


@pytest.mark.parametrize("module,value", [("embedding", 1), ("head", 1), ("layers", 1)])
def test_wrong_module_bytes_are_detected(manifest, module, value):
    payload = payload_of(manifest)
    payload["module_bytes"][module] = value
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_missing_module_key_is_rejected(manifest):
    payload = payload_of(manifest)
    del payload["module_bytes"]["head"]
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_layer_segments_must_tile_the_model(manifest):
    payload = payload_of(manifest)
    payload["artifacts"][0]["layer_start"] = 0
    payload["artifacts"][0]["layer_end"] = 2
    clone = copy.deepcopy(payload["artifacts"][0])
    clone["path"] = "second.npz"
    clone["layer_start"], clone["layer_end"] = 3, 4
    payload["artifacts"].append(clone)
    refresh_container(payload)
    with pytest.raises(ManifestError) as excinfo:
        ModelManifest.from_payload(payload)
    assert "tile" in str(excinfo.value)


def test_overlapping_layer_segments_are_rejected(manifest):
    payload = payload_of(manifest)
    payload["artifacts"][0]["layer_end"] = 3
    clone = copy.deepcopy(payload["artifacts"][0])
    clone["path"] = "second.npz"
    clone["layer_start"], clone["layer_end"] = 2, 4
    payload["artifacts"].append(clone)
    refresh_container(payload)
    with pytest.raises(ManifestError) as excinfo:
        ModelManifest.from_payload(payload)
    assert "tile" in str(excinfo.value)


def test_checkpoint_segment_beyond_the_model_is_rejected(manifest):
    payload = payload_of(manifest)
    payload["artifacts"][0]["layer_end"] = 9
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_at_least_one_checkpoint_artifact_is_required(manifest):
    payload = payload_of(manifest)
    payload["artifacts"] = [item for item in payload["artifacts"] if item["role"] != "checkpoint"]
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_duplicate_artifact_paths_are_rejected(manifest):
    payload = payload_of(manifest)
    payload["artifacts"].append(copy.deepcopy(payload["artifacts"][1]))
    refresh_container(payload)
    with pytest.raises(ManifestError) as excinfo:
        ModelManifest.from_payload(payload)
    assert "duplicate" in str(excinfo.value)


def test_checkpoint_dtype_must_match_the_manifest_dtype(manifest):
    payload = payload_of(manifest)
    payload["artifacts"][0]["dtype"] = "float16"
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_unknown_artifact_role_is_rejected(manifest):
    payload = payload_of(manifest)
    payload["artifacts"][1]["role"] = "executable"
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_bad_sha256_shape_is_rejected(manifest):
    payload = payload_of(manifest)
    payload["artifacts"][0]["sha256"] = "NOTAHASH"
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_layer_bounds_must_be_provided_together(manifest):
    payload = payload_of(manifest)
    del payload["artifacts"][0]["layer_end"]
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_backends_must_be_declared(manifest):
    payload = payload_of(manifest)
    payload["backends"] = {}
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


def test_storage_dtype_must_be_accepted_by_at_least_one_backend(manifest):
    payload = payload_of(manifest)
    for backend in payload["backends"].values():
        backend["dtypes"] = ["float16"]
    with pytest.raises(ManifestError) as excinfo:
        ModelManifest.from_payload(payload)
    assert "no declared backend" in str(excinfo.value)


def test_backend_dtypes_must_be_known(manifest):
    payload = payload_of(manifest)
    payload["backends"]["reference"]["dtypes"] = ["bfloat16"]
    with pytest.raises(ManifestError):
        ModelManifest.from_payload(payload)


@pytest.mark.parametrize("backend", ["unknown-backend", "Reference", ""])
def test_unknown_backend_is_rejected(manifest, backend):
    with pytest.raises(BackendCapabilityError) as excinfo:
        check_backend_capability(manifest, backend)
    assert backend in str(excinfo.value) or backend == ""


def test_backend_dtype_must_be_supported(manifest):
    payload = payload_of(manifest)
    payload["backends"]["reference"]["dtypes"] = ["float32"]
    payload["backends"]["inference-engine.stage"]["dtypes"] = ["float32"]
    narrow = ModelManifest.from_payload(payload)
    check_backend_capability(narrow, "reference", "float32")
    with pytest.raises(BackendCapabilityError) as excinfo:
        check_backend_capability(narrow, "reference", "float16")
    assert excinfo.value.dtype == "float16"
    assert excinfo.value.available == ("float32",)


def test_backend_max_seq_len_is_enforced(manifest):
    payload = payload_of(manifest)
    payload["backends"]["reference"]["max_seq_len"] = 4
    payload["backends"]["inference-engine.stage"]["max_seq_len"] = 4
    narrow = ModelManifest.from_payload(payload)
    with pytest.raises(BackendCapabilityError):
        check_backend_capability(narrow, "reference")


def test_backend_runtime_memory_is_enforced(manifest):
    payload = payload_of(manifest)
    payload["backends"]["reference"]["runtime_memory_bytes"] = 1
    narrow = ModelManifest.from_payload(payload)
    with pytest.raises(BackendCapabilityError):
        check_backend_capability(narrow, "reference")


def test_no_backend_requirement_still_checks_dtype(manifest):
    check_backend_capability(manifest, None)
    check_backend_capability(manifest, None, "float16")
    with pytest.raises(ManifestError):
        check_backend_capability(manifest, None, "int8")


@pytest.mark.parametrize("path", ["../evil.npz", "/etc/passwd", "C:/windows/x", "a/../../b",
                                  "./tiny.npz", "a//b", "", ".", "..", "a/./b"])
def test_unsafe_artifact_paths_are_rejected(path):
    with pytest.raises(PathTraversalError):
        normalize_artifact_path(path)


@pytest.mark.parametrize("path", ["tiny.npz", "sub/dir/tiny.npz", "a.b-c_d/e.npz"])
def test_clean_artifact_paths_are_accepted(path):
    assert normalize_artifact_path(path) == path


def test_traversal_in_artifact_path_fails_manifest_parsing(manifest):
    payload = payload_of(manifest)
    payload["artifacts"][0]["path"] = "../escape.npz"
    with pytest.raises(PathTraversalError):
        ModelManifest.from_payload(payload)


def test_traversal_in_source_path_fails_manifest_parsing(manifest):
    payload = payload_of(manifest)
    payload["artifacts"][0]["source_path"] = "/etc/passwd"
    with pytest.raises(PathTraversalError):
        ModelManifest.from_payload(payload)


def test_describe_artifact_uses_the_real_file_bytes(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"1234")
    described = describe_artifact(ArtifactInput(file=target, role="tokenizer", path="blob.bin"))
    assert described.size_bytes == 4
    assert described.sha256 == __import__("hashlib").sha256(b"1234").hexdigest()


def test_describe_artifact_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        describe_artifact(ArtifactInput(file=tmp_path / "nope.bin", role="tokenizer"))


def test_model_config_is_reused_from_the_engine(manifest):
    from inference_engine.config import ModelConfig

    assert isinstance(manifest.model, ModelConfig)
    assert manifest.model == tiny_config(MODEL_ID)
