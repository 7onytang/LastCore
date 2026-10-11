"""Task lifecycle domain operations: creation with scheduling, reads, reports, cancel."""

from uuid import uuid4

from fastapi import HTTPException

from .nodes import NodeService
from .repository import Repository
from .scheduler import allocate

ACTIVE = ("queued", "running")


class TaskService:
    def __init__(self, repository: Repository, node_service: NodeService):
        self.repository = repository
        self.node_service = node_service

    def _task_view(self, db, task_id):
        task = self.repository.task_view(db, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        return task

    def create_task(self, request):
        with self.repository.transaction() as db:
            nodes = [self.repository.node_view(db, n) for n in self.repository.all_nodes(db)]
            try:
                plan = allocate([n for n in nodes if n["status"] == "available"], request.num_layers,
                                request.layer_memory_mb, request.max_nodes)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            task_id, now = str(uuid4()), self.repository.clock()
            self.repository.insert_task(db, task_id, request.model_dump_json(), now,
                                        now + request.timeout_seconds)
            for position, shard in enumerate(plan):
                self.repository.insert_assignment(db, str(uuid4()), task_id, shard["node_id"], position,
                                                  shard["layer_start"], shard["layer_end"])
            return self._task_view(db, task_id)

    def get_task(self, task_id):
        with self.repository.transaction() as db:
            return self._task_view(db, task_id)

    def tasks(self):
        with self.repository.transaction() as db:
            return [self._task_view(db, task_id) for task_id in self.repository.task_ids(db)]

    def assignments(self, node_id, token):
        with self.repository.transaction() as db:
            self.node_service.authenticate(db, node_id, token)
            # Include terminal commands until the node observes them; no silent reuse after expiry.
            result = []
            for task_id in self.repository.task_ids_for_node(db, node_id):
                task = self._task_view(db, task_id)
                for assignment in task["assignments"]:
                    if assignment["node_id"] == node_id:
                        result.append({**assignment, "task_status": task["status"],
                                       "request": task["request"], "deadline": task["deadline"]})
            return result

    def report(self, node_id, token, assignment_id, report):
        with self.repository.transaction() as db:
            self.node_service.authenticate(db, node_id, token)
            assignment = self.repository.get_assignment(db, assignment_id, node_id)
            if assignment is None:
                raise HTTPException(404, "assignment not found")
            task = self._task_view(db, assignment["task_id"])
            if assignment["status"] == report.status:
                return task  # Retries are idempotent, including terminal acknowledgements.
            if task["status"] not in ACTIVE or assignment["status"] not in ("pending", "running"):
                raise HTTPException(409, "assignment is terminal")
            self.repository.update_assignment(db, assignment_id, report.status,
                                              report.error if report.status == "failed" else None)
            if report.status == "failed":
                self.repository.end_task(db, task["id"], "failed", report.error or "node reported failure")
            elif not self.repository.has_unfinished_assignment(db, task["id"]):
                self.repository.set_task_status(db, task["id"], "completed")
            else:
                self.repository.set_task_status(db, task["id"], "running")
            return self._task_view(db, task["id"])

    def cancel(self, task_id):
        with self.repository.transaction() as db:
            task = self._task_view(db, task_id)
            if task["status"] == "cancelled":
                return task
            if task["status"] not in ACTIVE:
                raise HTTPException(409, "task is terminal")
            self.repository.end_task(db, task_id, "cancelled", "cancelled by operator")
            return self._task_view(db, task_id)
