import httpx
import pytest

from lastcore_node_agent.controller import ControllerClient

from tests.helpers import FakeController


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def fake(clock):
    return FakeController(clock)


@pytest.fixture
def client(fake):
    client = ControllerClient("http://controller.test",
                              transport=httpx.MockTransport(fake.handler))
    yield client
    client.close()
