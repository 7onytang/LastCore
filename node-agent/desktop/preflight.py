#!/usr/bin/env python3
"""LastCore node-agent desktop preflight check (macOS / Windows).

Verifies the host is ready to run the node agent: Python version, httpx,
state directory permissions and lock, endpoint port, controller reachability,
credentials state, memory and disk resources. Never prints token values.

Usage:
    python preflight.py [--controller-url URL] [--endpoint URL]
                        [--state-dir PATH] [--min-memory-mb N] [--json]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import socket
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env_file import read_env_file

MIN_PYTHON = (3, 12)
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def probe_memory_mb() -> int | None:
    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        statex = MEMORYSTATUSEX()
        statex.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(statex)):
            return int(statex.ullAvailPhys // (1024 * 1024))
        return None
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        avail = os.sysconf("SC_AVPHYS_PAGES")
        return int(page * avail // (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        return None


def probe_disk_free_mb(path: Path) -> int | None:
    try:
        return int(shutil.disk_usage(path).free // (1024 * 1024))
    except OSError:
        return None


def check_python(result: list) -> None:
    version = sys.version_info[:3]
    if version[:2] >= MIN_PYTHON:
        result.append((PASS, f"python {platform.python_version()} ({platform.python_implementation()}) >= 3.12"))
    else:
        result.append((FAIL, f"python {platform.python_version()} < required 3.12"))


def check_httpx(result: list) -> None:
    try:
        import httpx
        result.append((PASS, f"httpx {httpx.__version__} importable"))
    except Exception as exc:
        result.append((FAIL, f"httpx not importable ({type(exc).__name__}): pip install -r requirements.txt"))


def resolve_state_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".lastcore" / "node-agent"


def check_state_dir(result: list, state_dir: Path) -> None:
    ancestor = state_dir
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK):
        result.append((FAIL, f"nearest existing state dir ancestor not writable: {ancestor}"))
        return
    try:
        # Apply a private mode only when creating the final state directory.
        # pathlib leaves an existing directory's mode untouched, so an
        # unsafe pre-existing directory is diagnosed below rather than fixed
        # behind the user's back.
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        result.append((FAIL, f"cannot create state dir {state_dir}: {exc}"))
        return
    if state_dir == Path.home() / ".lastcore" / "node-agent":
        result.append((PASS, f"state dir default: {state_dir}"))
    else:
        result.append((WARN, f"state dir non-default: {state_dir}"))
    if sys.platform == "win32":
        profile = Path.home()
        try:
            state_dir.relative_to(profile)
            result.append((PASS, f"state dir under user profile {profile} (NTFS ACL inherited)"))
        except ValueError:
            result.append((WARN, f"state dir outside user profile; ensure only current user has access: {state_dir}"))
    else:
        mode = stat.S_IMODE(state_dir.stat().st_mode)
        if mode & 0o077:
            result.append((FAIL, f"state dir mode {mode:04o} is group/other accessible; expected 0700; "
                                  "restrict access manually before starting the agent"))
        else:
            result.append((PASS, f"state dir mode {mode:04o}"))
    try:
        fd, probe_name = tempfile.mkstemp(prefix=".lastcore-preflight-", dir=state_dir)
        os.close(fd)
        Path(probe_name).unlink()
        result.append((PASS, "state dir is writable"))
    except OSError as exc:
        result.append((FAIL, f"state dir is not writable ({type(exc).__name__})"))


def check_lock_primitive(result: list) -> None:
    import tempfile
    try:
        from lastcore_node_agent.credentials import acquire_state_lock
    except Exception:
        desktop_dir = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(desktop_dir))
        try:
            from lastcore_node_agent.credentials import acquire_state_lock
        except Exception as exc:
            result.append((FAIL, f"cannot import credentials module: {type(exc).__name__}: {exc}"))
            return
    try:
        with tempfile.TemporaryDirectory(prefix="lastcore-preflight-") as tmp:
            handle = acquire_state_lock(Path(tmp))
            if handle is None:
                result.append((FAIL, "state lock primitive failed on this platform"))
            else:
                handle.close()
                result.append((PASS, f"state lock primitive ok ({'msvcrt' if sys.platform == 'win32' else 'fcntl'})"))
    except Exception as exc:
        result.append((FAIL, f"state lock primitive error: {type(exc).__name__}: {exc}"))


def check_state_lock_in_use(result: list, state_dir: Path) -> None:
    try:
        from lastcore_node_agent.credentials import acquire_state_lock
    except Exception:
        return
    try:
        handle = acquire_state_lock(state_dir)
    except OSError as exc:
        result.append((FAIL, f"cannot inspect state dir lock ({type(exc).__name__})"))
        return
    if handle is None:
        result.append((WARN, f"state dir in use by another agent process (lock held): {state_dir}"))
    else:
        handle.close()
        result.append((PASS, "state dir lock free"))


def check_credentials(result: list, state_dir: Path) -> None:
    credentials_path = state_dir / "node-credentials.json"
    if not credentials_path.exists():
        token_set = bool(os.environ.get("NODE_AGENT_REGISTRATION_TOKEN"))
        if token_set:
            result.append((WARN, "no persisted credentials; will register with NODE_AGENT_REGISTRATION_TOKEN (bootstrap)"))
        else:
            result.append((FAIL, "no persisted credentials and no NODE_AGENT_REGISTRATION_TOKEN set"))
        return
    try:
        if os.name != "nt":
            mode = stat.S_IMODE(credentials_path.stat().st_mode)
            if mode & 0o077:
                result.append((FAIL, f"credentials file mode {mode:04o} is not private; expected 0600"))
                return
        data = json.loads(credentials_path.read_text(encoding="utf-8"))
        node_id = str(data.get("node_id", ""))
        if not node_id or not data.get("node_token"):
            raise ValueError("missing node_id/node_token")
        result.append((PASS, f"persisted credentials valid for node {node_id} (token not shown)"))
    except Exception as exc:
        result.append((FAIL, f"credentials file unreadable/corrupt: {credentials_path} ({type(exc).__name__}: {exc}); "
                             "fix or remove it manually (auto re-register is blocked by design)"))


def check_endpoint_port(result: list, endpoint: str | None) -> None:
    if not endpoint:
        result.append((WARN, "no --endpoint given; skipping port conflict check"))
        return
    parsed = urlparse(endpoint if "//" in endpoint else f"//{endpoint}", scheme="http")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        result.append((WARN, f"endpoint host {host!r} does not resolve locally ({exc}); skipping bind check"))
        return
    family, socktype, _, _, sockaddr = infos[0]
    probe = socket.socket(family, socktype)
    try:
        probe.bind(sockaddr)
        result.append((PASS, f"endpoint port free: {host}:{port}"))
    except OSError as exc:
        result.append((FAIL, f"endpoint port busy on {host}:{port} ({exc}); free it or change NODE_AGENT_ENDPOINT"))
    finally:
        probe.close()


def check_controller(result: list, controller_url: str | None, timeout: float) -> None:
    if not controller_url:
        result.append((WARN, "no --controller-url given; skipping controller reachability check"))
        return
    url = controller_url.rstrip("/") + "/healthz"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")[:200]
            result.append((PASS, f"controller reachable {url} -> {response.status} {body}"))
    except urllib.error.HTTPError as exc:
        result.append((FAIL, f"controller answered with error {exc.code} at {url}"))
    except Exception as exc:
        result.append((FAIL, f"controller unreachable {url} ({type(exc).__name__}: {exc}); "
                             "check address/firewall; agent will retry every 1s at startup"))


def check_resources(result: list, state_dir: Path, min_memory_mb: int) -> None:
    memory = probe_memory_mb()
    if memory is None:
        if min_memory_mb > 0:
            result.append((FAIL, f"available memory is unknown; cannot verify configured minimum "
                                 f"of {min_memory_mb} MB"))
        else:
            result.append((WARN, "available memory: unknown on this platform"))
    elif memory >= max(min_memory_mb, 256):
        result.append((PASS, f"available memory: {memory} MB"))
    else:
        result.append((FAIL, f"available memory {memory} MB below minimum {max(min_memory_mb, 256)} MB"))
    disk = probe_disk_free_mb(state_dir)
    if disk is None:
        result.append((WARN, f"disk free for {state_dir}: unknown"))
    elif disk < 1024:
        result.append((WARN, f"disk free {disk} MB below 1024 MB for {state_dir} (model caches need space)"))
    else:
        result.append((PASS, f"disk free: {disk} MB"))


def run_checks(controller_url: str | None, endpoint: str | None, state_dir: Path,
               min_memory_mb: int, timeout: float) -> list:
    result: list = []
    check_python(result)
    check_httpx(result)
    check_state_dir(result, state_dir)
    check_state_lock_in_use(result, state_dir)
    check_lock_primitive(result)
    check_credentials(result, state_dir)
    check_endpoint_port(result, endpoint)
    check_controller(result, controller_url, timeout)
    check_resources(result, state_dir, min_memory_mb)
    return result


def main(argv: list[str] | None = None) -> int:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--env-file")
    early_args, _ = bootstrap.parse_known_args(argv)
    if early_args.env_file:
        try:
            os.environ.update(read_env_file(early_args.env_file))
        except ValueError as exc:
            bootstrap.error(str(exc))

    parser = argparse.ArgumentParser(prog="preflight", description="LastCore node-agent desktop preflight check")
    parser.add_argument("--env-file", help="load NODE_AGENT settings from a private KEY=VALUE file")
    parser.add_argument("--controller-url", default=os.environ.get("NODE_AGENT_CONTROLLER_URL", ""))
    parser.add_argument("--endpoint", default=os.environ.get("NODE_AGENT_ENDPOINT", ""))
    parser.add_argument("--state-dir", default=os.environ.get("NODE_AGENT_STATE_DIR", ""))
    parser.add_argument("--min-memory-mb", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    state_dir = resolve_state_dir(args.state_dir or None)
    result = run_checks(args.controller_url or None, args.endpoint or None, state_dir,
                        args.min_memory_mb, args.timeout)
    if args.as_json:
        print(json.dumps({
            "platform": {"system": platform.system(), "release": platform.release(),
                         "machine": platform.machine(), "python": platform.python_version()},
            "state_dir": str(state_dir),
            "checks": [{"status": status, "message": message} for status, message in result],
            "ok": all(status != FAIL for status, _ in result),
        }, ensure_ascii=False, indent=2))
    else:
        print(f"LastCore node-agent preflight @ {platform.system()} {platform.release()} "
              f"({platform.machine()}) python {platform.python_version()}")
        for status, message in result:
            print(f"  [{status}] {message}")
    return 0 if all(status != FAIL for status, _ in result) else 1


if __name__ == "__main__":
    sys.exit(main())
