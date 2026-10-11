import numpy as np
import pytest

from inference_engine import (ModelConfig, all_parameter_names, generate_weights,
                              segment_parameter_names, segment_plan)
from inference_engine.config import tiny_config
from inference_engine.errors import ConfigError, ShardError
from inference_engine.weights import parameter_shapes


def test_tiny_defaults_are_cpu_sized():
    config = tiny_config()
    assert config.n_layers == 4
    assert config.d_model % config.n_heads == 0
    assert sum(array.nbytes for array in generate_weights(config).values()) < 200_000


def test_config_roundtrip(tmp_path):
    config = tiny_config(n_layers=3, d_model=16, n_heads=2, d_ff=32)
    path = tmp_path / "config.json"
    config.save(path)
    assert ModelConfig.load(path) == config


@pytest.mark.parametrize("kwargs", [
    {"d_model": 10, "n_heads": 4},
    {"n_layers": 0},
    {"vocab_size": 0},
    {"max_seq_len": 0},
])
def test_invalid_configurations_are_rejected(kwargs):
    with pytest.raises(ConfigError):
        tiny_config(**kwargs)


@pytest.mark.parametrize("eps", [0.0, 1.0, -1.0])
def test_invalid_layer_norm_epsilon_is_rejected(eps):
    with pytest.raises(ConfigError):
        ModelConfig(model_id="m", vocab_size=4, d_model=4, n_heads=1, n_layers=1, d_ff=4,
                    max_seq_len=4, layer_norm_eps=eps)


def test_unknown_config_field_is_rejected():
    with pytest.raises(ConfigError):
        ModelConfig.from_dict({"model_id": "m", "vocab_size": 4, "d_model": 4, "n_heads": 1,
                               "n_layers": 1, "d_ff": 4, "max_seq_len": 4, "dropout": 0.1})


def test_parameter_shapes_cover_every_name():
    config = tiny_config()
    shapes = parameter_shapes(config)
    assert set(shapes) == set(all_parameter_names(config))


def test_segments_partition_the_parameter_set():
    config = tiny_config(n_layers=4)
    plan = segment_plan(4, 3)
    seen: list[str] = []
    for start, end in plan:
        names = segment_parameter_names(config, start, end)
        assert not set(names) & set(seen)
        seen.extend(names)
    assert set(seen) == set(all_parameter_names(config))


def test_segment_parameter_sets_are_self_contained():
    config = tiny_config(n_layers=4)
    first = segment_parameter_names(config, 0, 2)
    middle = segment_parameter_names(config, 1, 3)
    last = segment_parameter_names(config, 2, 4)
    assert "embed.token" in first and "embed.token" not in middle and "embed.token" not in last
    assert "head.proj" in last and "head.proj" not in first and "head.proj" not in middle
    assert "layer.1.ln1.g" in first and "layer.1.ln1.g" in middle and "layer.1.ln1.g" not in last


@pytest.mark.parametrize("num_layers,num_stages", [(4, 1), (4, 3), (5, 5), (7, 3)])
def test_segment_plan_is_contiguous_complete_and_balanced(num_layers, num_stages):
    plan = segment_plan(num_layers, num_stages)
    assert len(plan) == num_stages
    assert plan[0][0] == 0 and plan[-1][1] == num_layers
    for (start, end), (next_start, _) in zip(plan, plan[1:]):
        assert end == next_start
        assert end > start
    sizes = [end - start for start, end in plan]
    assert max(sizes) - min(sizes) <= 1


@pytest.mark.parametrize("num_layers,num_stages", [(4, 0), (4, 5), (0, 1)])
def test_impossible_plans_are_rejected(num_layers, num_stages):
    with pytest.raises(ShardError):
        segment_plan(num_layers, num_stages)


def test_segment_outside_the_model_is_rejected():
    config = tiny_config(n_layers=4)
    with pytest.raises(ShardError):
        segment_parameter_names(config, 0, 5)
    with pytest.raises(ShardError):
        segment_parameter_names(config, 3, 3)
