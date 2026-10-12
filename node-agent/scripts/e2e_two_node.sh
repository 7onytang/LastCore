#!/bin/bash
# Integrated two-node E2E: real controller + two node-agents running real
# inference-engine stages (tiny model), plus lifecycle (cancel/timeout/restart).
#
# Portable by design: the repository root is derived from this script's own
# location and all scratch state goes to a private temp dir, so there are no
# machine-specific paths. Optional overrides:
#   E2E_VENV            virtualenv bin dir prepended to PATH
#   E2E_WORK_DIR        scratch/state dir (default: a fresh mktemp dir)
#   E2E_CONTROLLER_PORT / E2E_NODE_A_PORT / E2E_NODE_B_PORT  loopback ports
# Cleanup is bounded: only the exact PIDs this script started are signalled.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${E2E_WORK_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/lastcore-e2e.XXXXXX")}"
mkdir -p "$WORK"
if [ -n "${E2E_VENV:-}" ]; then
  [ -d "$E2E_VENV" ] || { echo "E2E_VENV is not a directory: $E2E_VENV" >&2; exit 2; }
  export PATH="$E2E_VENV:$PATH"
fi

CTRL_PORT="${E2E_CONTROLLER_PORT:-8800}"
NODE_A_PORT="${E2E_NODE_A_PORT:-9101}"
NODE_B_PORT="${E2E_NODE_B_PORT:-9102}"
CTRL_URL="http://127.0.0.1:$CTRL_PORT"

CTRL_PID=""
A_PID=""
B_PID=""
RUN_SUFFIX=""
AV=""

cd "$REPO/controller-server"
export CONTROLLER_ADMIN_TOKEN="e2e-admin-token-0123456789abcdef0123456789abcdef"
export CONTROLLER_REGISTRATION_TOKEN="e2e-registration-token-0123456789abcdef0123"
export CONTROLLER_DATABASE_PATH="$WORK/controller.sqlite3"
python3 -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port "$CTRL_PORT" >"$WORK/controller.log" 2>&1 &
CTRL_PID=$!

start_agents() {  # $1 = log suffix
  RUN_SUFFIX="$1"
  PYTHONPATH="$REPO/inference-engine:$REPO/communication" NODE_AGENT_REGISTRATION_TOKEN="$CONTROLLER_REGISTRATION_TOKEN" \
    python3 -m lastcore_node_agent --controller-url "$CTRL_URL" \
    --endpoint "http://127.0.0.1:$NODE_A_PORT" --state-dir "$WORK/node-a" \
    --available-memory-mb 4096 --compute-weight 2.0 --name node-a \
    --heartbeat-interval 2 --executor stage --verbose >"$WORK/node-a-$1.log" 2>&1 &
  A_PID=$!
  PYTHONPATH="$REPO/inference-engine:$REPO/communication" NODE_AGENT_REGISTRATION_TOKEN="$CONTROLLER_REGISTRATION_TOKEN" \
    python3 -m lastcore_node_agent --controller-url "$CTRL_URL" \
    --endpoint "http://127.0.0.1:$NODE_B_PORT" --state-dir "$WORK/node-b" \
    --available-memory-mb 2048 --compute-weight 1.0 --name node-b \
    --heartbeat-interval 2 --executor stage --verbose >"$WORK/node-b-$1.log" 2>&1 &
  B_PID=$!
}

