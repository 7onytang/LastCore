# Controller / Node Agent contract

These integration tests preserve the applicable coverage from PR #27, fixed
head `7458b15bc1ada999ed88f506c0c1baf25bb0a1ef`: registration, heartbeat, assignment
polling, running/completed reports and idempotent completion; 401/404 status and
error details; cancellation propagation; and heartbeat TTL failure details. A fifth
test covers issue #43's core protocol: a fresh agent and client reuse the same
private state directory without another registration, retain node identity/token,
send updated resource/acceptance fields, and do not log either credential.

From the repository root, create an isolated environment and install both modules'
declared test dependencies:

```sh
python3 -m venv /tmp/lastcore-contract-venv
/tmp/lastcore-contract-venv/bin/python -m pip install -r controller-server/requirements-dev.txt -r node-agent/requirements-dev.txt
/tmp/lastcore-contract-venv/bin/python -m pytest -q -c tools/contracts/pytest.ini tools/contracts/test_controller_node.py
```

The dedicated pytest configuration explicitly imports only `controller-server/`
and `node-agent/`. `controller-node-contract.yml` installs both dependency files
and runs on changes to either module, these tests, or that workflow. No environment
wide `PYTHONPATH`, inference package, model weights, or numpy dependency is needed.
CI runs the suite on Ubuntu, Windows, and macOS 15 with Python 3.12. These are real
runner operating systems using an in-process application and Fake executor; they
do not establish acceptance on the user's devices, GUI, or clean CLI installation.

The real `ControllerClient` sends HTTPX requests through a small transport bridge
to [FastAPI's TestClient](https://fastapi.tiangolo.com/tutorial/testing/), which
invokes the actual ASGI routes, validation, authentication, and temporary SQLite
store. Responses are produced by the application; the bridge does not mock them.
See also [HTTPX transports](https://www.python-httpx.org/advanced/transports/).
Socket binding is prohibited by the fixture. Lifespan startup/shutdown runs through
TestClient, and event-controlled Fake executors perform no model or task compute.

The tests drive NodeAgent poll steps directly instead of starting its continuous
heartbeat/watchdog loops. An injected clock checks the TTL boundary without waiting
five real seconds. Unlike the old loopback test, this verifies application and wire
contracts without verifying TCP, uvicorn, real worker execution, devices, or a
production deployment. The declared dependencies may currently emit Starlette's
HTTPX deprecation warning; this suite uses the existing pinned HTTPX contract.
