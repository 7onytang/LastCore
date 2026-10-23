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
        self._result_storage_ready = False
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
                CREATE TABLE IF NOT EXISTS node_workloads (
                    node_id TEXT NOT NULL REFERENCES nodes(id), workload TEXT NOT NULL,
                    PRIMARY KEY(node_id, workload)
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
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS tasks_created_at ON tasks(created_at DESC);
                CREATE INDEX IF NOT EXISTS nodes_accepting_heartbeat ON nodes(last_seen)
                    WHERE accepting_tasks != 0;
                CREATE TABLE IF NOT EXISTS workload_results (
                    assignment_id TEXT PRIMARY KEY REFERENCES assignments(id), workload_result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assignment_results (
                    assignment_id TEXT PRIMARY KEY REFERENCES assignments(id), result TEXT NOT NULL
                );
            """)
            self._result_storage_ready = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assignment_results'"
            ).fetchone() is not None

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

    def _node_view(self, db, node, *, busy=None, workloads=None):
        data = dict(node)
        data.pop("token_hash")
        data["accepting_tasks"] = bool(data["accepting_tasks"])
        data["supported_workloads"] = workloads if workloads is not None else [row[0] for row in db.execute(
            "SELECT workload FROM node_workloads WHERE node_id=? ORDER BY workload", (node["id"],))]
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
        nodes, capabilities = {}, {}
        for row in db.execute("""SELECT n.*, w.workload AS supported_workload FROM nodes n
                              LEFT JOIN node_workloads w ON w.node_id=n.id
                              ORDER BY n.id, w.workload"""):
            data = dict(row)
            workload = data.pop("supported_workload")
            nodes.setdefault(data["id"], data)
            if workload is not None:
                capabilities.setdefault(data["id"], []).append(workload)
        return [self._node_view(db, node, busy=node["id"] in busy, workloads=capabilities.get(node["id"], []))
                for node in nodes.values()]

    def register(self, request):
        node_id, token = str(uuid4()), secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)",
                       (node_id, self._hash(token), request.name, request.platform, str(request.endpoint),
                        request.available_memory_mb, request.compute_weight, request.accepting_tasks, self.clock()))
            self._set_workloads(db, node_id, request.supported_workloads)
        return {"node_id": node_id, "node_token": token, "heartbeat_ttl_seconds": self.ttl}

    @staticmethod
    def _set_workloads(db, node_id, workloads):
        # An old client omits the field: remove any stale claim for this identity.
        db.execute("DELETE FROM node_workloads WHERE node_id=?", (node_id,))
        db.executemany("INSERT INTO node_workloads VALUES (?,?)",
                       [(node_id, workload) for workload in sorted(set(workloads))])

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
            self._set_workloads(db, node_id, [] if whole_model else request.supported_workloads)
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
        result_join = "LEFT JOIN assignment_results ar ON ar.assignment_id=a.id" if self._result_storage_ready else ""
        result_column = ", ar.result AS stored_result" if self._result_storage_ready else ", NULL AS stored_result"
        result_column += ", wr.workload_result AS stored_workload_result"
        for row in db.execute(f"""
            SELECT a.*, n.endpoint{result_column}
            FROM assignments a JOIN nodes n ON n.id=a.node_id
            {result_join}
            LEFT JOIN workload_results wr ON wr.assignment_id=a.id
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
                raw_workload_result = assignment.pop("stored_workload_result", None)
                assignment["workload_result"] = json.loads(raw_workload_result) if raw_workload_result else None
            for i, assignment in enumerate(assignments):
                assignment["previous_node"] = self._peer(assignments[i-1]) if i else None
                assignment["next_node"] = self._peer(assignments[i+1]) if i+1 < len(assignments) else None
            result["assignments"] = assignments
            result["result"] = ([value for assignment in assignments for value in assignment["result"]]
                                if result["status"] == "completed" and assignments
                                and all(a["result"] is not None for a in assignments) else None)
            result["workload_result"] = None
            if (result["status"] == "completed" and assignments
                    and all(a["workload_result"] is not None for a in assignments)):
                workload = result["request"].get("workload", "linear")
                if workload == "pi":
                    inside = sum(a["workload_result"]["inside"] for a in assignments)
                    samples = sum(a["workload_result"]["samples"] for a in assignments)
                    result["workload_result"] = {"inside": inside, "samples": samples,
                                                "pi_estimate": 4 * inside / samples}
                elif workload == "log_stats":
                    levels = {"ERROR": 0, "WARN": 0, "INFO": 0, "OTHER": 0}
                    for assignment in assignments:
                        for level, count in assignment["workload_result"].items():
                            levels[level] = levels.get(level, 0) + count
                    result["workload_result"] = {"levels": levels, "total_lines": sum(levels.values())}
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
            target_clause = " AND n.id=?" if request.target_node_id is not None else ""
            workload_clause = (" AND EXISTS (SELECT 1 FROM node_workloads w WHERE w.node_id=n.id AND w.workload=?)"
                               if request.workload in ("pi", "log_stats") else "")
            parameters = [self.clock() - self.ttl]
            if workload_clause:
                parameters.append(request.workload)
            if request.target_node_id is not None:
                parameters.append(str(request.target_node_id))
            nodes = [dict(node) for node in db.execute("""
                SELECT n.id, n.endpoint, n.available_memory_mb, n.compute_weight FROM nodes n
                WHERE n.accepting_tasks != 0 AND n.last_seen > ? AND n.id NOT IN (
                    SELECT a.node_id FROM tasks t JOIN assignments a ON a.task_id=t.id
                    WHERE t.status IN ('queued','running')
                )
            """ + workload_clause + target_clause, parameters).fetchall()]
            try:
                plan = allocate(nodes, request.num_layers,
                                request.layer_memory_mb, request.max_nodes)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            task_id, now = str(uuid4()), self.clock()
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,NULL)",
                       (task_id, request.protocol_dump_json(), "queued", now, now + request.timeout_seconds))
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
            # Bounded history, not a durable terminal-command delivery queue.
            rows = db.execute("""SELECT DISTINCT t.* FROM tasks t JOIN assignments a ON a.task_id=t.id
                                 WHERE a.node_id=? ORDER BY t.created_at DESC LIMIT 100""", (node_id,)).fetchall()
            result = []
            for task in self._task_views(db, rows):
                for assignment in task["assignments"]:
                    if assignment["node_id"] == node_id:
                        result.append({**assignment, "task_status": task["status"],
                                       "request": task["request"], "deadline": task["deadline"]})
            return result

    def assignment(self, node_id, token, assignment_id):
        """Resolve one owned assignment independently of the bounded history window."""
        with self.transaction() as db:
            self._node_auth(db, node_id, token)
            row = db.execute("SELECT task_id FROM assignments WHERE id=? AND node_id=?",
                             (assignment_id, node_id)).fetchone()
            if row is None:
                raise HTTPException(404, "assignment not found")
            task = self._task_view(db, row["task_id"])
            assignment = next(a for a in task["assignments"] if a["id"] == assignment_id)
            return {**assignment, "task_status": task["status"],
                    "request": task["request"], "deadline": task["deadline"]}

    def report(self, node_id, token, assignment_id, report):
        with self.transaction() as db:
            self._node_auth(db, node_id, token)
            assignment = db.execute("SELECT * FROM assignments WHERE id=? AND node_id=?",
                                    (assignment_id, node_id)).fetchone()
            if assignment is None:
                raise HTTPException(404, "assignment not found")
            task = self._task_view(db, assignment["task_id"])
            request = task["request"]
            workload = request.get("workload", "linear")
            linear = request.get("model_id") == "lastcore/linear-demo-v1"
            typed = workload in ("pi", "log_stats")
            if linear and report.status == "completed":
                if report.result is None or len(report.result) != assignment["layer_end"] - assignment["layer_start"]:
                    raise HTTPException(422, "completed linear result must contain every assigned row")
            elif report.result is not None:
                raise HTTPException(422, "result is only accepted for completed linear assignments")
            if typed and report.status == "completed":
                values = report.workload_result
                if not isinstance(values, dict):
                    raise HTTPException(422, "completed workload requires its typed result")
                if workload == "pi":
                    expected = (request["total_samples"] * assignment["layer_end"] // request["num_layers"]
                                - request["total_samples"] * assignment["layer_start"] // request["num_layers"])
                    valid = (set(values) == {"inside", "samples"}
                             and values["samples"] == expected and 0 <= values["inside"] <= expected)
                else:
                    valid = (set(values).issubset({"ERROR", "WARN", "INFO", "OTHER"})
                             and sum(values.values()) == assignment["layer_end"] - assignment["layer_start"])
                if not valid:
                    raise HTTPException(422, "typed result does not match its assigned workload")
            elif report.workload_result is not None:
                raise HTTPException(422, "workload_result is only accepted for completed pi/log assignments")
            if assignment["status"] == report.status:
                previous = next(a for a in task["assignments"] if a["id"] == assignment_id)
                if report.result != previous["result"] or report.workload_result != previous["workload_result"]:
                    raise HTTPException(409, "assignment retry conflicts with the saved result")
                return task
            if task["status"] not in ACTIVE or assignment["status"] not in ("pending", "running"):
                raise HTTPException(409, "assignment is terminal")
            db.execute("UPDATE assignments SET status=?, error=? WHERE id=?",
                       (report.status, report.error if report.status == "failed" else None, assignment_id))
            if report.result is not None:
                db.execute("INSERT INTO assignment_results VALUES (?,?)",
                           (assignment_id, json.dumps(report.result, allow_nan=False)))
            if report.workload_result is not None:
                db.execute("INSERT INTO workload_results VALUES (?,?)",
                           (assignment_id, json.dumps(report.workload_result, allow_nan=False)))
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
