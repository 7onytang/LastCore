from __future__ import annotations

import json
import hashlib

import httpx


class ControllerError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"controller returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ControllerClient:
    def __init__(self, base_url: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        self.origin = str(httpx.URL(base_url)).rstrip("/")
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                    transport=transport, trust_env=False)

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _headers(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = None
        if detail is None:
            detail = response.text[:512]
        elif not isinstance(detail, str):
            detail = json.dumps(detail)[:512]
        raise ControllerError(response.status_code, detail)

    def health(self) -> dict:
        response = self._client.get("/healthz")
        self._check(response)
        return response.json()

    def register(self, registration_token: str, payload: dict) -> dict:
        response = self._client.post("/v1/nodes/register", json=payload,
                                     headers=self._headers(registration_token))
        self._check(response)
        return response.json()

    def heartbeat(self, node_id: str, node_token: str, available_memory_mb: int,
                  accepting_tasks: bool, supported_workloads=None) -> dict:
        response = self._client.post(
            f"/v1/nodes/{node_id}/heartbeat",
            json={"available_memory_mb": available_memory_mb, "accepting_tasks": accepting_tasks,
                  **({"supported_workloads": list(supported_workloads)} if supported_workloads is not None else {})},
            headers=self._headers(node_token),
        )
        self._check(response)
        return response.json()

    def assignments(self, node_id: str, node_token: str) -> list:
        response = self._client.get(f"/v1/nodes/{node_id}/assignments",
                                    headers=self._headers(node_token))
        self._check(response)
        return response.json().get("assignments", [])

    def report(self, node_id: str, node_token: str, assignment_id: str, status: str,
               error: str | None = None, result=None) -> dict:
        payload = {"status": status, "error": error}
        if result is not None:
            payload["result"] = result
        response = self._client.post(
            f"/v1/nodes/{node_id}/assignments/{assignment_id}/report",
            json=payload,
            headers=self._headers(node_token),
        )
        self._check(response)
        return response.json()

    def report_result(self, node_id, node_token, assignment_id, status, error, result, binding):
        if (not isinstance(binding, dict) or binding.get("node_id") != node_id
                or binding.get("origin") != self.origin or binding.get("assignment_id") != assignment_id):
            raise ControllerError(503, "result binding unavailable; pending retained")
        response = self._client.post(f"/v1/nodes/{node_id}/assignments/{assignment_id}/report",
            json={"status": status, "error": error, "workload_result": result}, headers=self._headers(node_token))
        self._check(response)
        try:
            task = response.json()
            if not isinstance(task, dict) or not isinstance(task.get("assignments"), list):
                raise ValueError("invalid acknowledgement")
            request_hash = hashlib.sha256(json.dumps(task.get("request"), sort_keys=True, separators=(",", ":"),
                                                   allow_nan=False).encode()).hexdigest()
            matches = [row for row in task["assignments"] if isinstance(row, dict) and row.get("id") == assignment_id]
            if (task.get("id") != binding.get("task_id") or len(matches) != 1
                    or any(matches[0].get(key) != binding.get(key)
                           for key in ("task_id", "node_id", "layer_start", "layer_end"))
                    or (task.get("request") or {}).get("model_id") != binding.get("model_id")
                    or matches[0].get("status") != status
                    or not isinstance(matches[0].get("workload_result"), dict)
                    or any(type(value) is not int for value in matches[0]["workload_result"].values())
                    or matches[0]["workload_result"] != result
                    or request_hash != binding.get("request_sha256")):
                raise ValueError("acknowledgement mismatch")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ControllerError(503, "result acknowledgement mismatch; pending retained") from None
        return task
