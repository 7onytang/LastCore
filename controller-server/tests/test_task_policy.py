"""Policy boundaries: SQLite/ASGI/Fake only, no model, executor or sockets."""
import ast
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from app.main import Settings, create_app
from app.models import TaskRequest, NodeRegistration
from app.store import Store
from app.inference_jobs import JobRequest, Jobs, MODEL_ID, MODEL_SHA256, BACKEND_COMMIT
from app.task_policy import TaskPolicy, PolicyError

ADMIN, REG = 'a' * 32, 'r' * 32
AUTH = {'Authorization': 'Bearer ' + ADMIN}
MANIFEST = Path(__file__).parents[1] / 'app/task-policy-v1.json'

@pytest.fixture
def scene(tmp_path):
    now = [1000.0]
    store = Store(str(tmp_path/'policy.sqlite3'), 30, clock=lambda: now[0])
    node = store.register(NodeRegistration(name='fixture', platform='fake', endpoint='http://fixture.invalid:1',
        available_memory_mb=10000, compute_weight=1, supported_workloads=['pi','log_stats']))
    client = TestClient(create_app(Settings(ADMIN, REG, real_jobs_enabled=True), store))
    yield client, store, node, now
    client.close()

def task(node, model='lastcore/linear-demo-v1', **extra):
    value = dict(model_id=model, num_layers=1, layer_memory_mb=1)
    if model == 'lastcore/linear-demo-v1':
        value.update(input_vector=[1.0], weight_seed=7, target_node_id=node['node_id'], max_nodes=1)
    value.update(extra)
    return value

def count(store):
    with store.transaction() as db:
        return tuple(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('tasks','inference_jobs'))

@pytest.mark.parametrize('extra,code', [
    ({'model_id':'foreign'},'MODEL_NOT_ALLOWED'), ({'model_id':'lastcore/tiny-cpu'},'MODEL_METADATA_UNAVAILABLE'),
    ({'workload':'shell'},'TASK_TYPE_UNSUPPORTED'),
    ({'workload':'pi','model_id':'foreign','total_samples':1000,'weight_seed':1},'MODEL_NOT_ALLOWED'),
    ({'command':'not executed'},'EXECUTABLE_FIELD_FORBIDDEN'), ({'executor':'module:factory'},'EXECUTABLE_FIELD_FORBIDDEN'),
    ({'code':'not evaluated'},'EXECUTABLE_FIELD_FORBIDDEN'),
    ({'model_url':'https://untrusted.invalid/model'},'MODEL_SOURCE_UNTRUSTED'),
    ({'model_path':'/untrusted/model'},'MODEL_SOURCE_UNTRUSTED'),
    ({'input_ref':'file:///private/data'},'REFERENCE_UNSUPPORTED'),
    ({'output_ref':'https://untrusted.invalid/out'},'REFERENCE_UNSUPPORTED'),
    ({'unknown':True},'REQUEST_INVALID'), ({'legacy_task_model_ids':True},'REQUEST_INVALID'),
    ({'policy_version':'bypass'},'REQUEST_INVALID'), ({'num_layers':100001},'BUDGET_REJECTED'),
    ({'layer_memory_mb':10000001},'BUDGET_REJECTED'), ({'timeout_seconds':86401},'BUDGET_REJECTED')])
def test_rejection_never_enters_queue(scene, extra, code):
    client, store, node, _ = scene
    response = client.post('/v1/tasks', headers=AUTH, json=task(node,model='arbitrary',**extra))
    assert response.status_code == 422 and response.json()['error_code'] == code
    assert 'detail' in response.json() and count(store) == (0,0)

