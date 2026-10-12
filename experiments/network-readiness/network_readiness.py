#!/usr/bin/env python3
"""Small, dependency-free TCP readiness probe for explicitly named devices.

The protocol is deliberately only an echo/measurement protocol.  It does not
discover peers, open firewall rules, traverse NAT, or carry model tensors.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import ipaddress
import json
from pathlib import Path
import platform
import socket
import struct
import subprocess
import sys
import time
from typing import Any


SCHEMA_VERSION = 1
PROTOCOL = "LCNR/1"
MAGIC = b"LCNR1\0"
HEADER = struct.Struct("!6sI")
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_PAYLOAD_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 3.0


class ProbeError(Exception):
    """An expected CLI/protocol failure that should be rendered as JSON."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def emit_json(value: Any, stream: Any = sys.stdout) -> None:
    json.dump(value, stream, ensure_ascii=False, sort_keys=True)
    stream.write("\n")
    stream.flush()


def run_short_command(argv: list[str]) -> tuple[str, str | None]:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", None
    return completed.stdout.strip(), completed.stderr.strip() or None


def parse_int(value: str) -> int:
    try:
        return int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc


def parse_positive_int(value: str) -> int:
    parsed = parse_int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_port(value: str) -> int:
    parsed = parse_int(value)
    if not 0 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535")
    return parsed


