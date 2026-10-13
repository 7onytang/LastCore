import json

import httpx
import pytest

from lastcore_node_agent.controller import ControllerClient, ControllerError


def make_client(handler):
    return ControllerClient("http://controller.test///",
                            transport=httpx.MockTransport(handler))


def test_register_sends_bearer_and_payload():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(201, json={"node_id": "n", "node_token": "t",
                                         "heartbeat_ttl_seconds": 30})

    client = make_client(handler)
    result = client.register("reg-token", {"name": "x"})
    client.close()
    assert seen["auth"] == "Bearer reg-token"
    assert result["node_id"] == "n"


def test_error_mapping_includes_status_and_detail():
    def handler(request):
        return httpx.Response(409, json={"detail": "assignment is terminal"})

    client = make_client(handler)
    with pytest.raises(ControllerError) as exc_info:
        client.report("n", "t", "a", "completed")
    client.close()
    assert exc_info.value.status_code == 409
    assert "terminal" in str(exc_info.value)


def test_error_mapping_falls_back_to_text():
    def handler(request):
        return httpx.Response(500, text="boom")

    client = make_client(handler)
    with pytest.raises(ControllerError) as exc_info:
        client.health()
    client.close()
    assert exc_info.value.status_code == 500
    assert "boom" in exc_info.value.detail


def test_assignments_returns_list():
    def handler(request):
        return httpx.Response(200, json={"assignments": [{"id": "a1"}]})

    client = make_client(handler)
    assert client.assignments("n", "t") == [{"id": "a1"}]
    client.close()


def test_report_body_contains_error():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = make_client(handler)
    client.report("n", "t", "a", "failed", "boom")
    client.close()
    assert seen["body"] == {"status": "failed", "error": "boom"}


def test_base_url_trailing_slash_normalized():
    def handler(request):
        return httpx.Response(200, json={"status": "ok"})

    client = make_client(handler)
    assert client.health() == {"status": "ok"}
    client.close()
