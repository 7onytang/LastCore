"""Isolated, bounded single-worker real-model jobs. No layer distribution or retries."""
import hashlib
import hmac
import json
from contextlib import contextmanager
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, field_validator

from .models import StrictModel
from .task_policy import PolicyError

MODEL_ID = 'SmolLM2-135M-Instruct-Q4_K_M'
MODEL_REVISION = '04f7f8a05a8e4bc208e88d11e8c9122be36c2f9b'
MODEL_SHA256 = 'bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169'
BACKEND_COMMIT = '8345f333951c661d166b00e6f9362e553768f292'
LEASE_SECONDS = 10
ACTIVE = ('leased', 'running', 'cancelling')


class Capability(StrictModel):
    model_id: Literal['SmolLM2-135M-Instruct-Q4_K_M'] = MODEL_ID
    model_sha256: Literal['bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169']
    backend_commit: Literal['8345f333951c661d166b00e6f9362e553768f292']
    backend_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class JobRequest(StrictModel):
    worker_node_id: str = Field(min_length=1, max_length=128)
    model_id: Literal['SmolLM2-135M-Instruct-Q4_K_M'] = MODEL_ID
    prompt: str = Field(min_length=1, max_length=4096)
    max_tokens: int = Field(default=64, ge=1, le=64)
    timeout_seconds: int = Field(default=90, ge=10, le=90)
    execution_timeout_seconds: float = Field(default=60.0, ge=0.01, le=90, allow_inf_nan=False)
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r'^[a-zA-Z0-9_-]+$')

    @field_validator('prompt')
    @classmethod
    def prompt_bytes(cls, value):
        if not value.strip() or len(value.encode('utf-8')) > 4096:
            raise ValueError('prompt exceeds 4096 UTF-8 bytes')
        return value


class LeaseRequest(StrictModel):
    node_id: str = Field(min_length=1, max_length=128)
    lease_epoch: int = Field(ge=1)
    execution_key: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class Result(StrictModel):
    text: str = Field(max_length=65536)
    elapsed_seconds: float = Field(ge=0, le=90, allow_inf_nan=False)
    model_sha256: Literal['bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169']

    @field_validator('text')
    @classmethod
    def result_bytes(cls, value):
        if len(value.encode('utf-8')) > 65536:
            raise ValueError('result exceeds 65536 UTF-8 bytes')
        return value


class Report(LeaseRequest):
    status: Literal['running', 'succeeded', 'failed', 'cancelled']
    result: Result | None = None
    error_code: Literal['backend_error', 'deadline', 'worker_restart', 'lease_lost'] | None = None

    @field_validator('result')
    @classmethod
    def result_shape(cls, value, info):
        if info.data.get('status') != 'succeeded' and value is not None:
            raise ValueError('result only allowed for success')
        return value