def parse_nonnegative_int(value: str) -> int:
    parsed = parse_int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def parse_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_nonnegative_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def memory_info() -> dict[str, Any]:
    """Return a best-effort, explicitly labelled available-memory reading."""

    system = platform.system()
    total: int | None = None
    available: int | None = None
    method = "unavailable"
    errors: list[str] = []

    if system == "Windows":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_phys", ctypes.c_ulonglong),
                ("avail_phys", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("avail_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("avail_virtual", ctypes.c_ulonglong),
                ("avail_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            total = int(status.total_phys)
            available = int(status.avail_phys)
            method = "Windows GlobalMemoryStatusEx avail_phys"
        else:
            errors.append("GlobalMemoryStatusEx failed")
    elif system == "Linux":
        try:
            values: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, separator, rest = line.partition(":")
                if separator:
                    fields = rest.strip().split()
                    if fields and fields[0].isdigit():
                        values[key] = int(fields[0]) * (1024 if len(fields) > 1 and fields[1] == "kB" else 1)
            total = values.get("MemTotal")
            available = values.get("MemAvailable")
            method = "/proc/meminfo MemAvailable"
        except (OSError, UnicodeError) as exc:
            errors.append(f"/proc/meminfo: {exc}")
    elif system == "Darwin":
        total_text, total_error = run_short_command(["sysctl", "-n", "hw.memsize"])
        if total_text and total_text.isdigit():
            total = int(total_text)
        elif total_error:
            errors.append(f"sysctl hw.memsize: {total_error}")
        vm_text, vm_error = run_short_command(["vm_stat"])
        if vm_text:
            page_size = 4096
            page_size_text, _ = run_short_command(["sysctl", "-n", "vm.pagesize"])
            if page_size_text.isdigit():
                page_size = int(page_size_text)
            pages: dict[str, int] = {}
            for line in vm_text.splitlines():
                key, separator, value = line.partition(":")
                if separator:
                    digits = "".join(character for character in value if character.isdigit())
                    if digits:
                        pages[key.strip()] = int(digits)
            # This is a conservative approximation of reusable memory.  The
            # method is named so callers do not mistake it for a pressure API.
            available = sum(pages.get(key, 0) for key in ("Pages free", "Pages inactive", "Pages speculative")) * page_size
            method = "macOS vm_stat free+inactive+speculative"
        elif vm_error:
            errors.append(f"vm_stat: {vm_error}")
    else:
        errors.append(f"unsupported operating system: {system}")

    result: dict[str, Any] = {
        "total_bytes": total,
        "available_bytes": available,
        "available_method": method,
    }
    if errors:
        result["errors"] = errors
    return result


def device_info() -> dict[str, Any]:
    cpu_brand = platform.processor() or None
    model = None
    if platform.system() == "Darwin":
        model, _ = run_short_command(["sysctl", "-n", "hw.model"])
        cpu_brand, _ = run_short_command(["sysctl", "-n", "machdep.cpu.brand_string"])
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "cpu": cpu_brand,
        "model": model,
        "python": platform.python_version(),
    }


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_bind_host(host: str, allow_non_loopback: bool) -> None:
    if host in {"", "0.0.0.0", "::"}:
        raise ProbeError("wildcard bind is disabled; select the named LAN address explicitly")
    if not is_loopback_host(host) and not allow_non_loopback:
        raise ProbeError(
            f"refusing non-loopback bind {host!r}; use a specific LAN address with "
            "--allow-non-loopback after checking the device and firewall policy"
        )


def validate_payload_bytes(value: int) -> None:
    if not 0 <= value <= MAX_PAYLOAD_BYTES:
        raise ProbeError(f"payload must be between 0 and {MAX_PAYLOAD_BYTES} bytes")


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ProbeError("peer closed before the complete frame arrived")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_frame(sock: socket.socket, allow_eof: bool = False) -> bytes | None:
    first = sock.recv(HEADER.size)
    if not first and allow_eof:
        return None
    if len(first) != HEADER.size:
        first += recv_exact(sock, HEADER.size - len(first))
    magic, length = HEADER.unpack(first)
    if magic != MAGIC:
        raise ProbeError("invalid LCNR/1 frame magic")
    if length > MAX_PAYLOAD_BYTES:
        raise ProbeError(f"peer requested an excessive frame: {length} bytes")
    return recv_exact(sock, length)


def send_frame(sock: socket.socket, payload: bytes) -> int:
    frame = HEADER.pack(MAGIC, len(payload)) + payload
    sock.sendall(frame)
    return len(frame)


def open_listener(host: str, port: int) -> socket.socket:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    last_error: OSError | None = None
    for family, socktype, protocol, _canonname, sockaddr in infos:
        listener = socket.socket(family, socktype, protocol)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(sockaddr)
            listener.listen(16)
            listener.settimeout(0.5)
            return listener
        except OSError as exc:
            last_error = exc
            listener.close()
    raise ProbeError(f"cannot bind {host}:{port}: {last_error}")


def server_connection(
    conn: socket.socket,
    address: Any,
    timeout_seconds: float,
    response_delay_seconds: float,
) -> dict[str, Any]:
    conn.settimeout(timeout_seconds)
    written = 0
    read = 0
    frames = 0
    started = time.perf_counter()
    try:
        while True:
            frame = receive_frame(conn, allow_eof=True)
            if frame is None:
                break
            read += HEADER.size + len(frame)
            if response_delay_seconds:
                time.sleep(response_delay_seconds)
            written += send_frame(conn, frame)
            frames += 1
    except (ConnectionError, OSError, ProbeError) as exc:
        return {
            "status": "peer_error",
            "peer": repr(address),
            "frames": frames,
            "bytes_read": read,
            "bytes_written": written,
            "elapsed_seconds": time.perf_counter() - started,
            "error": str(exc),
        }
    finally:
        conn.close()
    return {
        "status": "ok",
        "peer": repr(address),
        "frames": frames,
        "bytes_read": read,
        "bytes_written": written,
        "elapsed_seconds": time.perf_counter() - started,
    }


def serve(args: argparse.Namespace) -> int:
    validate_bind_host(args.host, args.allow_non_loopback)
    listener = open_listener(args.host, args.port)
    bound = listener.getsockname()
    emit_json(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "listening",
            "command": "serve",
            "protocol": PROTOCOL,
            "bind": {
                "requested_host": args.host,
                "actual_host": bound[0],
                "port": bound[1],
                "non_loopback_allowed": args.allow_non_loopback,
                "reject_connections": args.reject_connections,
            },
            "device": device_info(),
            "memory": memory_info(),
            "sampling": "sequential TCP echo frames; no peer discovery or model traffic",
        }
    )
    handled = 0
    stopped_by_signal = False
    try:
        while not args.max_connections or handled < args.max_connections:
            try:
                conn, address = listener.accept()
            except socket.timeout:
                continue
            handled += 1
            if args.reject_connections:
                conn.close()
                event = {
                    "status": "rejected_by_server",
                    "peer": repr(address),
                    "bytes_read": 0,
                    "bytes_written": 0,
                    "frames": 0,
                }
            else:
                event = server_connection(conn, address, args.timeout, args.response_delay)
            event["connection_index"] = handled
            emit_json(event)
    except KeyboardInterrupt:
        stopped_by_signal = True
    finally:
        listener.close()
    emit_json(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "stopped",
            "command": "serve",
            "connections_handled": handled,
            "cleanup": {"listener_closed": True, "active_connections": 0},
            "stopped_by_signal": stopped_by_signal,
        }
    )
    return 0


