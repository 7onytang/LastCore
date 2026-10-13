def test_agent_retries_with_capped_backoff_and_resets_after_success(monkeypatch, capsys):
    from app import agent as agent_module

    class StopAfterWaits:
        def __init__(self):
            self.delays = []
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, delay):
            self.delays.append(delay)
            if len(self.delays) == 5:
                self.stopped = True

    calls = 0

    def request_json(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls in (1, 2, 3, 6):
            raise RuntimeError("temporary connection failure")
        if args[1].endswith("/assignments"):
            return {"assignments": []}
        return {}

    monkeypatch.setattr(agent_module, "request_json", request_json)
    worker = agent_module.NodeAgent.__new__(agent_module.NodeAgent)
    worker.controller = "http://controller"
    worker.name = "test-node"
    worker.node_id = "node-id"
    worker.node_token = "node-token"
    worker.available_memory_mb = 1024
    worker.poll_seconds = 0.1
    worker.max_retry_seconds = 0.25
    worker._started = set()
    stop_event = StopAfterWaits()

    worker.run(stop_event)

    assert stop_event.delays == [0.1, 0.2, 0.25, 0.1, 0.1]
    assert calls == 6
    assert capsys.readouterr().out.count("temporary connection failure") == 4


def test_assignment_watcher_stops_after_controller_cancellation(monkeypatch):
    from app import agent as agent_module

    worker = agent_module.NodeAgent.__new__(agent_module.NodeAgent)
    worker.controller = "http://controller"
    worker.node_id = "node-id"
    worker.node_token = "node-token"
    worker.poll_seconds = 0.01
    worker.heartbeat_ttl_seconds = 30
    worker._last_controller_contact = agent_module.time.monotonic()

    calls = []

    def request_json(*args, **kwargs):
        calls.append(args[1])
        if args[1].endswith("/assignments"):
            return {"assignments": [{"id": "assignment-id", "task_status": "cancelled",
                                      "status": "cancelled", "deadline": agent_module.time.time() + 60}]}
        return {}

    monkeypatch.setattr(agent_module, "request_json", request_json)
    cancel_event = agent_module.threading.Event()
    done_event = agent_module.threading.Event()
    assignment = {"id": "assignment-id", "deadline": agent_module.time.time() + 60}

    worker._watch_assignment(assignment, agent_module.threading.Event(), cancel_event, done_event)

    assert cancel_event.is_set()
    assert calls == ["/v1/nodes/node-id/heartbeat", "/v1/nodes/node-id/assignments"]


def test_execute_does_not_report_completed_after_stop(monkeypatch):
    from app import agent as agent_module

    worker = agent_module.NodeAgent.__new__(agent_module.NodeAgent)
    reports = []
    worker._report = lambda assignment_id, status, **values: reports.append((status, values))
    stop_event = agent_module.threading.Event()
    stop_event.set()
    assignment = {
        "id": "assignment-id",
        "layer_start": 0,
        "layer_end": 100,
        "request": {"workload": "linear", "model_id": agent_module.MODEL_ID,
                    "input_vector": [1.0], "weight_seed": 1},
    }

    worker._execute(assignment, stop_event)

    assert [status for status, _ in reports] == ["running"]
