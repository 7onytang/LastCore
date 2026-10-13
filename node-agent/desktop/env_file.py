"""Read a small, non-executable NODE_AGENT environment file."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

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


def validate_controller_url(value: str) -> str:
    """Controller credentials belong in Authorization, never in the URL.

    Use CPython's urlsplit; parsing errors are intentionally not echoed because
    even a malformed URL can contain user credentials or query tokens.
    """
    error = "Controller URL must be absolute HTTP(S), without userinfo, query or fragment"
    try:
        parsed = urlsplit(value)
        if (not value or any(c.isspace() or ord(c) < 32 for c in value)
                or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "?" in value or "#" in value):
            raise ValueError(error)
        parsed.port  # Validate port without letting its original text escape.
    except ValueError:
        raise ValueError(error) from None
    return value.rstrip("/")


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
    if values.get("NODE_AGENT_CONTROLLER_URL"):
        validate_controller_url(values["NODE_AGENT_CONTROLLER_URL"])
    return values