def probe_once(
    host: str,
    port: int,
    timeout_seconds: float,
    payload: bytes,
    cleanup: dict[str, int] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    written = 0
    read = 0
    conn = socket.create_connection((host, port), timeout=timeout_seconds)
    try:
        with conn:
            conn.settimeout(timeout_seconds)
            handshake_start = time.perf_counter()
            written += send_frame(conn, b"")
            response = receive_frame(conn)
            read += HEADER.size
            if response != b"":
                raise ProbeError("handshake response was not an empty LCNR frame")
            handshake_rtt = time.perf_counter() - handshake_start

            payload_start = time.perf_counter()
            written += send_frame(conn, payload)
            echoed = receive_frame(conn)
            read += HEADER.size + len(echoed or b"")
            payload_elapsed = time.perf_counter() - payload_start
            if echoed != payload:
                raise ProbeError("echo payload did not match the sent payload")
    finally:
        # create_connection returns only after connect succeeds. Count it after
        # the context manager has closed it, including protocol/timeout errors.
        if cleanup is not None:
            cleanup["connections_closed"] += 1
    total_elapsed = time.perf_counter() - started
    payload_rate = len(payload) / payload_elapsed if payload_elapsed else None
    return {
        "status": "ok",
        "handshake_rtt_ms": handshake_rtt * 1000,
        "payload_round_trip_seconds": payload_elapsed,
        "payload_bytes": len(payload),
        "payload_round_trip_bytes_per_second": payload_rate,
        "bytes_written": written,
        "bytes_read": read,
        "elapsed_seconds": total_elapsed,
        "connection_closed": True,
    }


def probe(args: argparse.Namespace) -> int:
    validate_payload_bytes(args.payload_bytes)
    payload = bytes(index % 251 for index in range(args.payload_bytes))
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "command": "probe",
        "protocol": PROTOCOL,
        "started_at": utc_now(),
        "device": device_info(),
        "memory": memory_info(),
        "target": {"host": args.host, "port": args.port},
        "options": {
            "timeout_seconds": args.timeout,
            "payload_bytes": args.payload_bytes,
            "samples": args.samples,
        },
        "sampling": {
            "method": "one TCP connection per sample; empty frame RTT then deterministic echo payload",
            "payload_rate_unit": "payload bytes per second over payload send+echo round trip",
            "wire_byte_accounting": "bytes passed to sendall/recv, including LCNR headers",
            "limitations": [
                "does not represent model tensor or hidden-state traffic",
                "does not convert network rate to tokens per second",
                "does not discover peers or test NAT/CGNAT traversal",
            ],
        },
        "samples": [],
        "cleanup": {"connections_closed": 0},
    }
    try:
        for index in range(1, args.samples + 1):
            sample = probe_once(args.host, args.port, args.timeout, payload, result["cleanup"])
            sample["index"] = index
            result["samples"].append(sample)
    except (ConnectionError, OSError, ProbeError, TimeoutError) as exc:
        result["status"] = "error"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        emit_json(result)
        return 2
    rates = [sample["payload_round_trip_bytes_per_second"] for sample in result["samples"]]
    rtts = [sample["handshake_rtt_ms"] for sample in result["samples"]]
    result["aggregate"] = {
        "samples_completed": len(result["samples"]),
        "handshake_rtt_ms_min": min(rtts),
        "handshake_rtt_ms_max": max(rtts),
        "handshake_rtt_ms_mean": sum(rtts) / len(rtts),
        "payload_round_trip_bytes_per_second_mean": sum(rates) / len(rates),
    }
    emit_json(result)
    return 0


def find_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def terminate_child(process: subprocess.Popen[str]) -> tuple[int | None, bool]:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
    return process.returncode, process.poll() is not None


def start_test_server(delay: float, reject_connections: bool = False) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--max-connections",
        "1",
        "--timeout",
        "2",
        "--response-delay",
        str(delay),
    ]
    if reject_connections:
        command.append("--reject-connections")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.stdout is None:
        raise ProbeError("self-test server stdout was unavailable")
    line = process.stdout.readline()
    try:
        startup = json.loads(line)
    except json.JSONDecodeError as exc:
        terminate_child(process)
        raise ProbeError(f"self-test server did not emit startup JSON: {line!r}") from exc
    return process, startup


