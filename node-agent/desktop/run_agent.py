#!/usr/bin/env python3
"""Load a private node-agent environment file and start the agent."""
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

from env_file import ALLOWED_KEYS, read_env_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version",
                        version="LastCore desktop 0.1.0 (tiny CPU stage; explicit demo mode)")
    parser.add_argument("--env-file", required=True,
                        help="private KEY=VALUE configuration file; values are never shell-evaluated")
    parser.add_argument("--ui-status-file",help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        values = read_env_file(args.env_file)
        mode = values.get("NODE_AGENT_EXECUTOR")
        if mode not in {"stage", "demo"}:
            raise ValueError("set NODE_AGENT_EXECUTOR=stage or demo explicitly in the private configuration")
        # A private configuration is authoritative; inherited settings must not
        # silently select another executor, checkpoint or credential.
        for key in ALLOWED_KEYS:
            os.environ.pop(key, None)
        os.environ.update(values)
    except ValueError as exc:
        parser.error(str(exc))

    node_agent_dir = str(Path(__file__).resolve().parent.parent)
    if node_agent_dir not in sys.path:
        sys.path.insert(0, node_agent_dir)
    print("LastCore desktop: tiny CPU stage inference" if mode == "stage" else
          "LastCore desktop: demo simulation (no inference)", file=sys.stderr)
    # Source UI supports stage only with the sibling modules and NumPy installed.
    for sibling in ("inference-engine", "communication"):
        sibling_path = Path(__file__).resolve().parents[2] / sibling
        if sibling_path.is_dir(): sys.path.insert(0,str(sibling_path))
    sys.argv = ["lastcore-node-agent"]
    if args.ui_status_file: sys.argv.extend(["--ui-status-file",args.ui_status_file])
    try:
        runpy.run_module("lastcore_node_agent", run_name="__main__")
    except ValueError:
        print("invalid node configuration", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
