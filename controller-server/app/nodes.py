"""Node lifecycle domain operations: registration, credentials, heartbeat."""

import hashlib
import hmac
import secrets
from uuid import uuid4

from .errors import ControllerError
from .repository import Repository


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class NodeService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def authenticate(self, db, node_id, token):
        node = self.repository.get_node(db, node_id)
        if node is None or not hmac.compare_digest(node["token_hash"], hash_token(token)):
            raise ControllerError(401, "invalid node credential")
        return node

    def register(self, request):
        node_id, token = str(uuid4()), secrets.token_urlsafe(32)
        with self.repository.transaction() as db:
            self.repository.insert_node(db, node_id, hash_token(token), request, self.repository.clock())
        return {"node_id": node_id, "node_token": token, "heartbeat_ttl_seconds": self.repository.ttl}

    def nodes(self):
        with self.repository.transaction() as db:
            return [self.repository.node_view(db, n) for n in self.repository.all_nodes_by_id(db)]

    def heartbeat(self, node_id, token, request):
        with self.repository.transaction() as db:
            self.authenticate(db, node_id, token)
            self.repository.update_heartbeat(db, node_id, request.available_memory_mb,
                                             request.accepting_tasks, self.repository.clock())
            return self.repository.node_view(db, self.repository.get_node(db, node_id))
