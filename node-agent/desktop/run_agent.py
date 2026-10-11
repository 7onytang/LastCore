#!/usr/bin/env python3
"""Load a private node-agent environment file and start the agent."""
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

from env_file import read_env_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True,
                        help="private KEY=VALUE configuration file; values are never shell-evaluated")
    args = parser.parse_args(argv)
    try:
        os.environ.update(read_env_file(args.env_file))
    except ValueError as exc:
        parser.error(str(exc))

    node_agent_dir = str(Path(__file__).resolve().parent.parent)
    if node_agent_dir not in sys.path:
        sys.path.insert(0, node_agent_dir)
    sys.argv = ["lastcore-node-agent"]
    runpy.run_module("lastcore_node_agent", run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
