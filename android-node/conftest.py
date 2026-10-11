import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# node-agent must be importable *before* any test module imports
# ``lastcore_android_node.executor``: that module binds its Executor /
# ExecutionCancelled base classes at import time.  In the real host process
# node-agent has already imported its own package before it loads this one via
# ``--executor lastcore_android_node.executor:executor_factory``; the env var
# below reproduces that ordering for the contract tests.
NODE_AGENT_PATH = os.environ.get("LASTCORE_NODE_AGENT_PATH")
if NODE_AGENT_PATH and NODE_AGENT_PATH not in sys.path:
    sys.path.insert(0, NODE_AGENT_PATH)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