def test_builtin_request_result_and_ack_are_unchanged(scene):
    client, store, node, _ = scene
    expected = json.loads(TaskRequest(**task(node)).protocol_dump_json())
    created = client.post('/v1/tasks', headers=AUTH, json=task(node))
    assert created.status_code == 201 and created.json()['request'] == expected
    saved = created.json()
    url = f"/v1/nodes/{node['node_id']}/assignments/{saved['assignments'][0]['id']}/report"
    headers = {'Authorization':'Bearer '+node['node_token']}
    first = client.post(url,headers=headers,json={'status':'completed','result':[.25]})
    second = client.post(url,headers=headers,json={'status':'completed','result':[.25]})
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert first.json()['result'] == [.25]
    assert client.post(url,headers=headers,json={'status':'completed','result':[.5]}).status_code == 409

@pytest.mark.parametrize('workload,fields',[('pi',{'total_samples':1000,'weight_seed':1}),
    ('log_stats',{'log_lines':['INFO shell text remains data']})])
def test_typed_algorithms_do_not_require_gguf(scene, workload, fields):
    client, _, node, _ = scene
    model = 'lastcore/pi-monte-carlo-v1' if workload == 'pi' else 'lastcore/log-statistics-v1'
    response = client.post('/v1/tasks',headers=AUTH,json=task(node,model=model,workload=workload,**fields))
    assert response.status_code == 201 and response.json()['request']['workload'] == workload

@pytest.mark.parametrize('legacy_id',['old-custom-id','lastcore/tiny-cpu',MODEL_ID])
def test_explicit_legacy_mode_does_not_allow_remote_code(scene, legacy_id):
    _, store, node, _ = scene
    client = TestClient(create_app(Settings(ADMIN,REG,legacy_task_model_ids=True),store))
    try:
        response = client.post('/v1/tasks',headers=AUTH,json=task(node,model=legacy_id))
        assert response.status_code == 201 and 'workload' not in response.json()['request']
        denied = client.post('/v1/tasks',headers=AUTH,json=task(node,model='other',command='no'))
        assert denied.status_code == 422 and denied.json()['error_code'] == 'EXECUTABLE_FIELD_FORBIDDEN'
    finally: client.close()

def test_historical_read_and_expired_identical_ack_remain_compatible(scene):
    client, store, node, now = scene
    old = store.create_task(TaskRequest(**task(node,model='historical',timeout_seconds=10)))
    now[0] += 11
    read = client.get('/v1/tasks/'+old['id'],headers=AUTH)
    assert read.status_code == 200 and read.json()['status'] == 'failed'
    url = f"/v1/nodes/{node['node_id']}/assignments/{old['assignments'][0]['id']}/report"
    headers = {'Authorization':'Bearer '+node['node_token']}
    rejected = client.post(url,headers=headers,json={'status':'running'})
    assert rejected.status_code == 409
    assert rejected.json() == {'detail':'assignment is terminal','error_code':'ASSIGNMENT_EXPIRED'}
    assert client.post(url,headers=headers,json={'status':'failed'}).status_code == 200

@pytest.mark.parametrize('field,value,code', [('model_id','foreign','MODEL_NOT_ALLOWED'),
    ('model_url','https://untrusted.invalid','MODEL_SOURCE_UNTRUSTED'),
    ('command','shell','EXECUTABLE_FIELD_FORBIDDEN'), ('max_tokens',65,'BUDGET_REJECTED'),
    ('prompt','x'*4097,'BUDGET_REJECTED'), ('execution_timeout_seconds',91,'BUDGET_REJECTED')])
def test_gguf_uses_its_own_policy(scene, field, value, code):
    client, store, node, _ = scene
    body = dict(worker_node_id=node['node_id'],prompt='fixture',idempotency_key='fixture-key-123456')
    body[field] = value
    response = client.post('/v1/inference/jobs',headers=AUTH,json=body)
    assert response.status_code == 422 and response.json()['error_code'] == code and count(store) == (0,0)