class Jobs:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS inference_workers (
                    node_id TEXT PRIMARY KEY REFERENCES nodes(id), capability TEXT NOT NULL,
                    ready INTEGER NOT NULL DEFAULT 1, lease_epoch INTEGER NOT NULL DEFAULT 0,
                    current_job_id TEXT REFERENCES inference_jobs(id)
                );
                CREATE TABLE IF NOT EXISTS inference_jobs (
                    id TEXT PRIMARY KEY, worker_node_id TEXT NOT NULL REFERENCES nodes(id),
                    idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
                    request TEXT NOT NULL, status TEXT NOT NULL
                    CHECK(status IN ('queued','leased','running','cancelling','succeeded','failed','cancelled')),
                    created_at REAL NOT NULL, deadline REAL NOT NULL,
                    lease_epoch INTEGER NOT NULL DEFAULT 0, lease_expires REAL,
                    result TEXT, error_code TEXT, stop_acknowledged INTEGER NOT NULL DEFAULT 0,
                    execution_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS inference_jobs_worker ON inference_jobs(worker_node_id,status);
            ''')

    def _expire(self, db):
        now = self.store.clock()
        db.execute("UPDATE inference_jobs SET status='cancelled',error_code='cancel_lease_expired' WHERE status='cancelling' AND (lease_expires<=? OR deadline<=?)", (now, now))
        db.execute("UPDATE inference_jobs SET status='failed',error_code=CASE WHEN deadline<=? THEN 'deadline' ELSE 'lease_lost' END WHERE status IN ('queued','leased','running') AND (deadline<=? OR (lease_expires IS NOT NULL AND lease_expires<=?))", (now, now, now))

    @contextmanager
    def transaction(self):
        with self.store.transaction() as db:
            self._expire(db)
            # Expiry survives a subsequent failed authentication/invalid action.
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            yield db

    def expire(self):
        with self.transaction():
            pass

    def _get(self, db, job_id):
        row = db.execute('SELECT * FROM inference_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'inference job not found')
        return row

    def _view(self, row):
        result = dict(row)
        result.pop('request_hash')
        result.pop('idempotency_key')
        result.pop('execution_hash')
        result['request'] = json.loads(result['request'])
        result['result'] = json.loads(result['result']) if result['result'] else None
        result['stop_acknowledged'] = bool(result['stop_acknowledged'])
        result['server_time'] = self.store.clock()
        result['cancel_requested'] = result['status'] in ('cancelling','cancelled')
        return result

    def _node(self, db, node_id, token):
        return self.store._node_auth(db, node_id, token)

    def capability(self, node_id, token, capability):
        with self.transaction() as db:
            self._node(db, node_id, token)
            busy = db.execute("SELECT 1 FROM assignments a JOIN tasks t ON t.id=a.task_id WHERE a.node_id=? AND t.status IN ('queued','running')", (node_id,)).fetchone()
            if busy:
                raise HTTPException(409, 'node has an active legacy assignment')
            db.execute('INSERT INTO inference_workers(node_id,capability) VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET capability=excluded.capability', (node_id, capability.model_dump_json()))
            db.execute('UPDATE nodes SET accepting_tasks=0,last_seen=? WHERE id=?', (self.store.clock(), node_id))
            return {'node_id': node_id, 'capability': capability.model_dump(), 'single_worker_only': True}

    def workers(self):
        with self.transaction() as db:
            return [{'node_id': row['node_id'], 'capability': json.loads(row['capability']), 'online': row['last_seen'] > self.store.clock() - self.store.ttl,
                     'ready': bool(row['ready']), 'current_job_id': row['current_job_id']}
                    for row in db.execute('SELECT w.*,n.last_seen FROM inference_workers w JOIN nodes n ON n.id=w.node_id').fetchall()]

    def create(self, request, policy=None):
        data = request.model_dump()
        key = data.pop('idempotency_key')
        canonical = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        request_hash = hashlib.sha256(canonical.encode()).hexdigest()
        with self.transaction() as db:
            old = db.execute('SELECT * FROM inference_jobs WHERE idempotency_key=?', (key,)).fetchone()
            if old:
                if old['request_hash'] != request_hash:
                    raise HTTPException(409, 'idempotency key has a different request')
                return self._view(old)
            if policy is not None:
                policy.admit(request, 'whole_model')
            node = db.execute('SELECT n.* FROM inference_workers w JOIN nodes n ON n.id=w.node_id WHERE node_id=?', (request.worker_node_id,)).fetchone()
            if node is None or node['last_seen'] <= self.store.clock() - self.store.ttl:
                raise HTTPException(409, 'compatible worker is not online')
            now = self.store.clock()
            identifier = str(uuid4())
            db.execute("INSERT INTO inference_jobs(id,worker_node_id,idempotency_key,request_hash,request,status,created_at,deadline) VALUES (?,?,?,?,?,'queued',?,?)", (identifier, request.worker_node_id, key, request_hash, canonical, now, now + request.timeout_seconds))
            return self._view(self._get(db, identifier))

    def get(self, job_id):
        with self.transaction() as db:
            return self._view(self._get(db, job_id))

    def claim(self, node_id, token):
        with self.transaction() as db:
            self._node(db, node_id, token)
            worker = db.execute('SELECT * FROM inference_workers WHERE node_id=?', (node_id,)).fetchone()
            if worker is None:
                raise HTTPException(409, 'worker capability required')
            db.execute('UPDATE nodes SET accepting_tasks=0,last_seen=? WHERE id=?', (self.store.clock(), node_id))
            row = db.execute("SELECT * FROM inference_jobs WHERE worker_node_id=? AND status IN ('leased','running','cancelling') ORDER BY created_at,rowid LIMIT 1", (node_id,)).fetchone()
            if row:
                return {'job': self._view(row)}
            if not worker['ready']:
                return {'job': None, 'cleanup_required': self._view(self._get(db, worker['current_job_id']))}
            row = db.execute("SELECT * FROM inference_jobs WHERE worker_node_id=? AND status='queued' ORDER BY created_at,rowid LIMIT 1", (node_id,)).fetchone()
            if row is None:
                return {'job': None}
            expires = min(self.store.clock() + LEASE_SECONDS, row['deadline'])
            epoch = worker['lease_epoch'] + 1
            db.execute("UPDATE inference_jobs SET status='leased',lease_epoch=?,lease_expires=? WHERE id=?", (epoch, expires, row['id']))
            db.execute('UPDATE inference_workers SET ready=0,lease_epoch=?,current_job_id=? WHERE node_id=?', (epoch, row['id'], node_id))
            return {'job': self._view(self._get(db, row['id']))}

    def _lease(self, db, job_id, request, token):
        self._node(db, request.node_id, token)
        row = self._get(db, job_id)
        if row['worker_node_id'] != request.node_id or row['lease_epoch'] != request.lease_epoch:
            raise HTTPException(409, 'stale or foreign inference lease')
        if row['execution_hash'] and (not request.execution_key or not hmac.compare_digest(row['execution_hash'], hashlib.sha256(request.execution_key.encode()).hexdigest())):
            raise HTTPException(409, 'execution ownership does not match')
        return row

    def renew(self, job_id, request, token):
        with self.transaction() as db:
            row = self._lease(db, job_id, request, token)
            if row['status'] not in ACTIVE:
                if row['error_code'] in ('deadline', 'lease_lost', 'cancel_lease_expired'):
                    raise PolicyError(409, 'inference lease is terminal', 'ASSIGNMENT_EXPIRED')
                raise HTTPException(409, 'inference lease is terminal')
            db.execute('UPDATE nodes SET last_seen=?,accepting_tasks=0 WHERE id=?', (self.store.clock(), request.node_id))
            if row['status'] != 'cancelling':
                db.execute('UPDATE inference_jobs SET lease_expires=? WHERE id=?', (min(self.store.clock() + LEASE_SECONDS, row['deadline']), job_id))
            return self._view(self._get(db, job_id))

    def report(self, job_id, request, token):
        if request.status == 'succeeded' and request.result is None:
            raise HTTPException(422, 'success requires a result')
        payload = request.result.model_dump_json() if request.result else None
        with self.transaction() as db:
            row = self._lease(db, job_id, request, token)
            if row['status'] == request.status and row['result'] == payload and row['error_code'] == request.error_code:
                return self._view(row)
            if row['status'] not in ACTIVE:
                if row['error_code'] in ('deadline', 'lease_lost', 'cancel_lease_expired'):
                    raise PolicyError(409, 'inference job is terminal', 'ASSIGNMENT_EXPIRED')
                raise HTTPException(409, 'inference job is terminal')
            if row['status'] == 'cancelling' and request.status != 'cancelled':
                raise HTTPException(409, 'cancellation acknowledgment required')
            if request.status == 'cancelled' and row['status'] != 'cancelling':
                raise HTTPException(409, 'job has not been cancelled')
            if request.status == 'running' and row['status'] == 'leased':
                if not request.execution_key:
                    raise HTTPException(422, 'persist an execution key before starting')
                db.execute('UPDATE inference_jobs SET execution_hash=? WHERE id=?', (hashlib.sha256(request.execution_key.encode()).hexdigest(), job_id))
            if request.status == 'succeeded' and row['status'] != 'running':
                raise HTTPException(409, 'execution has not started')
            db.execute('UPDATE inference_jobs SET status=?,result=?,error_code=?,stop_acknowledged=? WHERE id=?', (request.status, payload, request.error_code, int(request.status in ('succeeded','failed','cancelled')), job_id))
            if request.status != 'running':
                db.execute('UPDATE inference_workers SET ready=1,current_job_id=NULL WHERE node_id=? AND current_job_id=? AND lease_epoch=?', (request.node_id, job_id, request.lease_epoch))
            return self._view(self._get(db, job_id))

    def stop_ack(self, job_id, request, token):
        """Acknowledge local cleanup even after the lease expired; never accept a late result."""
        with self.transaction() as db:
            row = self._lease(db, job_id, request, token)
            if row['status'] not in ('failed', 'cancelled', 'cancelling'):
                raise HTTPException(409, 'job does not require a stop acknowledgment')
            db.execute("UPDATE inference_jobs SET status=CASE WHEN status='cancelling' THEN 'cancelled' ELSE status END,stop_acknowledged=1 WHERE id=?", (job_id,))
            db.execute('UPDATE inference_workers SET ready=1,current_job_id=NULL WHERE node_id=? AND current_job_id=? AND lease_epoch=?', (request.node_id, job_id, request.lease_epoch))
            return self._view(self._get(db, job_id))

    def cancel(self, job_id):
        with self.transaction() as db:
            row = self._get(db, job_id)
            if row['status'] == 'queued':
                db.execute("UPDATE inference_jobs SET status='cancelled',stop_acknowledged=1 WHERE id=?", (job_id,))
            elif row['status'] in ('leased','running'):
                db.execute("UPDATE inference_jobs SET status='cancelling' WHERE id=?", (job_id,))
            return self._view(self._get(db, job_id))


def router(jobs, credential, require_admin, policy):
    api = APIRouter(prefix='/v1/inference')
    Token = Annotated[str, Depends(credential)]

    @api.get('/workers', dependencies=[Depends(require_admin)])
    def workers():
        return {'workers': jobs.workers()}

    @api.post('/workers/{node_id}/capability')
    def capability(node_id: str, request: Capability, token: Token):
        return jobs.capability(node_id, token, request)

    @api.post('/workers/{node_id}/claim')
    def claim(node_id: str, token: Token):
        return jobs.claim(node_id, token)

    @api.post('/jobs', status_code=201, dependencies=[Depends(require_admin)])
    def create(request: JobRequest):
        return jobs.create(request, policy=policy)

    @api.get('/jobs/{job_id}', dependencies=[Depends(require_admin)])
    def get(job_id: str):
        return jobs.get(job_id)

    @api.post('/jobs/{job_id}/cancel', dependencies=[Depends(require_admin)])
    def cancel(job_id: str):
        return jobs.cancel(job_id)

    @api.post('/jobs/{job_id}/renew')
    def renew(job_id: str, request: LeaseRequest, token: Token):
        return jobs.renew(job_id, request, token)

    @api.post('/jobs/{job_id}/report')
    def report(job_id: str, request: Report, token: Token):
        return jobs.report(job_id, request, token)

    @api.post('/jobs/{job_id}/stop-ack')
    def stop_ack(job_id: str, request: LeaseRequest, token: Token):
        return jobs.stop_ack(job_id, request, token)

    return api
