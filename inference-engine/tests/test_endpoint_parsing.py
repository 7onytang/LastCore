"""Pure endpoint parsing regressions recovered from PR #7; no sockets or models."""
import pytest

from inference_engine import assignment_plan, parse_endpoint
from inference_engine.errors import ShardError


@pytest.mark.parametrize("value,expected", [
    ("node.local:9000", ("node.local", 9000)),
    ("http://10.0.0.12:9000/", ("10.0.0.12", 9000)),
    ("https://node.local:9000/", ("node.local", 9000)),
    ("tcp://node.local:9000/", ("node.local", 9000)),
    ("//node.local:9000/", ("node.local", 9000)),
    ("  node.local:9000/  ", ("node.local", 9000)),
    ("127.0.0.1:0", ("127.0.0.1", 0)),
    ("node.local:65535", ("node.local", 65535)),
    ("http://[::1]:9000/", ("::1", 9000)),
    ("[2001:db8::1]:9000", ("2001:db8::1", 9000)),
    ("::1:9000", ("::1", 9000)),
    ("http://::1:9000/", ("::1", 9000)),
])
def test_tcp_address_forms_are_preserved(value, expected):
    assert parse_endpoint(value) == expected


@pytest.mark.parametrize("value", [
    "", None, 9000, "node.local", "node.local:", "node.local:port",
    "node.local:-1", "node.local:65536", "http://:9000/",
    "http://[::1:9000/", "[not-ipv6]:9000", "host:9000/trailing",
    "http://host:9000/?token=secret", "http://host:9000/#route",
    "http://user:secret@host:9000/", "http://ho st:9000/",
    "http://host:90\n00/", "http://host:9000/path:9001",
])
def test_unusable_or_ambiguous_endpoint_components_are_rejected(value):
    with pytest.raises(ShardError):
        parse_endpoint(value)


@pytest.mark.parametrize("peer", ["previous_node", "next_node"])
def test_controller_root_slash_is_accepted_in_raw_assignment(peer):
    raw = {"task_id": "task", "id": "assignment", "layer_start": 1,
           "layer_end": 2, "request": {"num_layers": 4},
           peer: {"endpoint": "http://127.0.0.1:9000/"}}
    plan = assignment_plan(raw)
    assert getattr(plan, peer.removesuffix("_node") + "_endpoint") == ("127.0.0.1", 9000)
    assert raw[peer]["endpoint"].endswith("/")


def test_rejected_credentials_are_not_echoed_in_the_error():
    with pytest.raises(ShardError) as error:
        parse_endpoint("http://user:secret@host:9000/")
    assert "secret" not in str(error.value)