def test_idempotent_job_retry_precedes_readmission(scene):
    client, store, node, _ = scene
    capability = dict(model_sha256=MODEL_SHA256,backend_commit=BACKEND_COMMIT,backend_sha256='b'*64)
    url = f"/v1/inference/workers/{node['node_id']}/capability"
    assert client.post(url,headers={'Authorization':'Bearer '+node['node_token']},json=capability).status_code == 200
    body = dict(worker_node_id=node['node_id'],prompt='fixture',idempotency_key='fixture-key-123456')
    first = client.post('/v1/inference/jobs',headers=AUTH,json=body)
    assert first.status_code == 201
    denied = Mock();denied.admit.side_effect = AssertionError('existing identical jobs must not be re-admitted')
    assert Jobs(store).create(JobRequest(**body),policy=denied)['id'] == first.json()['id']
    assert client.post('/v1/inference/jobs',headers=AUTH,json=dict(body,prompt='different')).status_code == 409
    assert count(store) == (0,1)

@pytest.mark.parametrize('change',['budget','hash','duplicate','implementation','unsupported-budget'])
def test_manifest_missing_metadata_fails_closed(change):
    data = json.loads(MANIFEST.read_text(encoding='utf-8'))
    if change == 'budget': del data['profiles'][0]['limits']['input_values']
    if change == 'unsupported-budget': data['profiles'][0]['limits']['unimplemented_hard_rss'] = 512
    if change == 'hash': data['profiles'][3]['artifact_sha256'] = 'a'*64
    if change == 'duplicate': data['profiles'].append(deepcopy(data['profiles'][0]))
    if change == 'implementation': data['profiles'][0]['model_id'] = 'unimplemented-builtin'
    with pytest.raises(ValueError): TaskPolicy(manifest=data)

def test_cross_language_pins_and_unknown_hardware_metadata():
    policy = TaskPolicy();whole = policy.profiles[MODEL_ID]
    assert whole.artifact_sha256 == MODEL_SHA256 and whole.backend_commit == BACKEND_COMMIT
    assert whole.limits['max_tokens'] == 64 and whole.limits['output_bytes'] == 65536
    assert whole.native_hard_memory_limit_mb is None and whole.measured_peak_memory_mb is None
    assert not policy.profiles['lastcore/tiny-cpu'].executable
    root = Path(__file__).parents[2]
    constants = {n.targets[0].id:ast.literal_eval(n.value) for n in ast.parse(
        (root/'real-model-runtime/bootstrap.py').read_text(encoding='utf-8')).body
        if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and isinstance(n.value,ast.Constant)}
    assert constants['REVISION'] == whole.version and constants['COMMIT'] == whole.backend_commit
    android = (root/'android-app/app/src/main/java/ai/lastcore/android/CloudApi.java').read_text(encoding='utf-8')
    assert f'String MODEL = "{MODEL_ID}"' in android and MODEL_SHA256 in android and BACKEND_COMMIT in android

def test_legacy_flag_requires_explicit_valid_boolean(monkeypatch):
    monkeypatch.delenv('CONTROLLER_LEGACY_TASK_MODEL_IDS',raising=False)
    assert not Settings.from_env().legacy_task_model_ids
    monkeypatch.setenv('CONTROLLER_LEGACY_TASK_MODEL_IDS','true')
    assert Settings.from_env().legacy_task_model_ids
    monkeypatch.setenv('CONTROLLER_LEGACY_TASK_MODEL_IDS','typo')
    with pytest.raises(ValueError): Settings.from_env()
    with pytest.raises(ValueError): TaskPolicy('false')

@pytest.mark.parametrize('change', [{'num_layers':4097},{'max_nodes':2},{'input_vector':[1000001]}])
def test_builtin_profile_budget_has_specific_code(scene, change):
    client, store, node, _ = scene
    response = client.post('/v1/tasks',headers=AUTH,json=task(node,**change))
    assert response.status_code == 422 and response.json()['error_code'] == 'BUDGET_REJECTED'
    assert count(store) == (0,0)


