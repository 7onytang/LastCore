from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

ENV_PREFIX = "NODE_AGENT_"


@dataclass(frozen=True)
class AgentConfig:
    controller_url: str
    endpoint: str
    available_memory_mb: int
    compute_weight: float = 1.0
    node_name: str = ""
    platform_name: str = ""
    state_dir: Path = Path.home() / ".lastcore" / "node-agent"
    heartbeat_interval: float = 10.0
    poll_interval: float = 1.0
    accepting_tasks: bool = True
    executor: str = "demo"
    registration_token: str = ""
    report_retries: int = 3
    retry_backoff: float = 0.5
    stop_grace: float = 10.0
    request_timeout: float = 10.0

    def __post_init__(self):
        if not self.node_name:
            object.__setattr__(self, "node_name", platform.node() or "unknown-node")
        if not self.platform_name:
            object.__setattr__(self, "platform_name", platform.platform())

    def registration_payload(self) -> dict:
        return {
            "name": self.node_name,
            "platform": self.platform_name,
            "endpoint": self.endpoint,
            "available_memory_mb": self.available_memory_mb,
            "compute_weight": self.compute_weight,
        }

    @classmethod
    def from_env(cls, env: dict | None = None) -> "AgentConfig":
        env = os.environ if env is None else env

        def get(key: str, default: str = "") -> str:
            return env.get(ENV_PREFIX + key, default)

        return cls(
            controller_url=get("CONTROLLER_URL").rstrip("/"),
            endpoint=get("ENDPOINT"),
            available_memory_mb=int(get("AVAILABLE_MEMORY_MB", "0") or 0),
            compute_weight=float(get("COMPUTE_WEIGHT", "1") or 1),
            node_name=get("NAME"),
            platform_name=get("PLATFORM"),
            state_dir=Path(get("STATE_DIR", str(Path.home() / ".lastcore" / "node-agent"))).expanduser(),
            heartbeat_interval=float(get("HEARTBEAT_INTERVAL", "10") or 10),
            poll_interval=float(get("POLL_INTERVAL", "1") or 1),
            accepting_tasks=get("NO_ACCEPT", "") == "",
            executor=get("EXECUTOR", "demo") or "demo",
            registration_token=get("REGISTRATION_TOKEN"),
            request_timeout=float(get("REQUEST_TIMEOUT", "10") or 10),
        )
