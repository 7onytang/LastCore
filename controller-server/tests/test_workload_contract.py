"""Pure contracts/Fake scheduling and fixture metadata; never start a Controller or compute task."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models import TaskRequest, NodeRegistration, Heartbeat, AssignmentReport
from app.store import Store
from app import store as store_module
from app.workload_downgrade import check

PI = "lastcore/pi-monte-carlo-v1"
LOG = "lastcore/log-statistics-v1"
BASE = dict(model_id=PI, workload="pi", num_layers=8, layer_memory_mb=1,
            total_samples=1000, weight_seed=7)


@pytest.mark.parametrize("changes", [
    {"weight_seed": None}, {"weight_seed": True}, {"weight_seed": -1}, {"weight_seed": 2**31},
    {"total_samples": None}, {"total_samples": True}, {"total_samples": 100_000_001},
    {"num_layers": 1001}, {"workload": "linear"}, {"model_id": "different"},
    {"log_lines": ["INFO"]}, {"input_vector": [1]},
])
def test_pi_incomplete_or_contradictory_contract_is_rejected(changes):
    with pytest.raises(ValidationError):
        TaskRequest(**(BASE | changes))


@pytest.mark.parametrize("changes", [
    {"num_layers": 2}, {"weight_seed": 7}, {"total_samples": 1000},
    {"workload": "pi"}, {"model_id": PI}, {"log_lines": ["x"*4097]},
    {"log_lines": ["x"*1001]*1000, "num_layers": 1000},
])
def test_log_cross_fields_and_payload_bounds_are_rejected(changes):
    with pytest.raises(ValidationError):
        TaskRequest(**(dict(model_id=LOG, workload="log_stats", num_layers=1,
                           layer_memory_mb=1, log_lines=["INFO"]) | changes))


def test_frozen_legacy_request_shape_and_capability_defaults():
    data = dict(model_id="lastcore/linear-demo-v1", num_layers=2, layer_memory_mb=1,
                max_nodes=1, input_vector=[1, 2], weight_seed=7,
                target_node_id="00000000-0000-0000-0000-000000000001")
    frozen = '{"model_id":"lastcore/linear-demo-v1","num_layers":2,"layer_memory_mb":1,"max_nodes":1,"timeout_seconds":600,"input_vector":[1.0,2.0],"weight_seed":7,"target_node_id":"00000000-0000-0000-0000-000000000001"}'
    assert TaskRequest(**data).protocol_dump_json() == frozen
    tiny = json.loads(TaskRequest(model_id="tiny", num_layers=2, layer_memory_mb=1).protocol_dump_json())
    assert set(tiny) == set(json.loads(frozen))
    assert Heartbeat(available_memory_mb=1).supported_workloads == []
    assert NodeRegistration(name="fixture", platform="macOS", endpoint="http://127.0.0.1:1",
                            available_memory_mb=1, compute_weight=1).supported_workloads == []


def test_only_explicit_workload_nodes_reach_fake_allocator_and_no_task_is_written(tmp_path, monkeypatch):
    database = tmp_path / "fixture.sqlite3"
    store = Store(str(database), 30, clock=lambda:100)
    with sqlite3.connect(database) as db:
        for name in ("old-linear", "explicit", "offline", "wrong-kind"):
            db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)",
                       (name, "fixture-hash", name, "same-platform", "http://127.0.0.1:1", 1024, 1, 1,
                        0 if name == "offline" else 100))
        db.executemany("INSERT INTO node_workloads VALUES (?,?)",
                       [("explicit", "pi"), ("offline", "pi"), ("wrong-kind", "log_stats")])
    def allocator(nodes, *args):
        assert [node["id"] for node in nodes] == ["explicit"]
        raise ValueError("fixture abort before creating any task")
    monkeypatch.setattr(store_module, "allocate", allocator)
    with pytest.raises(HTTPException, match="409"):
        store.create_task(TaskRequest(**BASE))
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert len(db.execute("PRAGMA table_info(nodes)").fetchall()) == 9
        assert len(db.execute("PRAGMA table_info(assignments)").fetchall()) == 8
        assert db.execute("SELECT COUNT(*) FROM assignment_results").fetchone()[0] == 0


def test_typed_missing_or_old_result_field_rejected_before_any_write():
    store = object.__new__(Store)
    assignment = dict(id="a", task_id="t", node_id="n", status="running", layer_start=0, layer_end=1)
    db = Mock()
    db.execute.return_value.fetchone.return_value = assignment
    @contextmanager
    def transaction():
        yield db
    store.transaction = transaction
    store._node_auth = Mock()
    store._task_view = Mock(return_value={"id":"t", "request":BASE, "assignments":[assignment]})
    for report in [AssignmentReport(status="completed"), AssignmentReport(status="completed", result=[.1])]:
        with pytest.raises(HTTPException) as error:
            store.report("n", "fixture", "a", report)
        assert error.value.status_code == 422
    assert not any("UPDATE" in call.args[0] or "INSERT" in call.args[0] for call in db.execute.call_args_list)


def test_typed_counts_are_strict_and_do_not_replace_linear_result():
    for result in [{"inside":True,"samples":1000}, {"inside":-1,"samples":1000}]:
        with pytest.raises(ValidationError):
            AssignmentReport(status="completed", workload_result=result)
    report = AssignmentReport(status="completed", workload_result={"inside":1,"samples":1000})
    assert report.result is None and report.workload_result["inside"] == 1


def test_read_only_downgrade_gate_uses_metadata_fixture(tmp_path):
    path = tmp_path / "gate.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks (request TEXT, status TEXT)")
        db.execute("INSERT INTO tasks VALUES (?,?)", (json.dumps(BASE), "queued"))
    before = path.read_bytes()
    assert check(path)["status"] == "blocked"
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tasks SET status='completed'")
    assert check(path)["status"] == "ready"
    assert check(tmp_path / "missing.sqlite3")["status"] == "error"


def test_old_core_reader_sees_no_dict_in_legacy_result_storage(tmp_path):
    import subprocess
    import types
    database=tmp_path/'compat.sqlite3'
    store=Store(str(database),30,clock=lambda:100)
    request=TaskRequest(**BASE).protocol_dump_json()
    with sqlite3.connect(database) as db:
        db.execute('INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)',
            ('fixture-node','fixture-hash','fixture','fixture','http://127.0.0.1:1',1024,1,1,100))
        db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)',('fixture-task',request,'completed',100,200,None))
        db.execute('INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?)',
            ('fixture-assignment','fixture-task','fixture-node',0,0,8,'completed',None))
        db.execute('INSERT INTO workload_results VALUES (?,?)',
            ('fixture-assignment',json.dumps({'inside':500,'samples':1000})))
    new=store.get_task('fixture-task')
    assert new['result'] is None and new['assignments'][0]['result'] is None
    assert new['workload_result']=={'inside':500,'samples':1000,'pi_estimate':2.0}
    old_module=types.ModuleType('app.frozen_old_store')
    old_source=subprocess.check_output(['git','show','60c58831edaa4de06fd144b4e79f5df216900911:controller-server/app/store.py'])
    exec(compile(old_source,'frozen-public-old-store','exec'),old_module.__dict__)
    old=old_module.Store(str(database),30,clock=lambda:100).get_task('fixture-task')
    assert old['result'] is None and old['assignments'][0]['result'] is None
    assert 'workload_result' not in old
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT COUNT(*) FROM assignment_results').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM workload_results').fetchone()[0]==1


def test_workload_route_is_distinct_and_inherits_strict_console_headers():
    from app.main import create_app, Settings
    app=create_app(Settings(admin_token='fixture-admin-'+'a'*32,registration_token='fixture-register-'+'b'*32),
                   store=Mock())
    handlers={route.path:route.endpoint for route in app.routes if route.path in ('/','/console','/workloads')}
    assert set(handlers)=={'/','/console','/workloads'}
    console=handlers['/console']()
    workload=handlers['/workloads']()
    assert console.headers==workload.headers
    assert "script-src 'self'" in workload.headers['Content-Security-Policy']
    assert 'unsafe-inline' not in workload.headers['Content-Security-Policy']
    assert 'HEAD' in next(route.methods for route in app.routes if route.path=='/workloads')
    assert len([route for route in app.routes if route.path=='/'])==1
    assert str(console.path).endswith('/ui/index.html')
    assert str(workload.path).endswith('/showcase.html')