def test_profile_budget_is_enforced_not_only_documented():
    data = json.loads(MANIFEST.read_text(encoding='utf-8'))
    data['profiles'][0]['limits']['timeout_seconds'] = 30
    policy = TaskPolicy(manifest=data)
    request = TaskRequest(**task({'node_id':'00000000-0000-0000-0000-000000000001'},timeout_seconds=31))
    with pytest.raises(PolicyError) as exc: policy.admit(request,'linear')
    assert exc.value.error_code == 'BUDGET_REJECTED'


@pytest.mark.parametrize('pin', ['model_sha256','backend_commit'])
def test_bad_capability_pin_never_registers_as_executable_worker(scene, pin):
    client, store, node, _ = scene
    payload = dict(model_sha256=MODEL_SHA256,backend_commit=BACKEND_COMMIT,backend_sha256='b'*64)
    payload[pin] = 'a'*(64 if pin == 'model_sha256' else 40)
    response = client.post(f"/v1/inference/workers/{node['node_id']}/capability",
        headers={'Authorization':'Bearer '+node['node_token']},json=payload)
    assert response.status_code == 422 and response.json()['error_code'] == 'MODEL_SOURCE_UNTRUSTED'
    assert store.nodes()[0]['accepting_tasks'] is True


def test_expired_whole_lease_does_not_bypass_stop_acknowledgement(scene):
    client, _, node, now = scene
    headers = {'Authorization':'Bearer '+node['node_token']}
    cap = dict(model_sha256=MODEL_SHA256,backend_commit=BACKEND_COMMIT,backend_sha256='b'*64)
    client.post(f"/v1/inference/workers/{node['node_id']}/capability",headers=headers,json=cap)
    job = client.post('/v1/inference/jobs',headers=AUTH,json=dict(worker_node_id=node['node_id'],
        prompt='fixture',idempotency_key='expired-key-123456')).json()
    claimed = client.post(f"/v1/inference/workers/{node['node_id']}/claim",headers=headers).json()['job']
    now[0] += 11
    lease = dict(node_id=node['node_id'],lease_epoch=claimed['lease_epoch'])
    response = client.post(f"/v1/inference/jobs/{job['id']}/renew",headers=headers,json=lease)
    assert response.status_code == 409
    assert response.json() == {'detail':'inference lease is terminal','error_code':'ASSIGNMENT_EXPIRED'}
    blocked = client.post(f"/v1/inference/workers/{node['node_id']}/claim",headers=headers).json()
    assert blocked['job'] is None and blocked['cleanup_required']['stop_acknowledged'] is False
    assert client.post(f"/v1/inference/jobs/{job['id']}/stop-ack",headers=headers,json=lease).status_code == 200

@pytest.mark.parametrize('extra',[{'unexpected':1},{'command':'not executed'},{'num_layers':100001}])
def test_validation_detail_matches_existing_strictmodel_behavior(scene, extra):
    from fastapi import FastAPI
    client, _, node, _ = scene
    reference = FastAPI()
    @reference.post('/task')
    def old_shape(request: TaskRequest): return {}
    payload = task(node,**extra)
    with TestClient(reference) as old:
        baseline = old.post('/task',json=payload)
    current = client.post('/v1/tasks',headers=AUTH,json=payload)
    assert current.status_code == baseline.status_code == 422
    assert current.json()['detail'] == baseline.json()['detail']

@pytest.mark.parametrize('literal',['Infinity','NaN','1e309'])
def test_nonfinite_budget_is_a_coded_rejection_not_server_error(scene, literal):
    client, store, node, _ = scene
    payload = json.dumps(task(node)).replace('"num_layers": 1', '"num_layers": '+literal)
    response = client.post('/v1/tasks',headers={**AUTH,'Content-Type':'application/json'},content=payload)
    assert response.status_code == 422 and response.json()['error_code'] == 'BUDGET_REJECTED'
    assert count(store) == (0,0)


