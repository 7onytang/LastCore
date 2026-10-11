import time

from .nodes import NodeService
from .repository import Repository
from .tasks import TaskService


class Store:
    """Composition root wiring the repository to the domain services."""

    def __init__(self, path: str, heartbeat_ttl: int, clock=time.time):
        self.path = path
        self.ttl = heartbeat_ttl
        self.clock = clock
        self.repository = Repository(path, heartbeat_ttl, clock)
        self.node_service = NodeService(self.repository)
        self.task_service = TaskService(self.repository, self.node_service)

    def register(self, request):
        return self.node_service.register(request)

    def nodes(self):
        return self.node_service.nodes()

    def heartbeat(self, node_id, token, request):
        return self.node_service.heartbeat(node_id, token, request)

    def create_task(self, request):
        return self.task_service.create_task(request)

    def get_task(self, task_id):
        return self.task_service.get_task(task_id)

    def tasks(self):
        return self.task_service.tasks()

    def assignments(self, node_id, token):
        return self.task_service.assignments(node_id, token)

    def report(self, node_id, token, assignment_id, report):
        return self.task_service.report(node_id, token, assignment_id, report)

    def cancel(self, task_id):
        return self.task_service.cancel(task_id)
