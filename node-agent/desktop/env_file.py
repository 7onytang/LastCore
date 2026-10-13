"""Read a small, non-executable NODE_AGENT environment file."""
from __future__ import annotations

import os
import stat
from pathlib import Path

ALLOWED_KEYS = frozenset({
    "NODE_AGENT_CONTROLLER_URL",
    "NODE_AGENT_ENDPOINT",
    "NODE_AGENT_AVAILABLE_MEMORY_MB",
    "NODE_AGENT_COMPUTE_WEIGHT",
    "NODE_AGENT_NAME",
    "NODE_AGENT_PLATFORM",
    "NODE_AGENT_STATE_DIR",
    "NODE_AGENT_HEARTBEAT_INTERVAL",
    "NODE_AGENT_POLL_INTERVAL",
    "NODE_AGENT_NO_ACCEPT",
    "NODE_AGENT_EXECUTOR",
    "NODE_AGENT_MODEL_CONFIG",
    "NODE_AGENT_WEIGHTS",
    "NODE_AGENT_COMPLETE_AFTER",
    "NODE_AGENT_REGISTRATION_TOKEN",
    "NODE_AGENT_REQUEST_TIMEOUT",
})


def read_env_file(path: str | Path) -> dict[str, str]:
    """Parse KEY=VALUE lines without shell evaluation or value interpolation."""
    source = Path(path).expanduser()
    try:
        if os.name != "nt" and stat.S_IMODE(source.stat().st_mode) & 0o077:
            raise ValueError(f"environment file {source} is not private; set its mode to 0600")
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read environment file {source}: {type(exc).__name__}") from None

    values: dict[str, str] = {}
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment file {source} line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in ALLOWED_KEYS:
            raise ValueError(f"unsupported environment key {key!r} at {source} line {line_number}")
        if key in values:
            raise ValueError(f"duplicate environment key {key!r} at {source} line {line_number}")
        values[key] = value
    return values