def read_child_output(process: subprocess.Popen[str]) -> tuple[str, str]:
    stdout, stderr = process.communicate(timeout=4.0)
    return stdout, stderr


def run_probe_subprocess(host: str, port: int, timeout: float, payload_bytes: int) -> tuple[int, str, str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "probe",
        "--host",
        host,
        "--port",
        str(port),
        "--timeout",
        str(timeout),
        "--payload-bytes",
        str(payload_bytes),
        "--samples",
        "1",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=4.0, check=False)
    return completed.returncode, completed.stdout, completed.stderr


def self_test(_args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "command": "self-test",
        "device": device_info(),
        "memory": memory_info(),
        "scenarios": [],
        "scope": {
            "bind_host": "127.0.0.1",
            "network": "local loopback only",
            "macos": (
                "native host is macOS"
                if platform.system() == "Darwin"
                else f"not exercised on this {platform.system()} host"
            ),
        },
    }

    normal_process, normal_startup = start_test_server(0.0)
    normal_port = normal_startup["bind"]["port"]
    normal_rc, normal_stdout, normal_stderr = run_probe_subprocess("127.0.0.1", normal_port, 1.0, 4096)
    try:
        normal_server_stdout, normal_server_stderr = read_child_output(normal_process)
        normal_server_rc = normal_process.returncode
    except subprocess.TimeoutExpired:
        normal_server_rc, normal_cleanup = terminate_child(normal_process)
        normal_server_stdout, normal_server_stderr = read_child_output(normal_process)
    else:
        normal_cleanup = normal_process.poll() is not None
    normal_json: dict[str, Any] | None = None
    try:
        normal_json = json.loads(normal_stdout)
    except json.JSONDecodeError:
        pass
    normal_ok = (
        normal_rc == 0
        and normal_server_rc == 0
        and isinstance(normal_json, dict)
        and normal_json.get("cleanup", {}).get("connections_closed") == 1
    )
    report["scenarios"].append(
        {
            "name": "normal_loopback",
            "expected": "probe rc=0; bounded server exits rc=0",
            "pass": normal_ok,
            "probe_rc": normal_rc,
            "probe_stdout": normal_stdout,
            "probe_stderr": normal_stderr,
            "server_startup": normal_startup,
            "server_stdout_after_startup": normal_server_stdout,
            "server_stderr": normal_server_stderr,
            "server_rc": normal_server_rc,
            "cleanup_verified": normal_cleanup,
        }
    )

    reject_process, reject_startup = start_test_server(0.0, reject_connections=True)
    reject_port = reject_startup["bind"]["port"]
    reject_rc, reject_stdout, reject_stderr = run_probe_subprocess("127.0.0.1", reject_port, 0.5, 1024)
    try:
        reject_server_stdout, reject_server_stderr = read_child_output(reject_process)
        reject_server_rc = reject_process.returncode
        reject_cleanup = reject_process.poll() is not None
    except subprocess.TimeoutExpired:
        reject_server_rc, reject_cleanup = terminate_child(reject_process)
        reject_server_stdout, reject_server_stderr = read_child_output(reject_process)
    reject_json: dict[str, Any] | None = None
    try:
        reject_json = json.loads(reject_stdout)
    except json.JSONDecodeError:
        pass
    reject_cleanup_count = (reject_json or {}).get("cleanup", {}).get("connections_closed")
    reject_ok = (
        reject_rc != 0
        and isinstance(reject_json, dict)
        and reject_json.get("status") == "error"
        and reject_cleanup_count == 1
        and reject_cleanup
        and reject_server_rc == 0
    )
    report["scenarios"].append(
        {
            "name": "peer_rejected",
            "expected": "explicit peer closes without accepting an LCNR frame; probe emits JSON error",
            "pass": reject_ok,
            "probe_rc": reject_rc,
            "probe_stdout": reject_stdout,
            "probe_stderr": reject_stderr,
            "server_startup": reject_startup,
            "server_stdout_after_startup": reject_server_stdout,
            "server_stderr": reject_server_stderr,
            "server_rc": reject_server_rc,
            "cleanup_verified": reject_cleanup,
        }
    )

    refused_port = find_unused_port()
    refused_rc, refused_stdout, refused_stderr = run_probe_subprocess("127.0.0.1", refused_port, 0.5, 1024)
    refused_json: dict[str, Any] | None = None
    try:
        refused_json = json.loads(refused_stdout)
    except json.JSONDecodeError:
        pass
    refused_cleanup_count = (refused_json or {}).get("cleanup", {}).get("connections_closed")
    refused_ok = (
        refused_rc != 0
        and isinstance(refused_json, dict)
        and refused_json.get("status") == "error"
        and refused_cleanup_count == 0
    )
    report["scenarios"].append(
        {
            "name": "unavailable_endpoint",
            "expected": "probe emits JSON error and nonzero rc without starting a listener; OS may report refusal or timeout",
            "pass": refused_ok,
            "port": refused_port,
            "probe_rc": refused_rc,
            "probe_stdout": refused_stdout,
            "probe_stderr": refused_stderr,
        }
    )

    timeout_process, timeout_startup = start_test_server(0.5)
    timeout_port = timeout_startup["bind"]["port"]
    timeout_rc, timeout_stdout, timeout_stderr = run_probe_subprocess("127.0.0.1", timeout_port, 0.05, 1024)
    try:
        timeout_server_stdout, timeout_server_stderr = read_child_output(timeout_process)
        timeout_server_rc = timeout_process.returncode
        timeout_cleanup = timeout_process.poll() is not None
    except subprocess.TimeoutExpired:
        timeout_server_rc, timeout_cleanup = terminate_child(timeout_process)
        timeout_server_stdout, timeout_server_stderr = read_child_output(timeout_process)
    timeout_json: dict[str, Any] | None = None
    try:
        timeout_json = json.loads(timeout_stdout)
    except json.JSONDecodeError:
        pass
    timeout_cleanup_count = (timeout_json or {}).get("cleanup", {}).get("connections_closed")
    timeout_ok = (
        timeout_rc != 0
        and isinstance(timeout_json, dict)
        and timeout_json.get("status") == "error"
        and timeout_cleanup_count == 1
        and timeout_cleanup
    )
    report["scenarios"].append(
        {
            "name": "response_timeout_and_cleanup",
            "expected": "probe emits JSON error; bounded server exits and listener is closed",
            "pass": timeout_ok,
            "probe_rc": timeout_rc,
            "probe_stdout": timeout_stdout,
            "probe_stderr": timeout_stderr,
            "server_startup": timeout_startup,
            "server_stdout_after_startup": timeout_server_stdout,
            "server_stderr": timeout_server_stderr,
            "server_rc": timeout_server_rc,
            "cleanup_verified": timeout_cleanup,
        }
    )

    if not all(scenario["pass"] for scenario in report["scenarios"]):
        report["status"] = "failed"
    emit_json(report)
    return 0 if report["status"] == "ok" else 1


