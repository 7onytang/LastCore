from __future__ import annotations

import logging
import hashlib
import json
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
    load_pending_report_results,
    load_pending_reports,
    load_settled,
    record_pending_report,
    save_credentials,
    settle_journal,
)
from .executor import ExecutionContext, ExecutionCancelled, Executor, SliceDemoExecutor, load_executor

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
    result_payload: object | None = None


class NodeAgent:
    def __init__(self, config: AgentConfig, client: ControllerClient,
                 clock: Callable[[], float] = time.time,
                 executor_factory: Callable[[], Executor] | None = None,
                 stats: RunStats | None = None,
                 observer: Callable[[dict], None] | None = None, control_reader=None):
        self.config = config
        self.observer = observer
        self.client = client
        self.clock = clock
        self.supported_workloads = SliceDemoExecutor.supported_workloads if executor_factory is None and config.executor == "demo" else ()
        self.executor_factory = executor_factory or (lambda: load_executor(config.executor))
        self.stats = stats or RunStats()
        self.credentials: NodeCredentials | None = None
        self.stop_event = threading.Event()
        self._active: _ActiveRun | None = None
        self._active_lock = threading.Lock()
        self._settled: dict[str, str] = {}
        self._pending: dict[str, tuple[str, str | None]] = {}
        self._pending_results: dict[str, object] = {}
        self._heartbeat_lock = threading.Lock()
        self._last_heartbeat_ok = 0.0
        self._credential_error: str | None = None
        self._assignment_lookup_supported = True
        self.control_reader = control_reader
        self._admission_lock = threading.RLock()
        self._admission_desired = config.accepting_tasks
        self._admission_confirmed = None
        self._admission_generation = 0
        self._admission_ack_generation = -1
        self._admission_request_id = None
        self._admission_confirmed_request_id = None
        self._admission_requests = {}

    def admission_state(self) -> dict:
        with self._admission_lock:
            return {'desired': self._admission_desired, 'confirmed': self._admission_confirmed,
                    'request_id': self._admission_request_id,
                    'confirmed_request_id': self._admission_confirmed_request_id,
                    'status': 'confirmed' if self._admission_ack_generation == self._admission_generation else 'pending'}

    def set_accepting_tasks(self, value: bool, request_id: str | None = None) -> None:
        if type(value) is not bool:
            raise ValueError('accepting_tasks must be a boolean')
        if request_id is not None:
            from .local_control import validate_request_id
            validate_request_id(request_id)
        with self._admission_lock:
            if request_id is not None and request_id in self._admission_requests:
                if self._admission_requests[request_id] != value:
                    raise ValueError('request id reused with different command')
                return
            if request_id is not None:
                self._admission_requests[request_id] = value
            self._admission_generation += 1
            self._admission_desired = value
            self._admission_request_id = request_id
        self._notify()

    def _accepting_new_work(self) -> bool:
        with self._admission_lock:
            return (not self.stop_event.is_set() and self._admission_desired and self._admission_confirmed is True
                    and self._admission_ack_generation == self._admission_generation)

    def _consume_controls(self) -> None:
        if self.control_reader is not None:
            from .local_control import ControlError
            try:
                if self.control_reader.consume(self):
                    self._notify(control_error=None)
            except (OSError, ControlError):
                self._notify(control_error='invalid-command')

    def _notify(self, **updates) -> None:
        if self.observer is not None:
            try: self.observer({**updates, 'admission': self.admission_state()})
            except Exception: LOG.warning("local UI observation unavailable")

    def _task_details(self, row: dict) -> dict:
        return {"task_id": row.get("task_id"), "assignment_id": row.get("id"),
                "model_id": str((row.get("request") or {}).get("model_id", "")),
                "executor": self.config.executor, "layer_start": row.get("layer_start"),
                "layer_end": row.get("layer_end"), "status": "preparing",
                "cleanup_confirmed": False, "report_status": "running-reported", "result": None}

    def ensure_credentials(self) -> NodeCredentials:
        credentials = load_credentials(self.config.state_dir)
        if credentials is None:
            if not self.config.registration_token:
                raise RuntimeError("no persisted node credentials and no registration token configured")
            response = self.client.register(self.config.registration_token,
                                            {**self.config.registration_payload(), "supported_workloads": list(self.supported_workloads)})
            credentials = NodeCredentials.from_record(response)
            save_credentials(self.config.state_dir, credentials)
            LOG.info("registered as node %s", credentials.node_id)
        else:
            LOG.info("reusing persisted credentials for node %s", credentials.node_id)
        self.credentials = credentials
        return credentials

    def run_forever(self) -> None:
        try:
            self._settled = load_settled(self.config.state_dir)
            self._pending = load_pending_reports(self.config.state_dir)
            self._pending_results = load_pending_report_results(self.config.state_dir)
            joined = self._join_controller()
        except (OSError, StateError):
            self.stop_event.set()
            self._notify(connection="recovery-required")
            raise
        heartbeat_thread = None
        if joined:
            heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                                                name="lastcore-heartbeat", daemon=True)
            heartbeat_thread.start()
            watchdog_thread = threading.Thread(target=self._watchdog_loop,
                                               name="lastcore-watchdog", daemon=True)
            watchdog_thread.start()
        try:
            poll_delay = self.config.poll_interval
            poll_cap = max(poll_delay, 30.0)
            while joined and not self.stop_event.is_set() and self._credential_error is None:
                try:
                    self._poll_once()
                except httpx.HTTPError as exc:
                    self._notify(poll_status="unavailable")
                    LOG.warning("assignments poll failed: %s", exc)
                    poll_delay = min(poll_cap, max(self.config.poll_interval, poll_delay * 2))
                except ControllerError as exc:
                    self._notify(poll_status="rejected")
                    LOG.warning("assignments poll rejected: %s", exc)
                    poll_delay = min(poll_cap, max(self.config.poll_interval, poll_delay * 2))
                else:
                    poll_delay = self.config.poll_interval
                self.stop_event.wait(poll_delay)
            if self._credential_error:
                raise CredentialRejected(self._credential_error)
        finally:
            self.stop_event.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(1.0)
            self._shutdown_execution()
            self._notify(connection="stopped")

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
                self._notify(connection="unreachable")
                LOG.warning("controller not reachable yet (%s), retrying in 1s", exc)
                self.stop_event.wait(1.0)
        return False

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.config.heartbeat_interval):
            try:
                self._send_heartbeat()
            except CredentialRejected as exc:
                self._notify(connection="rejected")
                self._credential_error = str(exc)
                self.stop_event.set()
                return
            except (httpx.HTTPError, ControllerError) as exc:
                self._notify(connection="heartbeat-failed")
                self.stats.heartbeats_failed += 1
                LOG.warning("heartbeat failed: %s", exc)

    def _watchdog_loop(self) -> None:
        # Runs independently of assignments polling, so local deadline/heartbeat-TTL
        # cancellation still fires while GET is blocked or failing (total outage).
        while not self.stop_event.wait(min(self.config.watchdog_interval, self.config.poll_interval, 1.0)):
            self._consume_controls()
            run = self._current_run()
            if run is None:
                continue
            if self.clock() >= run.context.deadline:
                run.context.request_cancel("deadline")
            elif self._heartbeat_stalled():
                run.context.request_cancel("heartbeat-stall")

    def _send_heartbeat(self) -> None:
        assert self.credentials is not None
        self._consume_controls()
        with self._admission_lock:
            generation = self._admission_generation
            desired = self._admission_desired
            request_id = self._admission_request_id
        response = self._guarded(self.client.heartbeat)(
            self.credentials.node_id, self.credentials.node_token,
            self.config.available_memory_mb, desired,
            supported_workloads=self.supported_workloads)
        with self._admission_lock:
            if (generation == self._admission_generation and isinstance(response, dict)
                    and type(response.get('accepting_tasks')) is bool and response['accepting_tasks'] == desired):
                self._admission_confirmed = desired
                self._admission_ack_generation = generation
                self._admission_confirmed_request_id = request_id
        with self._heartbeat_lock:
            self._last_heartbeat_ok = self.clock()
        self.stats.heartbeats_ok += 1
        self._notify(connection="connected",node_id=self.credentials.node_id,last_heartbeat=self._last_heartbeat_ok,
                     heartbeat_ttl=self.credentials.heartbeat_ttl_seconds)

    def _poll_once(self) -> None:
        assert self.credentials is not None
        self._consume_controls()
        rows = self._guarded(self.client.assignments)(self.credentials.node_id,
                                                      self.credentials.node_token)
        self.stats.polls += 1
        self._notify(poll_status="ok")
        run = self._current_run()
        if run is not None:
            self._watch_active(rows, run)
        self._reap_finished()
        self._flush_pending_reports()
        if self._pending_results:
            return  # Unacknowledged numeric outcomes fence new work, including after restart.
        if self._current_run() is None and self._accepting_new_work():
            candidate = self._select_assignment(rows)
            if candidate is not None:
                self._start_assignment(candidate)

    def _current_run(self) -> _ActiveRun | None:
        with self._active_lock:
            return self._active

    def has_live_execution(self) -> bool:
        """Read actual executor liveness without reaping, reporting or releasing it."""
        with self._active_lock:
            return self._active is not None and self._active.worker.is_alive()

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
        row = next((row for row in rows if row.get("id") == run.assignment_id
                    and row.get("task_id") == run.task_id), None)
        if row is None and self._assignment_lookup_supported:
            assert self.credentials is not None
            try:
                row = self._guarded(self.client.assignment)(
                    self.credentials.node_id, self.credentials.node_token, run.assignment_id)
            except ControllerError as exc:
                # FastAPI's default route-missing response differs from the owned-ID
                # endpoint's "assignment not found". Neither is a stop acknowledgement.
                if (exc.status_code == 404 and exc.detail == "Not Found") or exc.status_code in (405, 501):
                    self._assignment_lookup_supported = False
                    LOG.warning("controller lacks assignment lookup; using legacy latest-100 history "
                                "until agent restart; cancellation outside that window may wait for deadline")
                else:
                    LOG.warning("active assignment lookup failed: %s", exc)
                return
            except httpx.HTTPError as exc:
                LOG.warning("active assignment lookup unavailable: %s", exc)
                return
        if (row is not None and row.get("id") == run.assignment_id
                and row.get("task_id") == run.task_id
                and row.get("task_status") in ("cancelled", "failed")):
            run.context.request_cancel("task-terminal")

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
        if not self._accepting_new_work():
            return
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
        self._notify(task=self._task_details(row))
        try:
            executor = self.executor_factory()
        except Exception as exc:
            LOG.exception("executor unavailable for %s", assignment_id)
            error = f"executor unavailable: {type(exc).__name__}: {exc}"
            if self._record_pending(assignment_id, "failed", error):
                self._settle(assignment_id, "failed")
            self._attempt_terminal_report(assignment_id, "failed", error)
            self.stats.failed.append(assignment_id)
            self._notify(task_update={"assignment_id":assignment_id,"status":"failed",
                "error_code":"executor_unavailable","cleanup_confirmed":True,
                **({"report_status":"pending"} if assignment_id in self._pending else {})})
            return
        context = ExecutionContext(
            clock=self.clock,
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
        with self._admission_lock:
            if not self._accepting_new_work():
                return
            with self._active_lock:
                self._active = run
            self._notify(task_update={"assignment_id":assignment_id,"status":"running"})
            worker.start()
        self.stats.started.append(assignment_id)
        LOG.info("started assignment %s (task %s) layers [%s, %s)",
                 assignment_id, task_id, context.layer_start, context.layer_end)

    def _execute(self, context: ExecutionContext, executor: Executor) -> None:
        status, error = "completed", None
        payload = None
        try:
            produced = executor.run(context)
            workload = ((context.raw or {}).get("request") or {}).get("workload", "linear")
            if workload in ("pi", "log_stats"):
                if produced is None:
                    if context.cancel.is_set():
                        raise ExecutionCancelled(context.cancel_reason or "cancelled")
                    raise ValueError("typed executor returned no workload result")
                payload = produced
            # Legacy executor return values were never a report contract.
        except ExecutionCancelled as exc:
            status, error = "cancelled-local", exc.reason
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            LOG.exception("executor failed for assignment %s", context.assignment_id)
        run = self._current_run()
        if run is not None and run.context is context:
            run.result = (status, error)
            run.result_payload = payload if status == "completed" else None
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
            self._notify(task_update={"assignment_id":run.assignment_id,"status":"stopped-local",
                "cleanup_confirmed":True,"report_status":"not-sent",
                "error_code":error if error in {"shutdown","deadline","heartbeat-stall","task-terminal"} else "local-cancel"})
            LOG.info("assignment %s stopped locally (%s)", run.assignment_id, error)
            return
        payload = getattr(run, "result_payload", None)
        binding = {"origin": self.client.origin, "assignment_id": run.assignment_id, "task_id": run.task_id, "node_id": self.credentials.node_id, "model_id": run.context.model_id,
                   "layer_start": run.context.layer_start, "layer_end": run.context.layer_end,
                   "request_sha256": hashlib.sha256(json.dumps(run.context.raw["request"], sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode()).hexdigest()} if payload is not None else None
        if self._record_pending(run.assignment_id, status, error, payload, binding):
            self._settle(run.assignment_id, status)
        elif payload is not None:
            self.stop_event.set()
            self._notify(task_update={"assignment_id": run.assignment_id, "report_status": "recovery-required"})
            return  # Never publish a typed outcome that was not durably saved.
        self._attempt_terminal_report(run.assignment_id, status, error,
                                      payload, binding)
        self._notify(task_update={"assignment_id":run.assignment_id,"status":status,
            "cleanup_confirmed":True,"error_code":"execution_failed" if error else None,
            "result":run.context.result_summary,
            **({"report_status":"pending"} if run.assignment_id in self._pending else {})})
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
        except StateError:
            self.stop_event.set()
            self._notify(connection="recovery-required")
            raise
        except OSError as exc:
            LOG.error("could not persist settle journal: %s", exc)

    def _flush_pending_reports(self) -> None:
        try:
            loaded = load_pending_report_results(self.config.state_dir)
            if any(assignment_id not in loaded or loaded[assignment_id] != saved
                   for assignment_id, saved in self._pending_results.items()):
                raise StateError("known typed outcome changed or disappeared; preserve for recovery")
            self._pending_results = loaded
        except (OSError, StateError):
            self.stop_event.set()
            self._notify(connection="recovery-required")
            raise
        for assignment_id, (status, error) in sorted(self._pending.items()):
            self._attempt_terminal_report(assignment_id, status, error,
                                          *(self._pending_results.get(assignment_id) or (None, None)))

    def _record_pending(self, assignment_id: str, status: str, error: str | None, result=None, binding=None) -> bool:
        # Typed outcomes are immutable snapshots read back from the durable journal.
        if result is None:
            self._pending[assignment_id] = (status, error)
        try:
            record_pending_report(self.config.state_dir, assignment_id, status, error, result, binding)
            if result is not None:
                saved = load_pending_report_results(self.config.state_dir)[assignment_id]
                self._pending_results[assignment_id] = saved
                self._pending[assignment_id] = (status, error)
        except (OSError, StateError) as exc:
            LOG.error("could not persist terminal outcome for %s; recovery required: %s", assignment_id, exc)
            if result is not None:
                self.stop_event.set()
            return False
        return True

    def _clear_pending(self, assignment_id: str) -> None:
        try:
            clear_pending_report(self.config.state_dir, assignment_id)
        except (OSError, StateError) as exc:
            LOG.error("could not clear pending terminal report for %s: %s", assignment_id, exc)
            return
        # Keep replay state and the typed-outcome fence until durable cleanup succeeds.
        self._pending.pop(assignment_id, None)
        self._pending_results.pop(assignment_id, None)

    def _attempt_terminal_report(self, assignment_id: str, status: str, error: str | None,
                                 result=None, binding=None) -> None:
        assert self.credentials is not None
        for _ in range(max(1, self.config.report_retries)):
            try:
                if result is None:
                    self._guarded(self.client.report)(self.credentials.node_id, self.credentials.node_token,
                                                      assignment_id, status, error)
                else:
                    self._guarded(self.client.report_result)(self.credentials.node_id, self.credentials.node_token,
                                                             assignment_id, status, error, result, binding)
            except ControllerError as exc:
                if result is not None and exc.status_code in (404, 409):
                    self.stop_event.set()
                    self._notify(task_update={"assignment_id": assignment_id, "report_status": "recovery-required"})
                    return
                if exc.status_code in (404, 409):
                    LOG.info("terminal report for %s not accepted: %s", assignment_id, exc)
                    self._clear_pending(assignment_id)
                    self._notify(task_update={"assignment_id":assignment_id,"report_status":"terminal-conflict"})
                    return
                LOG.warning("terminal report for %s rejected: %s", assignment_id, exc)
            except httpx.HTTPError as exc:
                LOG.warning("terminal report for %s unreachable: %s", assignment_id, exc)
            else:
                self._clear_pending(assignment_id)
                self._notify(task_update={"assignment_id":assignment_id,"report_status":"acknowledged"})
                return
            self.stop_event.wait(self.config.retry_backoff)
        LOG.error("gave up reporting %s=%s after %s attempts; kept pending for replay",
                  assignment_id, status, self.config.report_retries)

    def _shutdown_execution(self) -> None:
        run = self._current_run()
        if run is None:
            return
        self._notify(task_update={"assignment_id":run.assignment_id,"status":"stopping","cleanup_confirmed":False})
        run.context.request_cancel("shutdown")
        run.worker.join(self.config.stop_grace)
        if run.worker.is_alive():
            self._notify(task_update={"assignment_id":run.assignment_id,"status":"stop-unconfirmed",
                "cleanup_confirmed":False,"error_code":"cleanup_timeout","report_status":"not-sent"})
            LOG.error("executor for %s ignored cancellation within %ss; retaining isolation",
                      run.assignment_id, self.config.stop_grace)
            return
        self._reap_finished()
