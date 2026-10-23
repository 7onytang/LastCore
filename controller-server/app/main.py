import asyncio
import logging
import json
import math
import re
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler, http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import AssignmentReport, Heartbeat, NodeRegistration, TaskRequest
from .store import Store
from .task_policy import TaskPolicy, validation_code, finite_error_values


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    admin_token: str
    registration_token: str
    database_path: str = "data/controller.sqlite3"
    heartbeat_ttl_seconds: int = 30
    expiry_interval_seconds: float = 1.0
    real_jobs_enabled: bool = False
    legacy_task_model_ids: bool = False

    @classmethod
    def from_env(cls):
        flag = os.environ.get("CONTROLLER_REAL_JOBS_ENABLED", "false").lower()
        if flag not in ("true", "false", "1", "0"):
            raise ValueError("CONTROLLER_REAL_JOBS_ENABLED must be true or false")
        legacy = os.environ.get("CONTROLLER_LEGACY_TASK_MODEL_IDS", "false").lower()
        if legacy not in ("true", "false", "1", "0"):
            raise ValueError("CONTROLLER_LEGACY_TASK_MODEL_IDS must be true or false")
        return cls(os.environ.get("CONTROLLER_ADMIN_TOKEN", ""),
                   os.environ.get("CONTROLLER_REGISTRATION_TOKEN", ""),
                   os.environ.get("CONTROLLER_DATABASE_PATH", "data/controller.sqlite3"),
                   int(os.environ.get("CONTROLLER_HEARTBEAT_TTL_SECONDS", "30")),
                   float(os.environ.get("CONTROLLER_EXPIRY_INTERVAL_SECONDS", "1")), flag in ("true", "1"), legacy in ("true", "1"))


def create_app(settings: Settings | None = None, store: Store | None = None):
    settings = settings or Settings.from_env()
    if min(len(settings.admin_token), len(settings.registration_token)) < 32:
        raise ValueError("Set separate CONTROLLER_ADMIN_TOKEN and CONTROLLER_REGISTRATION_TOKEN (at least 32 characters)")
    if settings.admin_token == settings.registration_token:
        raise ValueError("Admin and registration credentials must be different")
    if not 5 <= settings.heartbeat_ttl_seconds <= 3600:
        raise ValueError("Heartbeat TTL must be between 5 and 3600 seconds")
    if not math.isfinite(settings.expiry_interval_seconds) or not 0.1 <= settings.expiry_interval_seconds <= 60:
        raise ValueError("Expiry interval must be between 0.1 and 60 seconds")
    policy = TaskPolicy(settings.legacy_task_model_ids)
    if settings.legacy_task_model_ids:
        logger.warning("Legacy model-ID compatibility enabled; arbitrary legacy identifiers do not meet strict task policy")
    store = store or Store(settings.database_path, settings.heartbeat_ttl_seconds)
    jobs = None
    if settings.real_jobs_enabled:
        from .inference_jobs import Jobs
        jobs = Jobs(store)

    def expire_all():
        store.expire()
        if jobs is not None:
            jobs.expire()

    @asynccontextmanager
    async def lifespan(app):
        # Reject startup if persisted state cannot be reconciled. SQLite work
        # runs off the event loop and uses the same locking as API transactions.
        await asyncio.to_thread(expire_all)
        stop = asyncio.Event()

        async def sweep():
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.expiry_interval_seconds)
                    return
                except TimeoutError:
                    try:
                        await asyncio.to_thread(expire_all)
                    except sqlite3.Error:
                        logger.exception("Controller expiry sweep failed; retrying on next interval")

        worker = asyncio.create_task(sweep(), name="controller-expiry")
        try:
            yield
        finally:
            stop.set()
            # Do not cancel a SQLite thread mid-transaction or leave it writing
            # after shutdown. An idle worker wakes immediately on this event.
            await worker

    app = FastAPI(title="LastCore Controller", version="0.1.0", lifespan=lifespan)
    @app.exception_handler(RequestValidationError)
    async def policy_validation_error(request: Request, exc: RequestValidationError):
        admission = request.method == "POST" and (request.url.path in ("/v1/tasks", "/v1/inference/jobs")
                or re.fullmatch(r"/v1/inference/workers/[^/]+/capability", request.url.path))
        rendered = RequestValidationError(finite_error_values(exc.errors()), body=exc.body) if admission else exc
        response = await request_validation_exception_handler(request, rendered)
        if admission:
            content = json.loads(response.body)
            content["error_code"] = validation_code(exc.errors(), exc.body, policy)
            response.body = json.dumps(content, ensure_ascii=False).encode("utf-8")
            response.headers["content-length"] = str(len(response.body))
        return response

    @app.exception_handler(StarletteHTTPException)
    async def policy_http_error(request: Request, exc: StarletteHTTPException):
        response = await http_exception_handler(request, exc)
        if hasattr(exc, "error_code"):
            content = json.loads(response.body)
            content["error_code"] = exc.error_code
            response.body = json.dumps(content, ensure_ascii=False).encode("utf-8")
            response.headers["content-length"] = str(len(response.body))
        return response

    ui = Path(__file__).parent / "ui"
    app.mount("/ui", StaticFiles(directory=ui), name="ui")

    page_headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/console", methods=["GET", "HEAD"], include_in_schema=False)
    def console():
        return FileResponse(ui / "index.html", headers=page_headers)
    bearer = HTTPBearer(auto_error=False)

    @app.api_route("/workloads", methods=["GET", "HEAD"], include_in_schema=False)
    def showcase():
        return FileResponse(Path(__file__).with_name("showcase.html"), headers=page_headers)

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

    @app.get("/v1/nodes/{node_id}/assignments/{assignment_id}")
    def assignment(node_id: str, assignment_id: str, token: Annotated[str, Depends(credential)]):
        return store.assignment(node_id, token, assignment_id)

    @app.post("/v1/nodes/{node_id}/assignments/{assignment_id}/report")
    def report(node_id: str, assignment_id: str, request: AssignmentReport,
               token: Annotated[str, Depends(credential)]):
        return store.report(node_id, token, assignment_id, request)

    @app.post("/v1/tasks", status_code=201, dependencies=[Depends(require_admin)])
    def create_task(request: TaskRequest):
        policy.admit(request, request.workload)
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

    if jobs is not None:
        from .inference_jobs import router
        app.include_router(router(jobs, credential, require_admin, policy))
    return app
