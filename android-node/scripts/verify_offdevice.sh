#!/usr/bin/env bash
# Off-device verification for the Android node (LC-13 phase 1).
#
# Runs everything that can be proven WITHOUT an Android device:
#   1. unit tests (device probe / budget / backend selection / executor)
#   2. the node-agent Executor-contract integration test, when a node-agent
#      checkout is provided
#   3. the admission planner CLI, asserting the *structured* decision and the
#      process exit code for every case (no `|| true`: a wrong answer or a
#      crashed command fails this script)
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
"$PYTHON" -m pytest

echo "== 2) node-agent contract =="
if [ -n "${LASTCORE_NODE_AGENT_PATH:-}" ]; then
  LASTCORE_NODE_AGENT_PATH="$LASTCORE_NODE_AGENT_PATH" "$PYTHON" -m pytest tests/test_node_agent_contract.py
else
  echo "SKIPPED: set LASTCORE_NODE_AGENT_PATH=<node-agent checkout at ba52895> to run it"
fi

echo "== 3) admission planner: structured decision + exit code =="

# expect_plan <expected_decision> <expected_exit_code> [args...]
expect_plan() {
  local expected_decision="$1" expected_rc="$2"
  shift 2
  local output rc actual
  set +e
  output="$("$PYTHON" -m lastcore_android_node plan --json "$@" 2>&1)"
  rc=$?
  set -e
  actual="$("$PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["decision"])' "$output" 2>/dev/null || echo "unparsable")"
  if [ "$actual" != "$expected_decision" ] || [ "$rc" != "$expected_rc" ]; then
    echo "FAIL: expected decision=$expected_decision rc=$expected_rc, got decision=$actual rc=$rc" >&2
    echo "--- output ---" >&2
    echo "$output" >&2
    return 1
  fi
  echo "ok: decision=$actual rc=$rc  ($* )"
}

export LASTCORE_ANDROID_MACHINE=arm64 LASTCORE_ANDROID_PLATFORM=Android
export LASTCORE_ANDROID_IS_ANDROID=true LASTCORE_ANDROID_THERMAL=nominal
export LASTCORE_ANDROID_TOTAL_MEMORY_MB=6144 LASTCORE_ANDROID_AVAILABLE_MEMORY_MB=2560

# Qwen2.5-0.5B-class shards (24 layers, d_model=896, vocab=151936, fp16).
expect_plan accept 0 --layers 0:12 --layers-total 24 \
  --d-model 896 --heads 14 --vocab 151936 --weights-bytes 616000000 --tokens 2048
expect_plan accept 0 --layers 12:24 --layers-total 24 \
  --d-model 896 --heads 14 --vocab 151936 --weights-bytes 344000000 --tokens 2048

LASTCORE_ANDROID_AVAILABLE_MEMORY_MB=700 expect_plan reject_memory 1 \
  --layers 0:12 --layers-total 24 --d-model 896 --heads 14 --vocab 151936 \
  --weights-bytes 616000000 --tokens 2048

LASTCORE_ANDROID_AVAILABLE_MEMORY_MB=2560 LASTCORE_ANDROID_THERMAL=serious expect_plan reject_thermal 1 \
  --layers 0:12 --layers-total 24 --d-model 896 --heads 14 --vocab 151936 \
  --weights-bytes 616000000 --tokens 2048

# "Not charging" must not buy the low-battery exemption (review finding 3).
LASTCORE_ANDROID_THERMAL=nominal LASTCORE_ANDROID_BATTERY_PERCENT=10 \
  LASTCORE_ANDROID_CHARGING=false expect_plan reject_battery 1 \
  --layers 0:12 --layers-total 24 --d-model 896 --heads 14 --vocab 151936 \
  --weights-bytes 616000000 --tokens 2048

LASTCORE_ANDROID_BATTERY_PERCENT=10 LASTCORE_ANDROID_CHARGING=true expect_plan accept 0 \
  --layers 0:12 --layers-total 24 --d-model 896 --heads 14 --vocab 151936 \
  --weights-bytes 616000000 --tokens 2048

echo "== 4) memory ceiling (1 GiB) =="
scripts/check_memory_ceiling.sh suite

echo "== done =="
