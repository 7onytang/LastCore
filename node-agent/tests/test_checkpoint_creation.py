"""First checkpoint publication with fake bytes only: no model execution or sockets."""
import os
import sys
from types import ModuleType

import pytest

from lastcore_node_agent import inference_adapter as adapter


@pytest.fixture
def fake_engine(monkeypatch):
    engine = ModuleType("inference_engine")
    calls = []
    weights = object()

    def generate(config, seed):
        calls.append(("generate", config, seed))
        return weights

    def save(path, values):
        assert values is weights
        assert path.suffix == ".npz"  # np.savez must not append a different filename.
        calls.append(("save", path))
        path.write_bytes(b"complete fake checkpoint")

    engine.generate_weights = generate
    engine.save_weights = save
    monkeypatch.setitem(sys.modules, "inference_engine", engine)
    return engine, calls


def test_existing_checkpoint_is_untouched(tmp_path, fake_engine):
    target = tmp_path / "tiny.npz"
    target.write_bytes(b"existing checkpoint")
    assert adapter._ensure_weights(object(), target) == target
    assert target.read_bytes() == b"existing checkpoint"
    assert fake_engine[1] == []
    assert list(tmp_path.iterdir()) == [target]


def test_publish_uses_complete_private_sibling_and_removes_only_its_temp(tmp_path, fake_engine):
    engine, calls = fake_engine
    target = tmp_path / "new-dir" / "tiny.npz"
    save = engine.save_weights

    def observing_save(path, weights):
        assert path.parent == target.parent
        assert path.name != target.name and not target.exists()
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0
        save(path, weights)
        assert not target.exists()

    engine.save_weights = observing_save
    config = object()
    assert adapter._ensure_weights(config, target) == target
    assert calls[0] == ("generate", config, 0)
    assert target.read_bytes() == b"complete fake checkpoint"
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("step", ["generate", "save", "fsync", "link"])
def test_failed_initialization_never_publishes_partial_bytes_or_removes_others(
        tmp_path, fake_engine, monkeypatch, step):
    engine, _ = fake_engine
    target = tmp_path / "tiny.npz"
    unrelated = tmp_path / ".tiny.npz.other-owner.npz"
    unrelated.write_bytes(b"do not remove")

    def fail(*args, **kwargs):
        if step == "save":
            args[0].write_bytes(b"partial")
        raise OSError("injected " + step + " failure")

    if step in ("generate", "save"):
        monkeypatch.setattr(engine, step + "_weights", fail)
    else:
        monkeypatch.setattr(adapter.os, step, fail)
    with pytest.raises(OSError, match="injected"):
        adapter._ensure_weights(object(), target)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == [unrelated]
    assert unrelated.read_bytes() == b"do not remove"


def test_concurrently_published_checkpoint_is_never_replaced(tmp_path, fake_engine):
    engine, _ = fake_engine
    target = tmp_path / "tiny.npz"
    save = engine.save_weights

    def another_initializer_wins(path, weights):
        save(path, weights)
        target.write_bytes(b"valid existing winner")

    engine.save_weights = another_initializer_wins
    assert adapter._ensure_weights(object(), target) == target
    assert target.read_bytes() == b"valid existing winner"
    assert list(tmp_path.iterdir()) == [target]


def test_failed_writer_preserves_a_checkpoint_that_appeared_in_the_meantime(tmp_path, fake_engine):
    engine, _ = fake_engine
    target = tmp_path / "tiny.npz"

    def partial_save(path, weights):
        target.write_bytes(b"valid existing winner")
        path.write_bytes(b"partial")
        raise OSError("injected save failure")

    engine.save_weights = partial_save
    with pytest.raises(OSError, match="injected save failure"):
        adapter._ensure_weights(object(), target)
    assert target.read_bytes() == b"valid existing winner"
    assert list(tmp_path.iterdir()) == [target]
