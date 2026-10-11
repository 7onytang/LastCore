import threading

import numpy as np
import pytest

from inference_engine import (PipelineClient, StageServer, forward_full,
                              generate_weights, load_stage, save_weights,
                              segment_plan)
from inference_engine.config import tiny_config
from inference_engine.errors import PeerError, ShardError


@pytest.fixture
def checkpoint(tmp_path):
    config = tiny_config()
    weights = generate_weights(config, seed=9)
    path = tmp_path / "tiny.npz"
    save_weights(path, weights)
    return config, weights, path


def build_chain(config, path, num_stages, **kwargs):
    """Create the stage servers for an even split, last stage first."""
    servers = []
    downstream = None
    for start, end in reversed(segment_plan(config.n_layers, num_stages)):
        server = StageServer(load_stage(config, path, start, end, task_id="t-1",
                                        assignment_id=f"a-{start}"),
                             downstream=downstream, **kwargs)
        servers.insert(0, server)
        downstream = server.address
    return servers


def run_chain(servers, tokens, **kwargs):
    threads = [threading.Thread(target=server.serve_once) for server in servers]
    for thread in threads:
        thread.start()
    try:
        return PipelineClient(servers[0].address, model_id=kwargs.pop("model_id", None),
                              **kwargs).run(tokens)
    finally:
        for thread in threads:
            thread.join(timeout=20)


@pytest.mark.parametrize("num_stages", [2, 3, 4])
def test_pipeline_over_tcp_matches_the_reference(checkpoint, num_stages):
    config, weights, path = checkpoint
    servers = build_chain(config, path, num_stages)
    tokens = np.array([[1, 2, 3, 4]], dtype=np.int64)
    try:
        logits = run_chain(servers, tokens, model_id=config.model_id)
    finally:
        for server in servers:
            server.close()
    assert np.array_equal(logits, forward_full(tokens, weights, config))


def test_each_stage_only_reports_its_own_parameter_bytes(checkpoint):
    config, weights, path = checkpoint
    servers = build_chain(config, path, 2)
    tokens = np.array([[4, 5, 6]], dtype=np.int64)
    try:
        run_chain(servers, tokens)
    finally:
        for server in servers:
            server.close()
    total = sum(value.nbytes for value in weights.values())
    assert sum(server.runtime.parameter_bytes for server in servers) == total
    assert all(server.runtime.parameter_bytes < total for server in servers)
    assert [server.processed for server in servers] == [1, 1]


def test_non_last_stage_without_a_downstream_peer_is_rejected(checkpoint):
    config, _, path = checkpoint
    with pytest.raises(ShardError, match="downstream"):
        StageServer(load_stage(config, path, 0, 2))


def serve_quietly(server):
    try:
        server.serve_once()
    except Exception:  # noqa: BLE001 - the upstream already received the ERROR frame
        pass


def test_stage_rejects_a_discontiguous_upstream(checkpoint):
    config, _, path = checkpoint
    server = StageServer(load_stage(config, path, 2, 4))
    try:
        thread = threading.Thread(target=serve_quietly, args=(server,))
        thread.start()
        with pytest.raises(PeerError, match="layer"):
            PipelineClient(server.address).run(np.array([[1, 2]], dtype=np.int64))
        thread.join(timeout=20)
    finally:
        server.close()


def test_client_rejects_a_model_mismatch(checkpoint):
    config, _, path = checkpoint
    server = StageServer(load_stage(config, path, 0, config.n_layers))
    try:
        thread = threading.Thread(target=serve_quietly, args=(server,))
        thread.start()
        with pytest.raises(PeerError, match="runs model"):
            PipelineClient(server.address, model_id="other/model").run(
                np.array([[1, 2]], dtype=np.int64))
        thread.join(timeout=20)
    finally:
        server.close()


def test_single_stage_pipeline_is_the_reference(checkpoint):
    config, weights, path = checkpoint
    server = StageServer(load_stage(config, path, 0, config.n_layers))
    try:
        tokens = np.array([[9, 8, 7]], dtype=np.int64)
        logits = run_chain([server], tokens)
    finally:
        server.close()
    assert np.array_equal(logits, forward_full(tokens, weights, config))
