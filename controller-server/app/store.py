import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
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
        with closing(sqlite3.connect(path)) as db, db:
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
                CREATE TABLE IF NOT EXISTS assignment_results (
                    assignment_id TEXT PRIMARY KEY REFERENCES assignments(id) ON DELETE CASCADE,
                    result TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS assignments_node ON assignments(node_id);
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS tasks_created_at ON tasks(created_at DESC);
                CREATE INDEX IF NOT EXISTS nodes_accepting_heartbeat ON nodes(last_seen)
                    WHERE accepting_tasks != 0;
            """)

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
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
        by_error = {"task deadline exceeded": [], "assigned node heartbeat expired": []}
        for row in rows:
            error = "task deadline exceeded" if row["deadline"] <= now else "assigned node heartbeat expired"
            by_error[error].append(row["id"])
        # Keep every chunk in the original expiry transaction. Respect even a
        # connection whose parameter limit is lower than SQLite's default.
        batch_size = min(500, db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - 1)
        for error, task_ids in by_error.items():
            for start in range(0, len(task_ids), batch_size):
                chunk = task_ids[start:start + batch_size]
                placeholders = ",".join("?" for _ in chunk)
                db.execute(f"UPDATE tasks SET status='failed', error=? WHERE id IN ({placeholders})",
                           [error, *chunk])
                db.execute(f"""UPDATE assignments SET status='failed', error=?
                    WHERE task_id IN ({placeholders}) AND status IN ('pending','running')""",
                           [error, *chunk])

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
            raise HTTPException(401, "invalid node credential", headers={"WWW-Authenticate": "Bearer"})
        return node

    def _node_view(self, db, node, *, busy=None):
        data = dict(node)
        data.pop("token_hash")
        data["accepting_tasks"] = bool(data["accepting_tasks"])
        if busy is None:
            busy = db.execute("""SELECT 1 FROM assignments a JOIN tasks t ON t.id=a.task_id
                             WHERE a.node_id=? AND t.status IN ('queued','running') LIMIT 1""",
                          (node["id"],)).fetchone()
        data["status"] = ("offline" if node["last_seen"] <= self.clock() - self.ttl
                          else "busy" if busy else "available" if node["accepting_tasks"] else "draining")
        return data

    def _node_views(self, db):
        # Completed shards remain reserved until their entire task terminates.
        busy = {row["node_id"] for row in db.execute("""
            SELECT DISTINCT a.node_id FROM tasks t JOIN assignments a ON a.task_id=t.id
            WHERE t.status IN ('queued','running')
        """)}
        return [self._node_view(db, node, busy=node["id"] in busy)
                for node in db.execute("SELECT * FROM nodes ORDER BY id").fetchall()]

    def register(self, request):
        node_id, token = str(uuid4()), secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)",
                       (node_id, self._hash(token), request.name, request.platform, str(request.endpoint),
                        request.available_memory_mb, request.compute_weight, request.accepting_tasks, self.clock()))
        return {"node_id": node_id, "node_token": token, "heartbeat_ttl_seconds": self.ttl}

    def nodes(self):
        with self.transaction() as db:
            return self._node_views(db)

    def heartbeat(self, node_id, token, request):
        with self.transaction() as db:
            self._node_auth(db, node_id, token)
            # Whole-model workers are explicitly ineligible for legacy layers,
            # even if a client sends accepting_tasks=true on an old heartbeat.
            real_workers = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inference_workers'").fetchone()
            whole_model = real_workers and db.execute('SELECT 1 FROM inference_workers WHERE node_id=?', (node_id,)).fetchone()
            db.execute("UPDATE nodes SET available_memory_mb=?, accepting_tasks=?, last_seen=? WHERE id=?",
                       (request.available_memory_mb, False if whole_model else request.accepting_tasks, self.clock(), node_id))
            return self._node_view(db, db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())

    def _task_view(self, db, task_id):
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise HTTPException(404, "task not found")
        return self._task_views(db, [task])[0]

    def _task_views(self, db, tasks):
        if not tasks:
            return []
        # Public list/poll calls select at most 100 tasks. Load every shard so
        # peer addresses still describe the full pipeline before poll filtering.
        task_ids = [task["id"] for task in tasks]
        placeholders = ",".join("?" for _ in task_ids)
        by_task = {task_id: [] for task_id in task_ids}
        for row in db.execute(f"""
            SELECT a.*, n.endpoint, ar.result AS stored_result
            FROM assignments a JOIN nodes n ON n.id=a.node_id
            LEFT JOIN assignment_results ar ON ar.assignment_id=a.id
            WHERE a.task_id IN ({placeholders}) ORDER BY a.task_id, a.position
        """, task_ids):
            by_task[row["task_id"]].append(dict(row))
        results = []
        for task in tasks:
            result = dict(task)
            result["request"] = json.loads(result["request"])
            assignments = by_task[task["id"]]
            for assignment in assignments:
                raw_result = assignment.pop("stored_result", None)
                assignment["result"] = json.loads(raw_result) if raw_result else None
            for i, assignment in enumerate(assignments):
                assignment["previous_node"] = self._peer(assignments[i-1]) if i else None
                assignment["next_node"] = self._peer(assignments[i+1]) if i+1 < len(assignments) else None
            result["assignments"] = assignments
            result["result"] = None
            if result["status"] == "completed" and all(a["result"] is not None for a in assignments):
                workload = result["request"].get("workload", "linear")
                if workload == "pi":
                    inside = sum(a["result"]["inside"] for a in assignments)
                    samples = sum(a["result"]["samples"] for a in assignments)
                    result["result"] = {"inside": inside, "samples": samples,
                                         "pi_estimate": 4 * inside / samples}
                elif workload == "log_stats":
                    levels = {}
                    for assignment in assignments:
                        for level, count in assignment["result"].items():
                            levels[level] = levels.get(level, 0) + count
                    result["result"] = {"levels": levels,
                                         "total_lines": sum(levels.values())}
                else:
                    result["result"] = [value for a in assignments for value in a["result"]]
            results.append(result)
        return results

    @staticmethod
    def _peer(assignment):
        return {key: assignment[key] for key in ("node_id", "endpoint", "id")}

    def create_task(self, request):
        with self.transaction() as db:
            # Retain historical nodes in the registry without materializing
            # offline/draining entries on the scheduling path. node_id is NOT
            # NULL, so NOT IN safely excludes all active pipeline reservations.
            nodes = [dict(node) for node in db.execute("""
                SELECT n.id, n.endpoint, n.available_memory_mb, n.compute_weight FROM nodes n
                WHERE n.accepting_tasks != 0 AND n.last_seen > ? AND n.id NOT IN (
                    SELECT a.node_id FROM tasks t JOIN assignments a ON a.task_id=t.id
                    WHERE t.status IN ('queued','running')
                )
            """, (self.clock() - self.ttl,)).fetchall()]
            try:
                plan = allocate(nodes, request.num_layers,
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
            rows = db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100").fetchall()
            return self._task_views(db, rows)

    def assignments(self, node_id, token):
        with self.transaction() as db:
            self._node_auth(db, node_id, token)
            # Include terminal commands until the node observes them; no silent reuse after expiry.
            rows = db.execute("""SELECT DISTINCT t.* FROM tasks t JOIN assignments a ON a.task_id=t.id
                                 WHERE a.node_id=? ORDER BY t.created_at DESC LIMIT 100""", (node_id,)).fetchall()
            result = []
            for task in self._task_views(db, rows):
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
            if report.result is not None:
                request = task["request"]
                workload = request.get("workload", "linear")
                valid = report.status == "completed"
                if workload == "pi":
                    expected_samples = (request["total_samples"] * assignment["layer_end"] // request["num_layers"]
                                        - request["total_samples"] * assignment["layer_start"] // request["num_layers"])
                    valid = (valid and isinstance(report.result, dict)
                             and set(report.result) == {"inside", "samples"}
                             and all(type(value) is int and value >= 0 for value in report.result.values())
                             and report.result["samples"] == expected_samples
                             and report.result["inside"] <= expected_samples)
                elif workload == "log_stats":
                    valid = (valid and isinstance(report.result, dict)
                             and set(report.result).issubset({"ERROR", "WARN", "INFO", "OTHER"})
                             and all(isinstance(key, str) and type(value) is int and value >= 0
                                     for key, value in report.result.items())
                             and sum(report.result.values()) == assignment["layer_end"] - assignment["layer_start"])
                else:
                    expected = assignment["layer_end"] - assignment["layer_start"]
                    valid = (valid and isinstance(report.result, list)
                             and len(report.result) == expected)
                if not valid:
                    raise HTTPException(422, "completed result does not match the assigned workload")
            if assignment["status"] == report.status:
                return task  # Retries are idempotent, including terminal acknowledgements.
            if task["status"] not in ACTIVE or assignment["status"] not in ("pending", "running"):
                raise HTTPException(409, "assignment is terminal")
            db.execute("UPDATE assignments SET status=?, error=? WHERE id=?",
                       (report.status, report.error if report.status == "failed" else None, assignment_id))
            if report.result is not None:
                db.execute("INSERT INTO assignment_results (assignment_id, result) VALUES (?, ?) "
                           "ON CONFLICT(assignment_id) DO UPDATE SET result=excluded.result",
                           (assignment_id, json.dumps(report.result)))
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
