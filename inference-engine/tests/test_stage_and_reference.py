import numpy as np
import pytest

from inference_engine import forward_full, generate_weights, load_shard
from inference_engine.config import tiny_config
from inference_engine.errors import ShardError
from inference_engine.model import MAX_BATCH_SIZE
from inference_engine.stage import StageRuntime, load_stage
from inference_engine.weights import all_parameter_names


@pytest.fixture
def checkpoint(tmp_path):
    config = tiny_config()
    weights = generate_weights(config, seed=3)
    path = tmp_path / "tiny.npz"
    from inference_engine import save_weights
    save_weights(path, weights)
    return config, weights, path


def test_generation_is_deterministic_and_dtype_stable():
    config = tiny_config()
    first, second = generate_weights(config, seed=11), generate_weights(config, seed=11)
    assert set(first) == set(all_parameter_names(config))
    for name, value in first.items():
        assert value.dtype == np.float32
        assert np.array_equal(value, second[name])
    assert not np.array_equal(first["layer.0.attn.wqkv"], generate_weights(config, seed=12)["layer.0.attn.wqkv"])
    assert np.all(first["layer.0.ln1.g"] == 1.0)
    assert np.all(first["layer.0.ln1.b"] == 0.0)


def test_load_shard_keeps_only_segment_parameters(checkpoint):
    config, weights, path = checkpoint
    shard = load_shard(path, config, 2, 4)
    assert set(shard) == {name for name in weights if name.startswith("layer.2.")
                          or name.startswith("layer.3.") or name.startswith("head.")}
    assert "embed.token" not in shard
    assert shard.nbytes < sum(value.nbytes for value in weights.values())


def test_shard_byte_budget_sums_to_the_whole_checkpoint(checkpoint):
    config, weights, path = checkpoint
    total = sum(value.nbytes for value in weights.values())
    assert sum(load_shard(path, config, start, end).nbytes
               for start, end in ((0, 2), (2, 4))) == total


def test_missing_parameters_are_reported(checkpoint, tmp_path):
    config, _, path = checkpoint
    from inference_engine import save_weights
    broken = {name: np.zeros((1,), dtype=np.float32) for name in ("embed.token",)}
    broken_path = tmp_path / "broken.npz"
    save_weights(broken_path, broken)
    with pytest.raises(ShardError, match="missing"):
        load_shard(broken_path, config, 0, 2)


def test_wrong_shape_is_reported(checkpoint, tmp_path):
    config, weights, _ = checkpoint
    from inference_engine import save_weights
    mutated = {name: value for name, value in weights.items() if name.startswith(("embed", "layer.0", "layer.1"))}
    mutated["layer.0.attn.wo"] = np.zeros((3, 3), dtype=np.float32)
    path = tmp_path / "bad-shape.npz"
    save_weights(path, mutated)
    with pytest.raises(ShardError, match="shape"):
        load_shard(path, config, 0, 2)


def test_unreadable_checkpoint_is_reported(tmp_path):
    with pytest.raises(ShardError, match="cannot open"):
        load_shard(tmp_path / "missing.npz", tiny_config(), 0, 2)


def test_runtime_rejects_weights_from_another_segment(checkpoint):
    config, weights, path = checkpoint
    shard = load_shard(path, config, 0, 2)
    with pytest.raises(ShardError, match="weights mismatch"):
        StageRuntime(config, 2, 4, shard)


def test_reference_forward_is_deterministic(checkpoint):
    config, weights, _ = checkpoint
    tokens = np.array([[1, 2, 3, 4]], dtype=np.int64)
    first = forward_full(tokens, weights, config)
    assert first.shape == (1, 4, config.vocab_size)
    assert first.dtype == np.float32
    assert np.array_equal(first, forward_full(tokens, weights, config))


def test_attention_is_causal(checkpoint):
    config, weights, _ = checkpoint
    original = forward_full(np.array([[3, 4, 5, 6]], dtype=np.int64), weights, config)
    changed = forward_full(np.array([[3, 4, 5, 9]], dtype=np.int64), weights, config)
    assert np.array_equal(original[:, :3], changed[:, :3])
    assert not np.array_equal(original[:, 3], changed[:, 3])


def test_first_stage_requires_integer_tokens(checkpoint):
    config, _, path = checkpoint
    runtime = load_stage(config, path, 0, 2)
    with pytest.raises(ShardError, match="integer token ids"):
        runtime.execute(np.zeros((1, 2, config.d_model), dtype=np.float32))
    with pytest.raises(ShardError, match="token ids"):
        runtime.execute(np.array([[config.vocab_size]], dtype=np.int64))


def test_hidden_state_contract_is_enforced(checkpoint):
    config, _, path = checkpoint
    runtime = load_stage(config, path, 2, 4)
    with pytest.raises(ShardError, match="hidden state"):
        runtime.execute(np.zeros((1, 2, config.d_model + 1), dtype=np.float32))
    with pytest.raises(ShardError, match="max_seq_len"):
        runtime.execute(np.zeros((1, config.max_seq_len + 1, config.d_model), dtype=np.float32))
    assert runtime.execute(np.zeros((1, 2, config.d_model), dtype=np.float32)).shape == \
        (1, 2, config.vocab_size)


def test_oversized_batch_is_rejected_before_allocating(checkpoint):
    config, weights, path = checkpoint
    over = MAX_BATCH_SIZE + 1
    first = load_stage(config, path, 0, 2)
    with pytest.raises(ShardError, match="batch size"):
        first.execute(np.zeros((over, 2), dtype=np.int64))
    last = load_stage(config, path, 2, 4)
    with pytest.raises(ShardError, match="batch size"):
        last.execute(np.zeros((over, 2, config.d_model), dtype=np.float32))
    with pytest.raises(ShardError, match="batch size"):
        forward_full(np.zeros((over, 2), dtype=np.int64), weights, config)


def test_batch_at_the_bound_is_served(checkpoint):
    config, _, path = checkpoint
    tokens = np.zeros((MAX_BATCH_SIZE, 2), dtype=np.int64)
    hidden = load_stage(config, path, 0, 2).execute(tokens)
    assert hidden.shape == (MAX_BATCH_SIZE, 2, config.d_model)
    assert load_stage(config, path, 2, 4).execute(hidden).shape == \
        (MAX_BATCH_SIZE, 2, config.vocab_size)


def test_roles_and_descriptions(checkpoint):
    config, _, path = checkpoint
    assert load_stage(config, path, 0, 2).role == "first"
    assert load_stage(config, path, 2, 4).role == "last"
    single = load_stage(config, path, 0, config.n_layers)
    assert single.role == "single"
    assert single.describe()["parameter_bytes"] == single.parameter_bytes
