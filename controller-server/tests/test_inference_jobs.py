import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.store import Store
from app.inference_jobs import BACKEND_COMMIT, MODEL_SHA256

ADMIN='a'*40; REG='r'*40
CAP={'model_sha256':MODEL_SHA256,'backend_commit':BACKEND_COMMIT,'backend_sha256':'b'*64}

@pytest.fixture
def setup(tmp_path):
    clock=[1000.0]; store=Store(str(tmp_path/'controller.sqlite3'),30,clock=lambda:clock[0])
    app=create_app(Settings(ADMIN,REG,store.path,30,60,True),store)
    with TestClient(app) as client:
        node=client.post('/v1/nodes/register',headers={'Authorization':'Bearer '+REG},json={'name':'real-job-fixture','platform':'test','endpoint':'http://127.0.0.1:19100','available_memory_mb':2048,'compute_weight':1}).json()
        h={'Authorization':'Bearer '+node['node_token']}
        assert client.post('/v1/inference/workers/'+node['node_id']+'/capability',headers=h,json=CAP).status_code==200
        yield client,node,h,clock,store

def create(c,n,key='idempotent_job_0001',**kwargs):
    return c.post('/v1/inference/jobs',headers={'Authorization':'Bearer '+ADMIN},json={'worker_node_id':n['node_id'],'prompt':'What is the capital of France?','idempotency_key':key,**kwargs})

def claim(c,n,h):return c.post('/v1/inference/workers/'+n['node_id']+'/claim',headers=h).json()['job']
def lease(n,j):return {'node_id':n['node_id'],'lease_epoch':j['lease_epoch'],'execution_key':'e'*64}
def report(c,n,h,j,status,**kw):return c.post('/v1/inference/jobs/'+j['id']+'/report',headers=h,json=dict(lease(n,j),status=status,**kw))

def test_default_disabled_has_no_routes_or_new_tables(tmp_path):
    path=str(tmp_path/'db.sqlite3')
    with TestClient(create_app(Settings(ADMIN,REG,path))) as client:
        assert client.get('/v1/inference/workers',headers={'Authorization':'Bearer '+ADMIN}).status_code==404
    with closing(sqlite3.connect(path)) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'inference_%'").fetchall()

def test_auth_capability_and_utf8_budget(setup):
    c,n,h,_,_=setup
    assert c.get('/v1/inference/workers').status_code==401
    assert create(c,n,prompt='中'*1366).status_code==422
    assert create(c,n,max_tokens=65).status_code==422
    assert c.post('/v1/inference/workers/'+n['node_id']+'/capability',headers=h,json={**CAP,'model_sha256':'a'*64}).status_code==422
    assert create(c,n).status_code==201
    assert c.get('/v1/nodes',headers={'Authorization':'Bearer '+ADMIN}).json()['nodes'][0]['accepting_tasks'] is False

def test_concurrent_idempotency_and_conflict(setup):
    c,n,h,_,_=setup
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses=list(pool.map(lambda _:create(c,n),range(5)))
    assert all(r.status_code==201 for r in responses)
    assert len({r.json()['id'] for r in responses})==1
    assert create(c,n,execution_timeout_seconds=60.0).json()['id']==responses[0].json()['id']
    assert create(c,n,prompt='A different request').status_code==409

def test_claim_one_job_retry_no_duplicate_and_success_retry(setup):
    c,n,h,_,_=setup
    first=create(c,n).json();second=create(c,n,'idempotent_job_0002').json()
    j=claim(c,n,h);assert j['id']==first['id'];assert claim(c,n,h)['lease_epoch']==j['lease_epoch']==1
    assert report(c,n,h,j,'running').status_code==200
    result={'text':'Paris','elapsed_seconds':.2,'model_sha256':MODEL_SHA256}
    assert report(c,n,h,j,'succeeded',result=result).status_code==200
    assert report(c,n,h,j,'succeeded',result=result).status_code==200
    assert report(c,n,h,j,'succeeded',result={**result,'text':'changed'}).status_code==409
    assert claim(c,n,h)['id']==second['id']

def test_cancel_ack_keeps_capacity_and_rejects_late_success(setup):
    c,n,h,_,_=setup
    create(c,n);second=create(c,n,'idempotent_job_0002').json();j=claim(c,n,h)
    cancelled=c.post('/v1/inference/jobs/'+j['id']+'/cancel',headers={'Authorization':'Bearer '+ADMIN}).json()
    assert cancelled['status']=='cancelling' and not cancelled['stop_acknowledged']
    assert claim(c,n,h)['id']==j['id']
    assert report(c,n,h,j,'succeeded',result={'text':'late','elapsed_seconds':.1,'model_sha256':MODEL_SHA256}).status_code==409
    ack=report(c,n,h,j,'cancelled');assert ack.json()['stop_acknowledged'];assert ack.json()['status']=='cancelled'
    assert report(c,n,h,j,'cancelled').status_code==200
    assert claim(c,n,h)['id']==second['id']

def test_lease_expiry_fences_and_never_reexecutes(setup):
    c,n,h,clock,store=setup
    create(c,n);j=claim(c,n,h);clock[0]+=11;store.expire()
    assert claim(c,n,h) is None
    assert report(c,n,h,j,'running').status_code==409
    state=c.get('/v1/inference/jobs/'+j['id'],headers={'Authorization':'Bearer '+ADMIN}).json()
    assert state['status']=='failed' and state['error_code']=='lease_lost' and not state['stop_acknowledged']

