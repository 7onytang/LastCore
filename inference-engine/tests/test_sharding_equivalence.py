import numpy as np
import pytest

from inference_engine import forward_full, generate_weights, save_weights
from inference_engine.config import tiny_config
from inference_engine.stage import load_stage, segment_plan

PROMPTS = ([1, 2, 3, 4], [7], [0, 63, 5, 8, 9, 10], [2, 2, 2, 2, 2, 2, 2, 2])


@pytest.fixture
def checkpoint(tmp_path):
    config = tiny_config()
    weights = generate_weights(config, seed=5)
    path = tmp_path / "tiny.npz"
    save_weights(path, weights)
    return config, weights, path


@pytest.mark.parametrize("num_stages", [1, 2, 3, 4])
@pytest.mark.parametrize("prompt", PROMPTS)
def test_chained_segments_match_the_single_process_reference(checkpoint, num_stages, prompt):
    config, weights, path = checkpoint
    tokens = np.array([prompt], dtype=np.int64)
    expected = forward_full(tokens, weights, config)
    activation = tokens
    for start, end in segment_plan(config.n_layers, num_stages):
        activation = load_stage(config, path, start, end).execute(activation)
    assert activation.shape == expected.shape
    assert np.array_equal(activation, expected)


@pytest.mark.parametrize("prompt", PROMPTS)
def test_two_stage_split_is_bit_identical_not_merely_close(checkpoint, prompt):
    config, weights, path = checkpoint
    tokens = np.array([prompt], dtype=np.int64)
    reference = forward_full(tokens, weights, config)
    distributed = load_stage(config, path, 2, 4).execute(
        load_stage(config, path, 0, 2).execute(tokens))
    assert np.array_equal(reference, distributed)
    bytes_reference = np.ascontiguousarray(reference).tobytes()
    assert bytes_reference == np.ascontiguousarray(distributed).tobytes()


def test_no_stage_holds_a_parameter_of_another_stage(checkpoint):
    config, weights, path = checkpoint
    first = load_stage(config, path, 0, 2)
    last = load_stage(config, path, 2, 4)
    assert set(first.weights) & set(last.weights) == set()
    assert set(first.weights) | set(last.weights) == set(weights)
