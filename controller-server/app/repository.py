"""SQLite persistence: schema, transactions, row access and read models.

This layer has no HTTP or scheduling dependencies; callers own the domain rules.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Repository:
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
            self.expire_stale(db)
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

    def end_task(self, db, task_id, status, error=None):
        db.execute("UPDATE tasks SET status=?, error=? WHERE id=?", (status, error, task_id))
        db.execute("UPDATE assignments SET status=?, error=? WHERE task_id=? AND status IN ('pending','running')",
                   ("cancelled" if status == "cancelled" else "failed", error, task_id))

    def expire_stale(self, db):
        now = self.clock()
        rows = db.execute("""
            SELECT DISTINCT t.id, t.deadline FROM tasks t
            JOIN assignments a ON a.task_id=t.id JOIN nodes n ON n.id=a.node_id
            WHERE t.status IN ('queued','running') AND (t.deadline <= ? OR n.last_seen <= ?)
        """, (now, now - self.ttl)).fetchall()
        for row in rows:
            error = "task deadline exceeded" if row["deadline"] <= now else "assigned node heartbeat expired"
            self.end_task(db, row["id"], "failed", error)

    def insert_node(self, db, node_id, token_hash, request, now):
        db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)",
                   (node_id, token_hash, request.name, request.platform, str(request.endpoint),
                    request.available_memory_mb, request.compute_weight, 1, now))

    def get_node(self, db, node_id):
        return db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()

    def all_nodes(self, db):
        return db.execute("SELECT * FROM nodes").fetchall()

    def all_nodes_by_id(self, db):
        return db.execute("SELECT * FROM nodes ORDER BY id").fetchall()

    def update_heartbeat(self, db, node_id, available_memory_mb, accepting_tasks, now):
        db.execute("UPDATE nodes SET available_memory_mb=?, accepting_tasks=?, last_seen=? WHERE id=?",
                   (available_memory_mb, accepting_tasks, now, node_id))

    def node_view(self, db, node):
        data = dict(node)
        data.pop("token_hash")
        data["accepting_tasks"] = bool(data["accepting_tasks"])
        busy = db.execute("""SELECT 1 FROM assignments a JOIN tasks t ON t.id=a.task_id
                             WHERE a.node_id=? AND t.status IN ('queued','running') LIMIT 1""",
                          (node["id"],)).fetchone()
        data["status"] = ("offline" if node["last_seen"] <= self.clock() - self.ttl
                          else "busy" if busy else "available" if node["accepting_tasks"] else "draining")
        return data

    def insert_task(self, db, task_id, request_json, created_at, deadline):
        db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,NULL)",
                   (task_id, request_json, "queued", created_at, deadline))

    def insert_assignment(self, db, assignment_id, task_id, node_id, position, layer_start, layer_end):
        db.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?,?,NULL)",
                   (assignment_id, task_id, node_id, position, layer_start, layer_end, "pending"))

    def get_assignment(self, db, assignment_id, node_id):
        return db.execute("SELECT * FROM assignments WHERE id=? AND node_id=?",
                          (assignment_id, node_id)).fetchone()

    def update_assignment(self, db, assignment_id, status, error):
        db.execute("UPDATE assignments SET status=?, error=? WHERE id=?", (status, error, assignment_id))

    def set_task_status(self, db, task_id, status):
        db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))

    def has_unfinished_assignment(self, db, task_id):
        return bool(db.execute("SELECT 1 FROM assignments WHERE task_id=? AND status!='completed'",
                               (task_id,)).fetchone())

    def task_ids(self, db, limit=100):
        rows = db.execute("SELECT id FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [row["id"] for row in rows]

    def task_ids_for_node(self, db, node_id, limit=100):
        rows = db.execute("""SELECT DISTINCT t.id FROM tasks t JOIN assignments a ON a.task_id=t.id
                             WHERE a.node_id=? ORDER BY t.created_at DESC LIMIT ?""",
                          (node_id, limit)).fetchall()
        return [row["id"] for row in rows]

    @staticmethod
    def _peer(assignment):
        return {key: assignment[key] for key in ("node_id", "endpoint", "id")}

    def task_view(self, db, task_id):
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            return None
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
