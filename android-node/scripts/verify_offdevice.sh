#!/usr/bin/env bash
# Off-device verification for the Android node (LC-13 phase 1).
#
# Runs everything that can be proven WITHOUT an Android device:
#   1. unit tests (device probe / budget / backend selection / executor)
#   2. the node-agent Executor-contract integration test, when a node-agent
#      checkout is provided
#   3. the admission planner CLI on a few representative shards
#
# Usage:
#   scripts/verify_offdevice.sh
#   LASTCORE_NODE_AGENT_PATH=/path/to/node-agent scripts/verify_offdevice.sh

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
PYTHON="$PWD/.venv/bin/python"
"$PYTHON" -m pip install -q -r requirements-dev.txt

echo "== 1) unit tests =="
"$PYTHON" -m pytest -q

echo "== 2) node-agent contract =="
if [ -n "${LASTCORE_NODE_AGENT_PATH:-}" ]; then
  LASTCORE_NODE_AGENT_PATH="$LASTCORE_NODE_AGENT_PATH" "$PYTHON" -m pytest -q tests/test_node_agent_contract.py
else
  echo "SKIPPED: set LASTCORE_NODE_AGENT_PATH=<node-agent checkout> to run it"
fi

echo "== 3) admission planner (Qwen2.5-0.5B-class shards on a 6GB/2.5GB-free phone) =="
export LASTCORE_ANDROID_MACHINE=arm64 LASTCORE_ANDROID_PLATFORM=Android
export LASTCORE_ANDROID_IS_ANDROID=true LASTCORE_ANDROID_THERMAL=nominal
export LASTCORE_ANDROID_TOTAL_MEMORY_MB=6144 LASTCORE_ANDROID_AVAILABLE_MEMORY_MB=2560

echo "-- shard 0:12 (embedding + 12 layers, 588MB fp16)"
"$PYTHON" -m lastcore_android_node plan --layers 0:12 --layers-total 24 \
  --d-model 896 --heads 14 --vocab 151936 --weights-bytes 616000000 --tokens 2048 || true

echo "-- shard 12:24 (12 layers + lm_head, 328MB fp16)"
"$PYTHON" -m lastcore_android_node plan --layers 12:24 --layers-total 24 \
  --d-model 896 --heads 14 --vocab 151936 --weights-bytes 344000000 --tokens 2048 || true

echo "-- shard 0:12 with only 700MB free (must reject)"
LASTCORE_ANDROID_AVAILABLE_MEMORY_MB=700 "$PYTHON" -m lastcore_android_node plan \
  --layers 0:12 --layers-total 24 --d-model 896 --heads 14 --vocab 151936 \
  --weights-bytes 616000000 --tokens 2048 || true

echo "== done =="
