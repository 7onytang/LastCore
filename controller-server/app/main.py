import os
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .models import AssignmentReport, Heartbeat, NodeRegistration, TaskRequest
from .store import Store


@dataclass(frozen=True)
class Settings:
    admin_token: str
    registration_token: str
    database_path: str = "data/controller.sqlite3"
    heartbeat_ttl_seconds: int = 30

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("CONTROLLER_ADMIN_TOKEN", ""),
                   os.environ.get("CONTROLLER_REGISTRATION_TOKEN", ""),
                   os.environ.get("CONTROLLER_DATABASE_PATH", "data/controller.sqlite3"),
                   int(os.environ.get("CONTROLLER_HEARTBEAT_TTL_SECONDS", "30")))


def create_app(settings: Settings | None = None, store: Store | None = None):
    settings = settings or Settings.from_env()
    if min(len(settings.admin_token), len(settings.registration_token)) < 32:
        raise ValueError("Set separate CONTROLLER_ADMIN_TOKEN and CONTROLLER_REGISTRATION_TOKEN (at least 32 characters)")
    if settings.admin_token == settings.registration_token:
        raise ValueError("Admin and registration credentials must be different")
    if not 5 <= settings.heartbeat_ttl_seconds <= 3600:
        raise ValueError("Heartbeat TTL must be between 5 and 3600 seconds")
    store = store or Store(settings.database_path, settings.heartbeat_ttl_seconds)
    app = FastAPI(title="LastCore Controller", version="0.1.0")
    bearer = HTTPBearer(auto_error=False)

    def credential(auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if auth is None or auth.scheme.lower() != "bearer":
            raise HTTPException(401, "Bearer credential required", headers={"WWW-Authenticate": "Bearer"})
        return auth.credentials

    def require_admin(token: Annotated[str, Depends(credential)]):
        if not secrets.compare_digest(token.encode(), settings.admin_token.encode()):
            raise HTTPException(401, "invalid admin credential", headers={"WWW-Authenticate": "Bearer"})

    def require_registration(token: Annotated[str, Depends(credential)]):
        if not secrets.compare_digest(token.encode(), settings.registration_token.encode()):
            raise HTTPException(401, "invalid registration credential", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post("/v1/nodes/register", status_code=201, dependencies=[Depends(require_registration)])
    def register(request: NodeRegistration):
        return store.register(request)

    @app.get("/v1/nodes", dependencies=[Depends(require_admin)])
    def nodes():
        return {"nodes": store.nodes()}

    @app.post("/v1/nodes/{node_id}/heartbeat")
    def heartbeat(node_id: str, request: Heartbeat, token: Annotated[str, Depends(credential)]):
        return store.heartbeat(node_id, token, request)

    @app.get("/v1/nodes/{node_id}/assignments")
    def assignments(node_id: str, token: Annotated[str, Depends(credential)]):
        return {"assignments": store.assignments(node_id, token)}

    @app.post("/v1/nodes/{node_id}/assignments/{assignment_id}/report")
    def report(node_id: str, assignment_id: str, request: AssignmentReport,
               token: Annotated[str, Depends(credential)]):
        return store.report(node_id, token, assignment_id, request)

    @app.post("/v1/tasks", status_code=201, dependencies=[Depends(require_admin)])
    def create_task(request: TaskRequest):
        return store.create_task(request)

    @app.get("/v1/tasks", dependencies=[Depends(require_admin)])
    def tasks():
        return {"tasks": store.tasks()}

    @app.get("/v1/tasks/{task_id}", dependencies=[Depends(require_admin)])
    def get_task(task_id: str):
        return store.get_task(task_id)

    @app.post("/v1/tasks/{task_id}/cancel", dependencies=[Depends(require_admin)])
    def cancel(task_id: str):
        return store.cancel(task_id)

    return app
