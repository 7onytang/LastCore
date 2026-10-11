import sys
from pathlib import Path

import httpx
import pytest

from lastcore_node_agent.controller import ControllerClient

REPO_ROOT = Path(__file__).resolve().parents[2]
for _sibling in ("inference-engine", "communication"):
    _candidate = REPO_ROOT / _sibling
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

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