stop_one() {  # $1 = pid: signal only the exact process this script started
  local pid="$1"
  [ -n "$pid" ] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

stop_agents() {
  stop_one "${A_PID:-}"; A_PID=""
  stop_one "${B_PID:-}"; B_PID=""
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  stop_agents
  stop_one "${CTRL_PID:-}"; CTRL_PID=""
  exit "$rc"
}
trap cleanup EXIT INT TERM

for i in $(seq 1 40); do
  curl -sf "$CTRL_URL/healthz" >/dev/null && break
  sleep 0.5
done
curl -sf "$CTRL_URL/healthz" >/dev/null || { echo "controller did not become healthy; see $WORK/controller.log" >&2; exit 1; }
ADMIN="Authorization: Bearer $CONTROLLER_ADMIN_TOKEN"
BASE="$CTRL_URL"
cd "$REPO/node-agent"

start_agents run1
check_agents_alive() {  # fail fast if a started agent process already exited
  local name pid log
  for name in a b; do
    if [ "$name" = a ]; then pid="${A_PID:-}"; else pid="${B_PID:-}"; fi
    [ -n "$pid" ] || continue
    kill -0 "$pid" 2>/dev/null && continue
    echo "ERROR: node-$name process (pid $pid) exited before becoming available" >&2
    log="$WORK/node-$name-$RUN_SUFFIX.log"
    if [ -f "$log" ]; then tail -n 20 "$log" >&2; fi
    return 1
  done
  return 0
}
wait_available() {
  for i in $(seq 1 60); do
    check_agents_alive || return 2
    AV=$(curl -sf "$BASE/v1/nodes" -H "$ADMIN" | python3 -c "import json,sys; ns=json.load(sys.stdin)['nodes']; print(sum(1 for n in ns if n['status']=='available'))" 2>/dev/null || echo 0)
    [ "$AV" = "2" ] && return 0
    sleep 0.5
  done
  echo "ERROR: expected 2 available nodes, got $AV" >&2
  return 1
}
if ! wait_available; then exit 1; fi
echo "nodes available: $AV (expect 2)"

create_task() { curl -s -X POST "$BASE/v1/tasks" -H "$ADMIN" -H 'Content-Type: application/json' -d "$1" -w '\nHTTP %{http_code}'; }
wait_status() { # $1 task id, $2 expected, $3 seconds
  for i in $(seq 1 "$3"); do
    S=$(curl -sf "$BASE/v1/tasks/$1" -H "$ADMIN" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "?")
    [ "$S" = "$2" ] && return 0
    sleep 1
  done
  return 1
}
wait_serving() { # both agents bound their stage listeners
  for i in $(seq 1 40); do
    grep -q "serving on" "$WORK/node-a-$1.log" 2>/dev/null && grep -q "serving on" "$WORK/node-b-$1.log" 2>/dev/null && return 0
    sleep 0.5
  done
  return 1
}

echo "=== task 1: real two-node tiny-model inference ==="
RESP=$(create_task '{"model_id":"lastcore/tiny-cpu","num_layers":4,"layer_memory_mb":512,"timeout_seconds":120}')
T1=$(echo "$RESP" | head -1 | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "task1=$T1"
if ! wait_serving run1; then echo "ERROR: stages did not report serving" >&2; exit 1; fi
echo "both stages serving"
curl -sf "$BASE/v1/tasks/$T1" -H "$ADMIN" | python3 -c "import json,sys; d=json.load(sys.stdin); print('plan:', [(a['node_id'][:8], a['layer_start'], a['layer_end']) for a in d['assignments']])"
if ! python3 scripts/client_check.py 127.0.0.1 "$NODE_A_PORT" "$WORK/node-a/tiny-model.npz" "$T1" >"$WORK/client1.json" 2>"$WORK/client1.err"; then
  echo "ERROR: client_check.py failed for task1=$T1" >&2
  cat "$WORK/client1.err" >&2
  exit 1
fi
cat "$WORK/client1.json"
if ! wait_status "$T1" completed 30; then echo "ERROR: task1 status $S (expect completed)" >&2; exit 1; fi
echo "task1 status: $S (expect completed)"
curl -sf "$BASE/v1/tasks/$T1" -H "$ADMIN" | python3 -c "import json,sys; d=json.load(sys.stdin); print('assignments:', [(a['id'][:13], a['status']) for a in d['assignments']])"

echo "=== task 2: cancel while stages are serving ==="
BEFORE_A=$(grep -c "serving on" "$WORK/node-a-run1.log" || true)
BEFORE_B=$(grep -c "serving on" "$WORK/node-b-run1.log" || true)
RESP=$(create_task '{"model_id":"lastcore/tiny-cpu","num_layers":4,"layer_memory_mb":512,"timeout_seconds":120}')
T2=$(echo "$RESP" | head -1 | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
FRESH=0
for i in $(seq 1 30); do
  NOW_A=$(grep -c "serving on" "$WORK/node-a-run1.log" || true)
  NOW_B=$(grep -c "serving on" "$WORK/node-b-run1.log" || true)
  if [ "$NOW_A" -gt "$BEFORE_A" ] && [ "$NOW_B" -gt "$BEFORE_B" ]; then FRESH=1; break; fi
  sleep 0.5
done
if [ "$FRESH" != 1 ]; then echo "ERROR: task2 stages did not report fresh listeners" >&2; exit 1; fi
echo "both stages serving (fresh listeners for task2)"
curl -s -X POST "$BASE/v1/tasks/$T2/cancel" -H "$ADMIN" -o /dev/null -w "cancel: HTTP %{http_code}\n"
if ! wait_status "$T2" cancelled 30; then echo "ERROR: task2 status $S (expect cancelled)" >&2; exit 1; fi
echo "task2 status: $S (expect cancelled)"
sleep 2
grep -c "stopped locally (task-terminal)" "$WORK/node-a-run1.log" "$WORK/node-b-run1.log" || true

echo "=== restart agents with same state dirs ==="
stop_agents
start_agents run2
if ! wait_available; then exit 1; fi
echo "nodes available after restart: $AV (expect 2, no duplicate registration)"

echo "=== task 3: deadline (10s) with no client request ==="
RESP=$(create_task '{"model_id":"lastcore/tiny-cpu","num_layers":4,"layer_memory_mb":512,"timeout_seconds":10}')
T3=$(echo "$RESP" | head -1 | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
if ! wait_status "$T3" failed 40; then echo "ERROR: task3 status $S (expect failed)" >&2; exit 1; fi
echo "task3 status: $S (expect failed)"
sleep 3

echo "=== final node count (expect 2) ==="
curl -sf "$BASE/v1/nodes" -H "$ADMIN" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['nodes']))"

echo "=== agent log evidence ==="
grep -hE "registered|reusing persisted|started assignment|serving on|served request|completed|stopped locally|failed:" "$WORK"/node-a-run1.log "$WORK"/node-b-run1.log "$WORK"/node-a-run2.log "$WORK"/node-b-run2.log || true
echo "=== errors in agent logs (expect none) ==="
grep -hE "ERROR|Traceback" "$WORK"/node-a-run1.log "$WORK"/node-b-run1.log "$WORK"/node-a-run2.log "$WORK"/node-b-run2.log || echo "(none)"
