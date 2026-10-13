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