def add_common_network_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", help="explicit peer/bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=parse_port, default=9100, help="TCP port (0 only for an ephemeral server port)")
    parser.add_argument("--timeout", type=parse_timeout, default=DEFAULT_TIMEOUT_SECONDS, help="socket timeout in seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="listen and echo LCNR/1 frames")
    add_common_network_args(serve_parser)
    serve_parser.add_argument("--max-connections", type=parse_nonnegative_int, default=0, help="stop after N connections; 0 means run until interrupted")
    serve_parser.add_argument("--response-delay", type=parse_nonnegative_timeout, default=0.0, help="test-only delay before each echo response")
    serve_parser.add_argument("--reject-connections", action="store_true", help="test-only: close accepted connections before reading a frame")
    serve_parser.add_argument("--allow-non-loopback", action="store_true", help="explicitly permit binding a named LAN address")
    serve_parser.set_defaults(handler=serve)

    probe_parser = subparsers.add_parser("probe", help="measure an explicitly named TCP peer")
    add_common_network_args(probe_parser)
    probe_parser.add_argument("--payload-bytes", type=parse_positive_int, default=DEFAULT_PAYLOAD_BYTES)
    probe_parser.add_argument("--samples", type=parse_positive_int, default=3)
    probe_parser.set_defaults(handler=probe)

    test_parser = subparsers.add_parser("self-test", help="run bounded loopback, refused, timeout and cleanup checks")
    test_parser.set_defaults(handler=self_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (ProbeError, OSError, ValueError, TimeoutError) as exc:
        emit_json(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "error",
                "command": getattr(args, "command", None),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