def test_actual_idle_smoke_payload_is_admissible_without_legacy_mode(scene):
    client, store, node, _ = scene
    source = Path(__file__).parents[1]/'scripts/smoke_idle_expiry.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    call = next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
        and n.func.attr == 'post' and n.args and isinstance(n.args[0],ast.Constant)
        and n.args[0].value == '/v1/tasks')
    expression = next(k.value for k in call.keywords if k.arg == 'json')
    payload = {}
    for key,value in zip(expression.keys,expression.values):
        name = ast.literal_eval(key)
        if isinstance(value,ast.Subscript) and isinstance(value.value,ast.Name) and value.value.id == 'node':
            assert ast.literal_eval(value.slice) == 'node_id'
            payload[name] = node['node_id']
        else: payload[name] = ast.literal_eval(value)
    response = client.post('/v1/tasks',headers=AUTH,json=payload)
    assert response.status_code == 201 and count(store) == (1,0)
    assert response.json()['request']['model_id'] == 'lastcore/linear-demo-v1'


def test_actual_tiny_bundle_verifier_labels_and_explicitly_selects_compatibility(scene):
    client, store, _, _ = scene
    source = Path(__file__).parents[2]/'node-agent/desktop/verify_stage_bundle.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    call = next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
        and n.func.id == 'post' and n.args and isinstance(n.args[0],ast.Constant)
        and n.args[0].value == '/v1/tasks')
    payload = ast.literal_eval(call.args[1])
    strict = client.post('/v1/tasks',headers=AUTH,json=payload)
    assert strict.status_code == 422 and strict.json()['error_code'] == 'MODEL_METADATA_UNAVAILABLE'
    env = next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
        and n.func.id == 'dict' and any(k.arg == 'CONTROLLER_DATABASE_PATH' for k in n.keywords))
    assert next(ast.literal_eval(k.value) for k in env.keywords if k.arg == 'CONTROLLER_LEGACY_TASK_MODEL_IDS') == 'true'
    labels = [ast.literal_eval(v) for n in ast.walk(tree) if isinstance(n,ast.Dict)
        for k,v in zip(n.keys,n.values) if isinstance(k,ast.Constant) and k.value == 'task_policy_mode']
    assert labels == ['legacy-compatibility']
    compat = TestClient(create_app(Settings(ADMIN,REG,legacy_task_model_ids=True),store))
    try: assert compat.post('/v1/tasks',headers=AUTH,json=payload).status_code == 201
    finally: compat.close()


@pytest.mark.parametrize('output_bytes',[1,65535,65537])
def test_v1_manifest_cannot_advertise_an_unimplemented_output_budget(output_bytes):
    from app.inference_jobs import Result, RESULT_OUTPUT_BYTES
    assert RESULT_OUTPUT_BYTES == 65536
    # Existing result acceptance remains unchanged; smaller manifest values must
    # be rejected at startup rather than silently claiming this report is bounded.
    assert Result(text='xx',elapsed_seconds=0,model_sha256=MODEL_SHA256).text == 'xx'
    data = json.loads(MANIFEST.read_text(encoding='utf-8'))
    data['profiles'][3]['limits']['output_bytes'] = output_bytes
    with pytest.raises(ValueError,match='enforced result output limit'):
        TaskPolicy(manifest=data)


def test_documented_strict_request_is_admissible_with_an_actual_target(scene):
    client, store, node, _ = scene
    readme = (Path(__file__).parents[2]/'README.md').read_text(encoding='utf-8')
    section = readme.split('严格模式提交任务示例：',1)[1]
    payload = json.loads(section.split('```json',1)[1].split('```',1)[0])
    assert payload['target_node_id'] == '00000000-0000-0000-0000-000000000001'
    payload['target_node_id'] = node['node_id']
    response = client.post('/v1/tasks',headers=AUTH,json=payload)
    assert response.status_code == 201 and count(store) == (1,0)
    assert response.json()['request']['model_id'] == 'lastcore/linear-demo-v1'
