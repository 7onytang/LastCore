import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from .scheduler import allocate


ACTIVE = ("queued", "running")


class Store:
    def __init__(self, path: str, heartbeat_ttl: int, clock=time.time):
        self.path = path
        self.ttl = heartbeat_ttl
        self.clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, name TEXT NOT NULL,
                    platform TEXT NOT NULL, endpoint TEXT NOT NULL,
                    available_memory_mb INTEGER NOT NULL, compute_weight REAL NOT NULL,
                    accepting_tasks INTEGER NOT NULL, last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, deadline REAL NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    node_id TEXT NOT NULL REFERENCES nodes(id), position INTEGER NOT NULL,
                    layer_start INTEGER NOT NULL, layer_end INTEGER NOT NULL,
                    status TEXT NOT NULL, error TEXT,
                    UNIQUE(task_id, position)
                );
                CREATE INDEX IF NOT EXISTS assignments_node ON assignments(node_id);
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            # Persist expiry even when the subsequent request is rejected.
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _end_task(self, db, task_id, status, error=None):
        db.execute("UPDATE tasks SET status=?, error=? WHERE id=?", (status, error, task_id))
        db.execute("UPDATE assignments SET status=?, error=? WHERE task_id=? AND status IN ('pending','running')",
                   ("cancelled" if status == "cancelled" else "failed", error, task_id))

    def _expire(self, db):
        now = self.clock()
        rows = db.execute("""
            SELECT DISTINCT t.id, t.deadline FROM tasks t
            JOIN assignments a ON a.task_id=t.id JOIN nodes n ON n.id=a.node_id
            WHERE t.status IN ('queued','running') AND (t.deadline <= ? OR n.last_seen <= ?)
        """, (now, now - self.ttl)).fetchall()
        for row in rows:
            error = "task deadline exceeded" if row["deadline"] <= now else "assigned node heartbeat expired"
            self._end_task(db, row["id"], "failed", error)

    def expire(self):
        """Persist overdue task and node expiry without requiring API traffic."""
        with self.transaction():
            pass

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _node_auth(self, db, node_id, token):
        node = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if node is None or not hmac.compare_digest(node["token_hash"], self._hash(token)):
            raise HTTPException(401, "invalid node credential")
        return node

    def _node_view(self, db, node):
        data = dict(node)
        data.pop("token_hash")
        data["accepting_tasks"] = bool(data["accepting_tasks"])
        busy = db.execute("""SELECT 1 FROM assignments a JOIN tasks t ON t.id=a.task_id
                             WHERE a.node_id=? AND t.status IN ('queued','running') LIMIT 1""",
                          (node["id"],)).fetchone()
        data["status"] = ("offline" if node["last_seen"] <= self.clock() - self.ttl
                          else "busy" if busy else "available" if node["accepting_tasks"] else "draining")
        return data

    def register(self, request):
        node_id, token = str(uuid4()), secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)",
                       (node_id, self._hash(token), request.name, request.platform, str(request.endpoint),
                        request.available_memory_mb, request.compute_weight, 1, self.clock()))
        return {"node_id": node_id, "node_token": token, "heartbeat_ttl_seconds": self.ttl}

    def nodes(self):
        with self.transaction() as db:
            return [self._node_view(db, n) for n in db.execute("SELECT * FROM nodes ORDER BY id").fetchall()]

    def heartbeat(self, node_id, token, request):
        with self.transaction() as db:
            self._node_auth(db, node_id, token)
            db.execute("UPDATE nodes SET available_memory_mb=?, accepting_tasks=?, last_seen=? WHERE id=?",
                       (request.available_memory_mb, request.accepting_tasks, self.clock(), node_id))
            return self._node_view(db, db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())

    def _task_view(self, db, task_id):
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise HTTPException(404, "task not found")
        result = dict(task)
        result["request"] = json.loads(result["request"])
        assignments = [dict(a) for a in db.execute("""
            SELECT a.*, n.endpoint FROM assignments a JOIN nodes n ON n.id=a.node_id
            WHERE a.task_id=? ORDER BY a.position
        """, (task_id,)).fetchall()]
        for i, assignment in enumerate(assignments):
            assignment["previous_node"] = self._peer(assignments[i-1]) if i else None
            assignment["next_node"] = self._peer(assignments[i+1]) if i+1 < len(assignments) else None
        result["assignments"] = assignments
        return result

    @staticmethod
    def _peer(assignment):
        return {key: assignment[key] for key in ("node_id", "endpoint", "id")}

    def create_task(self, request):
        with self.transaction() as db:
            nodes = [self._node_view(db, n) for n in db.execute("SELECT * FROM nodes").fetchall()]
            try:
                plan = allocate([n for n in nodes if n["status"] == "available"], request.num_layers,
                                request.layer_memory_mb, request.max_nodes)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            task_id, now = str(uuid4()), self.clock()
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,NULL)",
                       (task_id, request.model_dump_json(), "queued", now, now + request.timeout_seconds))
            for position, shard in enumerate(plan):
                db.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?,?,NULL)",
                           (str(uuid4()), task_id, shard["node_id"], position,
                            shard["layer_start"], shard["layer_end"], "pending"))
            return self._task_view(db, task_id)

    def get_task(self, task_id):
        with self.transaction() as db:
            return self._task_view(db, task_id)

    def tasks(self):
        with self.transaction() as db:
            return [self._task_view(db, t["id"]) for t in
                    db.execute("SELECT id FROM tasks ORDER BY created_at DESC LIMIT 100").fetchall()]

    def assignments(self, node_id, token):
        with self.transaction() as db:
            self._node_auth(db, node_id, token)
            # Include terminal commands until the node observes them; no silent reuse after expiry.
            rows = db.execute("""SELECT DISTINCT t.id FROM tasks t JOIN assignments a ON a.task_id=t.id
                                 WHERE a.node_id=? ORDER BY t.created_at DESC LIMIT 100""", (node_id,)).fetchall()
            result = []
            for row in rows:
                task = self._task_view(db, row["id"])
                for assignment in task["assignments"]:
                    if assignment["node_id"] == node_id:
                        result.append({**assignment, "task_status": task["status"],
                                       "request": task["request"], "deadline": task["deadline"]})
            return result

    def report(self, node_id, token, assignment_id, report):
        with self.transaction() as db:
            self._node_auth(db, node_id, token)
            assignment = db.execute("SELECT * FROM assignments WHERE id=? AND node_id=?",
                                    (assignment_id, node_id)).fetchone()
            if assignment is None:
                raise HTTPException(404, "assignment not found")
            task = self._task_view(db, assignment["task_id"])
            if assignment["status"] == report.status:
                return task  # Retries are idempotent, including terminal acknowledgements.
            if task["status"] not in ACTIVE or assignment["status"] not in ("pending", "running"):
                raise HTTPException(409, "assignment is terminal")
            db.execute("UPDATE assignments SET status=?, error=? WHERE id=?",
                       (report.status, report.error if report.status == "failed" else None, assignment_id))
            if report.status == "failed":
                self._end_task(db, task["id"], "failed", report.error or "node reported failure")
            elif not db.execute("SELECT 1 FROM assignments WHERE task_id=? AND status!='completed'",
                                (task["id"],)).fetchone():
                db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task["id"],))
            else:
                db.execute("UPDATE tasks SET status='running' WHERE id=?", (task["id"],))
            return self._task_view(db, task["id"])

    def cancel(self, task_id):
        with self.transaction() as db:
            task = self._task_view(db, task_id)
            if task["status"] == "cancelled":
                return task
            if task["status"] not in ACTIVE:
                raise HTTPException(409, "task is terminal")
            self._end_task(db, task_id, "cancelled", "cancelled by operator")
            return self._task_view(db, task_id)
