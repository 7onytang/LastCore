from __future__ import annotations

import json

import httpx


class ControllerError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"controller returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ControllerClient:
    def __init__(self, base_url: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
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
                  accepting_tasks: bool) -> dict:
        response = self._client.post(
            f"/v1/nodes/{node_id}/heartbeat",
            json={"available_memory_mb": available_memory_mb, "accepting_tasks": accepting_tasks},
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
               error: str | None = None) -> dict:
        response = self._client.post(
            f"/v1/nodes/{node_id}/assignments/{assignment_id}/report",
            json={"status": status, "error": error},
            headers=self._headers(node_token),
        )
        self._check(response)
        return response.json()
