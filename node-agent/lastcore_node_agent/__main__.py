from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from .agent import CredentialRejected, NodeAgent
from .config import AgentConfig
from .controller import ControllerClient, ControllerError
from .credentials import StateError, acquire_state_lock


def _resolve_executor_factory(config: AgentConfig):
    if config.executor == "stage":
        from .inference_adapter import build_stage_executor
        return build_stage_executor(config)
    from .executor import load_executor
    return lambda: load_executor(config.executor)

LOG = logging.getLogger("lastcore.node_agent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lastcore-node-agent",
                                     description="LastCore node agent")
    parser.add_argument("--controller-url",
                        default=os.environ.get("NODE_AGENT_CONTROLLER_URL", ""))
    parser.add_argument("--endpoint",
                        default=os.environ.get("NODE_AGENT_ENDPOINT", ""))
    parser.add_argument("--name", default=os.environ.get("NODE_AGENT_NAME", ""))
    parser.add_argument("--platform-name", default=os.environ.get("NODE_AGENT_PLATFORM", ""))
    parser.add_argument("--available-memory-mb", type=int,
                        default=int(os.environ.get("NODE_AGENT_AVAILABLE_MEMORY_MB", "0") or 0))
    parser.add_argument("--compute-weight", type=float,
                        default=float(os.environ.get("NODE_AGENT_COMPUTE_WEIGHT", "1") or 1))
    parser.add_argument("--state-dir",
                        default=os.environ.get("NODE_AGENT_STATE_DIR",
                                               "~/.lastcore/node-agent"))
    parser.add_argument("--heartbeat-interval", type=float,
                        default=float(os.environ.get("NODE_AGENT_HEARTBEAT_INTERVAL", "10") or 10))
    parser.add_argument("--poll-interval", type=float,
                        default=float(os.environ.get("NODE_AGENT_POLL_INTERVAL", "1") or 1))
    parser.add_argument("--executor", default=os.environ.get("NODE_AGENT_EXECUTOR", "demo"))
    parser.add_argument("--model-config", default=os.environ.get("NODE_AGENT_MODEL_CONFIG", ""),
                        help="model config JSON for --executor stage (default: built-in tiny model)")
    parser.add_argument("--weights", default=os.environ.get("NODE_AGENT_WEIGHTS", ""),
                        help="checkpoint path for --executor stage (default: generated tiny model in state dir)")
    parser.add_argument("--complete-after", type=int,
                        default=int(os.environ.get("NODE_AGENT_COMPLETE_AFTER", "1") or 0),
                        help="report completed after N served requests (0 = serve until cancelled)")
    parser.add_argument("--no-accept", action="store_true",
                        help="start in drain mode (accepting_tasks=false)")
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--ui-status-file",help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not args.controller_url:
        print("controller URL is required (--controller-url or NODE_AGENT_CONTROLLER_URL)",
              file=sys.stderr)
        return 1
    if not args.endpoint:
        print("peer endpoint is required (--endpoint or NODE_AGENT_ENDPOINT)", file=sys.stderr)
        return 1
    if args.available_memory_mb <= 0:
        print("--available-memory-mb must be positive", file=sys.stderr)
        return 1
    config = AgentConfig(
        controller_url=args.controller_url,
        endpoint=args.endpoint,
        available_memory_mb=args.available_memory_mb,
        compute_weight=args.compute_weight,
        node_name=args.name,
        platform_name=args.platform_name,
        state_dir=Path(args.state_dir).expanduser(),
        heartbeat_interval=args.heartbeat_interval,
        poll_interval=args.poll_interval,
        accepting_tasks=not args.no_accept,
        executor=args.executor,
        model_config_path=args.model_config,
        weights_path=args.weights,
        complete_after=args.complete_after,
        registration_token=os.environ.get("NODE_AGENT_REGISTRATION_TOKEN", ""),
        request_timeout=args.request_timeout,
    )
    state_lock = acquire_state_lock(config.state_dir)
    if state_lock is None:
        print(f"state directory {config.state_dir} is in use by another agent process",
              file=sys.stderr)
        return 4
    try:
        executor_factory = _resolve_executor_factory(config)
    except (RuntimeError, StateError) as exc:
        print(f"executor {config.executor!r} cannot be loaded: {exc}", file=sys.stderr)
        return 4
    observer = None
    if args.ui_status_file:
        from .local_status import LocalStatusWriter
        observer = LocalStatusWriter(args.ui_status_file)
    agent = NodeAgent(config, ControllerClient(config.controller_url,
                                               timeout=config.request_timeout),
                      executor_factory=executor_factory,observer=observer)

    def handle_signal(signum, frame):
        agent.shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_signal)
    try:
        agent.run_forever()
    except CredentialRejected as exc:
        LOG.error("persisted credentials were rejected by the controller: %s", exc)
        return 2
    except ControllerError as exc:
        LOG.error("controller error: %s", exc)
        return 3
    except (RuntimeError, StateError) as exc:
        LOG.error("node agent cannot start: %s", exc)
        return 4
    finally:
        agent.client.close()
    stats = agent.stats
    print(f"node agent stopped: heartbeats_ok={stats.heartbeats_ok} "
          f"heartbeats_failed={stats.heartbeats_failed} polls={stats.polls} "
          f"started={len(stats.started)} completed={len(stats.completed)} "
          f"failed={len(stats.failed)} stopped={len(stats.stopped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