def test_wrong_epoch_and_identity_rejected(setup):
    c,n,h,_,_=setup
    create(c,n);j=claim(c,n,h)
    assert c.post('/v1/inference/jobs/'+j['id']+'/renew',headers=h,json={**lease(n,j),'lease_epoch':2}).status_code==409
    other=c.post('/v1/nodes/register',headers={'Authorization':'Bearer '+REG},json={'name':'other','platform':'test','endpoint':'http://127.0.0.1:19101','available_memory_mb':2048,'compute_weight':1}).json()
    assert c.post('/v1/inference/jobs/'+j['id']+'/renew',headers={'Authorization':'Bearer '+other['node_token']},json=lease(n,j)).status_code==401
    assert c.post('/v1/inference/jobs/'+j['id']+'/renew',headers={'Authorization':'Bearer '+other['node_token']},json={**lease(n,j),'node_id':other['node_id']}).status_code==409

def test_cancel_lease_expiry_stays_explicitly_unacknowledged(setup):
    c,n,h,clock,_=setup
    create(c,n);j=claim(c,n,h);c.post('/v1/inference/jobs/'+j['id']+'/cancel',headers={'Authorization':'Bearer '+ADMIN})
    clock[0]+=11
    state=c.get('/v1/inference/jobs/'+j['id'],headers={'Authorization':'Bearer '+ADMIN}).json()
    assert state['status']=='cancelled' and state['error_code']=='cancel_lease_expired' and not state['stop_acknowledged']

def test_backup_recovery_preserves_request_result_and_idempotency(setup,tmp_path):
    c,n,h,_,store=setup
    create(c,n);j=claim(c,n,h)
    assert report(c,n,h,j,'running').status_code==200
    result={'text':'Paris','elapsed_seconds':.2,'model_sha256':MODEL_SHA256}
    report(c,n,h,j,'succeeded',result=result)
    target=tmp_path/'recovered.sqlite3'
    from app.backup import backup_database
    assert backup_database(store.path,target)==target.resolve()
    recovered=Store(str(target),30,clock=lambda:1000)
    with TestClient(create_app(Settings(ADMIN,REG,str(target),30,60,True),recovered)) as rc:
        assert create(rc,n).json()['id']==j['id']
        assert rc.get('/v1/inference/jobs/'+j['id'],headers={'Authorization':'Bearer '+ADMIN}).json()['result']==result
        assert claim(rc,n,h) is None
    with closing(sqlite3.connect(target)) as db:assert not db.execute('PRAGMA foreign_key_check').fetchall()

def test_expired_worker_cannot_take_next_job_without_cleanup_ack(setup):
    c,n,h,clock,_=setup
    create(c,n);second=create(c,n,'idempotent_job_0002').json();j=claim(c,n,h)
    clock[0]+=11
    blocked=c.post('/v1/inference/workers/'+n['node_id']+'/claim',headers=h).json()
    assert blocked['job'] is None and blocked['cleanup_required']['id']==j['id']
    ack=c.post('/v1/inference/jobs/'+j['id']+'/stop-ack',headers=h,json=lease(n,j))
    assert ack.json()['stop_acknowledged']
    next_job=claim(c,n,h);assert next_job['id']==second['id'] and next_job['lease_epoch']>j['lease_epoch']
    # Old cleanup retries cannot release the newer busy worker reservation.
    assert c.post('/v1/inference/jobs/'+j['id']+'/stop-ack',headers=h,json=lease(n,j)).status_code==200
    workers=c.get('/v1/inference/workers',headers={'Authorization':'Bearer '+ADMIN}).json()['workers']
    assert not workers[0]['ready'] and workers[0]['current_job_id']==next_job['id']

def test_whole_worker_cannot_become_eligible_for_legacy_tasks(setup):
    c,n,h,_,_=setup
    heartbeat=c.post('/v1/nodes/'+n['node_id']+'/heartbeat',headers=h,json={'available_memory_mb':2048,'accepting_tasks':True})
    assert heartbeat.json()['accepting_tasks'] is False
    legacy=c.post('/v1/tasks',headers={'Authorization':'Bearer '+ADMIN},json={'model_id':'lastcore/linear-demo-v1','num_layers':1,'layer_memory_mb':64,'max_nodes':1,'target_node_id':n['node_id'],'input_vector':[1.0],'weight_seed':0})
    assert legacy.status_code==409

def test_atomic_nonlegacy_registration_and_single_execution_owner(setup):
    c,n,h,_,_=setup
    inert=c.post('/v1/nodes/register',headers={'Authorization':'Bearer '+REG},json={'name':'inert-registration','platform':'test','endpoint':'http://127.0.0.1:19102','available_memory_mb':512,'compute_weight':1,'accepting_tasks':False}).json()
    view=c.get('/v1/nodes',headers={'Authorization':'Bearer '+ADMIN}).json()['nodes']
    assert next(x for x in view if x['id']==inert['node_id'])['accepting_tasks'] is False
    create(c,n);j=claim(c,n,h)
    assert report(c,n,h,j,'running').status_code==200
    other=c.post('/v1/inference/jobs/'+j['id']+'/report',headers=h,json={**lease(n,j),'execution_key':'f'*64,'status':'running'})
    assert other.status_code==409
    assert 'execution_hash' not in c.get('/v1/inference/jobs/'+j['id'],headers={'Authorization':'Bearer '+ADMIN}).json()
