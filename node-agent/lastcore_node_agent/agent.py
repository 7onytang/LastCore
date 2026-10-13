from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .config import AgentConfig
from .controller import ControllerClient, ControllerError
from .credentials import (
    NodeCredentials,
    StateError,
    clear_pending_report,
    load_credentials,
    load_pending_reports,
    load_settled,
    record_pending_report,
    save_credentials,
    settle_journal,
)
from .executor import ExecutionContext, ExecutionCancelled, Executor, load_executor

LOG = logging.getLogger("lastcore.node_agent")


class CredentialRejected(Exception):
    pass


@dataclass
class RunStats:
    heartbeats_ok: int = 0
    heartbeats_failed: int = 0
    polls: int = 0
    started: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)


@dataclass
class _ActiveRun:
    context: ExecutionContext
    worker: threading.Thread
    assignment_id: str
    task_id: str
    result: tuple[str, str | None] | None = None


class NodeAgent:
    def __init__(self, config: AgentConfig, client: ControllerClient,
                 clock: Callable[[], float] = time.time,
                 executor_factory: Callable[[], Executor] | None = None,
                 stats: RunStats | None = None):
        self.config = config
        self.client = client
        self.clock = clock
        self.executor_factory = executor_factory or (lambda: load_executor(config.executor))
        self.stats = stats or RunStats()
        self.credentials: NodeCredentials | None = None
        self.stop_event = threading.Event()
        self._active: _ActiveRun | None = None
        self._active_lock = threading.Lock()
        self._settled: dict[str, str] = {}
        self._pending: dict[str, tuple[str, str | None]] = {}
        self._heartbeat_lock = threading.Lock()
        self._last_heartbeat_ok = 0.0
        self._credential_error: str | None = None

    def ensure_credentials(self) -> NodeCredentials:
        credentials = load_credentials(self.config.state_dir)
        if credentials is None:
            if not self.config.registration_token:
                raise RuntimeError("no persisted node credentials and no registration token configured")
            response = self.client.register(self.config.registration_token,
                                            self.config.registration_payload())
            credentials = NodeCredentials(str(response["node_id"]), str(response["node_token"]),
                                          int(response.get("heartbeat_ttl_seconds") or 30))
            save_credentials(self.config.state_dir, credentials)
            LOG.info("registered as node %s", credentials.node_id)
        else:
            LOG.info("reusing persisted credentials for node %s", credentials.node_id)
        self.credentials = credentials
        return credentials

    def run_forever(self) -> None:
        joined = self._join_controller()
        self._settled = load_settled(self.config.state_dir)
        self._pending = load_pending_reports(self.config.state_dir)
        heartbeat_thread = None
        if joined:
            heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                                                name="lastcore-heartbeat", daemon=True)
            heartbeat_thread.start()
            watchdog_thread = threading.Thread(target=self._watchdog_loop,
                                               name="lastcore-watchdog", daemon=True)
            watchdog_thread.start()
        try:
            while joined and not self.stop_event.is_set() and self._credential_error is None:
                try:
                    self._poll_once()
                except httpx.HTTPError as exc:
                    LOG.warning("assignments poll failed: %s", exc)
                except ControllerError as exc:
                    LOG.warning("assignments poll rejected: %s", exc)
                self.stop_event.wait(self.config.poll_interval)
            if self._credential_error:
                raise CredentialRejected(self._credential_error)
        finally:
            self.stop_event.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(1.0)
            self._shutdown_execution()

    def shutdown(self) -> None:
        self.stop_event.set()

    def _guarded(self, call):
        def invoke(*args, **kwargs):
            try:
                return call(*args, **kwargs)
            except ControllerError as exc:
                if exc.status_code == 401:
                    raise CredentialRejected(str(exc)) from exc
                raise
        return invoke

    def _join_controller(self) -> bool:
        while not self.stop_event.is_set():
            try:
                self.ensure_credentials()
                self._send_heartbeat()
                return True
            except (httpx.HTTPError, ControllerError) as exc:
                LOG.warning("controller not reachable yet (%s), retrying in 1s", exc)
                self.stop_event.wait(1.0)
        return False

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.config.heartbeat_interval):
            try:
                self._send_heartbeat()
            except CredentialRejected as exc:
                self._credential_error = str(exc)
                self.stop_event.set()
                return
            except (httpx.HTTPError, ControllerError) as exc:
                self.stats.heartbeats_failed += 1
                LOG.warning("heartbeat failed: %s", exc)

    def _watchdog_loop(self) -> None:
        # Runs independently of assignments polling, so local deadline/heartbeat-TTL
        # cancellation still fires while GET is blocked or failing (total outage).
        while not self.stop_event.wait(self.config.watchdog_interval):
            run = self._current_run()
            if run is None:
                continue
            if self.clock() >= run.context.deadline:
                run.context.request_cancel("deadline")
            elif self._heartbeat_stalled():
                run.context.request_cancel("heartbeat-stall")

    def _send_heartbeat(self) -> None:
        assert self.credentials is not None
        self._guarded(self.client.heartbeat)(
            self.credentials.node_id, self.credentials.node_token,
            self.config.available_memory_mb, self.config.accepting_tasks)
        with self._heartbeat_lock:
            self._last_heartbeat_ok = self.clock()
        self.stats.heartbeats_ok += 1

    def _poll_once(self) -> None:
        assert self.credentials is not None
        rows = self._guarded(self.client.assignments)(self.credentials.node_id,
                                                      self.credentials.node_token)
        self.stats.polls += 1
        run = self._current_run()
        if run is not None:
            self._watch_active(rows, run)
        self._reap_finished()
        self._flush_pending_reports()
        if self._current_run() is None:
            candidate = self._select_assignment(rows)
            if candidate is not None:
                self._start_assignment(candidate)

    def _current_run(self) -> _ActiveRun | None:
        with self._active_lock:
            return self._active

    def _heartbeat_stalled(self) -> bool:
        assert self.credentials is not None
        with self._heartbeat_lock:
            last_ok = self._last_heartbeat_ok
        return self.clock() - last_ok > self.credentials.heartbeat_ttl_seconds

    def _watch_active(self, rows: list, run: _ActiveRun) -> None:
        if self.clock() >= run.context.deadline:
            run.context.request_cancel("deadline")
            return
        if self._heartbeat_stalled():
            run.context.request_cancel("heartbeat-stall")
            return
        for row in rows:
            if row.get("task_id") == run.task_id and row.get("task_status") in ("cancelled", "failed"):
                run.context.request_cancel("task-terminal")
                return

    def _select_assignment(self, rows: list) -> dict | None:
        for row in reversed(rows):
            assignment_id = row.get("id")
            if not assignment_id or assignment_id in self._settled or assignment_id in self._pending:
                continue
            if row.get("task_status") not in ("queued", "running"):
                continue
            if self.clock() >= float(row.get("deadline") or 0.0):
                continue
            return row
        return None

    def _start_assignment(self, row: dict) -> None:
        assert self.credentials is not None
        assignment_id = row["id"]
        task_id = row["task_id"]
        try:
            self._guarded(self.client.report)(self.credentials.node_id, self.credentials.node_token,
                                              assignment_id, "running")
        except ControllerError as exc:
            if exc.status_code == 409:
                LOG.info("assignment %s already terminal, skipping", assignment_id)
                self._settle(assignment_id, "skipped-terminal")
                self.stats.stopped.append(assignment_id)
                return
            LOG.warning("could not report running for %s, will retry next poll: %s", assignment_id, exc)
            return
        try:
            executor = self.executor_factory()
        except Exception as exc:
            LOG.exception("executor unavailable for %s", assignment_id)
            error = f"executor unavailable: {type(exc).__name__}: {exc}"
            if self._record_pending(assignment_id, "failed", error):
                self._settle(assignment_id, "failed")
            self._attempt_terminal_report(assignment_id, "failed", error)
            self.stats.failed.append(assignment_id)
            return
        context = ExecutionContext(
            task_id=task_id,
            assignment_id=assignment_id,
            model_id=str((row.get("request") or {}).get("model_id", "")),
            position=int(row.get("position") or 0),
            layer_start=int(row.get("layer_start") or 0),
            layer_end=int(row.get("layer_end") or 0),
            previous_node=row.get("previous_node"),
            next_node=row.get("next_node"),
            deadline=float(row.get("deadline") or 0.0),
            raw=row,
        )
        worker = threading.Thread(target=self._execute, args=(context, executor),
                                  name=f"lastcore-exec-{assignment_id[:8]}", daemon=True)
        run = _ActiveRun(context=context, worker=worker,
                         assignment_id=assignment_id, task_id=task_id)
        with self._active_lock:
            self._active = run
        worker.start()
        self.stats.started.append(assignment_id)
        LOG.info("started assignment %s (task %s) layers [%s, %s)",
                 assignment_id, task_id, context.layer_start, context.layer_end)

    def _execute(self, context: ExecutionContext, executor: Executor) -> None:
        status, error = "completed", None
        try:
            executor.run(context)
        except ExecutionCancelled as exc:
            status, error = "cancelled-local", exc.reason
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            LOG.exception("executor failed for assignment %s", context.assignment_id)
        run = self._current_run()
        if run is not None and run.context is context:
            run.result = (status, error)
        else:
            LOG.error("no active run for finished assignment %s", context.assignment_id)

    def _reap_finished(self) -> None:
        run = self._current_run()
        if run is None or run.worker.is_alive():
            return
        with self._active_lock:
            if self._active is run:
                self._active = None
        if run.result is None:
            LOG.error("assignment %s worker finished without result", run.assignment_id)
            return
        status, error = run.result
        if status == "cancelled-local":
            self._settle(run.assignment_id, "stopped")
            self.stats.stopped.append(run.assignment_id)
            LOG.info("assignment %s stopped locally (%s)", run.assignment_id, error)
            return
        if self._record_pending(run.assignment_id, status, error):
            self._settle(run.assignment_id, status)
        self._attempt_terminal_report(run.assignment_id, status, error)
        if status == "completed":
            self.stats.completed.append(run.assignment_id)
            LOG.info("assignment %s completed", run.assignment_id)
        else:
            self.stats.failed.append(run.assignment_id)
            LOG.info("assignment %s failed: %s", run.assignment_id, error)

    def _settle(self, assignment_id: str, status: str) -> None:
        self._settled[assignment_id] = status
        try:
            settle_journal(self.config.state_dir, assignment_id, status)
        except OSError as exc:
            LOG.error("could not persist settle journal: %s", exc)

    def _flush_pending_reports(self) -> None:
        for assignment_id, (status, error) in sorted(self._pending.items()):
            self._attempt_terminal_report(assignment_id, status, error)

    def _record_pending(self, assignment_id: str, status: str, error: str | None) -> bool:
        # Durable pending handoff must precede the settle journal: a crash between
        # the two writes must leave the terminal report replayable, never lost.
        self._pending[assignment_id] = (status, error)
        try:
            record_pending_report(self.config.state_dir, assignment_id, status, error)
        except (OSError, StateError) as exc:
            LOG.error("could not persist pending terminal report for %s; "
                      "settle deferred so the assignment stays re-executable: %s",
                      assignment_id, exc)
            return False
        return True

    def _clear_pending(self, assignment_id: str) -> None:
        self._pending.pop(assignment_id, None)
        try:
            clear_pending_report(self.config.state_dir, assignment_id)
        except (OSError, StateError) as exc:
            LOG.error("could not clear pending terminal report for %s: %s", assignment_id, exc)

    def _attempt_terminal_report(self, assignment_id: str, status: str, error: str | None) -> None:
        assert self.credentials is not None
        for _ in range(max(1, self.config.report_retries)):
            try:
                self._guarded(self.client.report)(
                    self.credentials.node_id, self.credentials.node_token,
                    assignment_id, status, error)
            except ControllerError as exc:
                if exc.status_code in (404, 409):
                    LOG.info("terminal report for %s not accepted: %s", assignment_id, exc)
                    self._clear_pending(assignment_id)
                    return
                LOG.warning("terminal report for %s rejected: %s", assignment_id, exc)
            except httpx.HTTPError as exc:
                LOG.warning("terminal report for %s unreachable: %s", assignment_id, exc)
            else:
                self._clear_pending(assignment_id)
                return
            self.stop_event.wait(self.config.retry_backoff)
        LOG.error("gave up reporting %s=%s after %s attempts; kept pending for replay",
                  assignment_id, status, self.config.report_retries)

    def _shutdown_execution(self) -> None:
        run = self._current_run()
        if run is None:
            return
        run.context.request_cancel("shutdown")
        run.worker.join(self.config.stop_grace)
        if run.worker.is_alive():
            LOG.error("executor for %s ignored cancellation within %ss; abandoning thread",
                      run.assignment_id, self.config.stop_grace)
            with self._active_lock:
                self._active = None
            return
        self._reap_finished()
