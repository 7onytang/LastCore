#!/bin/bash
# Integrated two-node E2E: real controller + two node-agents running real
# inference-engine stages (tiny model), plus lifecycle (cancel/timeout/restart).
set -u
VENV=/tmp/multica-task-2291416987/opencode/venv/bin
export PATH="$VENV:$PATH"
REPO=/Users/lmg/multica_workspaces_desktop-api.multica.ai/lastcore-a50f1ac76564/lc-1-b33d0fac979a/workdir/LastCore
WORK=/tmp/multica-task-2291416987/opencode/e2e
rm -rf "$WORK"; mkdir -p "$WORK"

cd "$REPO/controller-server"
export CONTROLLER_ADMIN_TOKEN="e2e-admin-token-0123456789abcdef0123456789abcdef"
export CONTROLLER_REGISTRATION_TOKEN="e2e-registration-token-0123456789abcdef0123"
export CONTROLLER_DATABASE_PATH="$WORK/controller.sqlite3"
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8800 >"$WORK/controller.log" 2>&1 &
CTRL_PID=$!

start_agents() {  # $1 = log suffix
  PYTHONPATH="$REPO/inference-engine:$REPO/communication" NODE_AGENT_REGISTRATION_TOKEN="$CONTROLLER_REGISTRATION_TOKEN" \
    python -m lastcore_node_agent --controller-url http://127.0.0.1:8800 \
    --endpoint http://127.0.0.1:9101 --state-dir "$WORK/node-a" \
    --available-memory-mb 4096 --compute-weight 2.0 --name node-a \
    --heartbeat-interval 2 --executor stage --verbose >"$WORK/node-a-$1.log" 2>&1 &
  echo $! >"$WORK/a.pid"
  PYTHONPATH="$REPO/inference-engine:$REPO/communication" NODE_AGENT_REGISTRATION_TOKEN="$CONTROLLER_REGISTRATION_TOKEN" \
    python -m lastcore_node_agent --controller-url http://127.0.0.1:8800 \
    --endpoint http://127.0.0.1:9102 --state-dir "$WORK/node-b" \
    --available-memory-mb 2048 --compute-weight 1.0 --name node-b \
    --heartbeat-interval 2 --executor stage --verbose >"$WORK/node-b-$1.log" 2>&1 &
  echo $! >"$WORK/b.pid"
}
stop_agents() {
  kill "$(cat "$WORK/a.pid")" "$(cat "$WORK/b.pid")" 2>/dev/null
  wait "$(cat "$WORK/a.pid")" "$(cat "$WORK/b.pid")" 2>/dev/null
}
cleanup() { stop_agents; kill $CTRL_PID 2>/dev/null; wait $CTRL_PID 2>/dev/null; }
trap cleanup EXIT

for i in $(seq 1 40); do curl -sf http://127.0.0.1:8800/healthz >/dev/null && break; sleep 0.5; done
ADMIN="Authorization: Bearer $CONTROLLER_ADMIN_TOKEN"
BASE=http://127.0.0.1:8800
cd "$REPO/node-agent"

start_agents run1
wait_available() {
  for i in $(seq 1 60); do
    AV=$(curl -sf $BASE/v1/nodes -H "$ADMIN" | python3 -c "import json,sys; ns=json.load(sys.stdin)['nodes']; print(sum(1 for n in ns if n['status']=='available'))" 2>/dev/null || echo 0)
    [ "$AV" = "2" ] && return 0
    sleep 0.5
  done
  return 1
}
wait_available; echo "nodes available: $AV (expect 2)"

create_task() { curl -s -X POST $BASE/v1/tasks -H "$ADMIN" -H 'Content-Type: application/json' -d "$1" -w '\nHTTP %{http_code}'; }
wait_status() { # $1 task id, $2 expected, $3 seconds
  for i in $(seq 1 $3); do
    S=$(curl -sf $BASE/v1/tasks/$1 -H "$ADMIN" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "?")
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
wait_serving run1 && echo "both stages serving"
curl -sf $BASE/v1/tasks/$T1 -H "$ADMIN" | python3 -c "import json,sys; d=json.load(sys.stdin); print('plan:', [(a['node_id'][:8], a['layer_start'], a['layer_end']) for a in d['assignments']])"
python scripts/client_check.py 127.0.0.1 9101 "$WORK/node-a/tiny-model.npz" "$T1" >"$WORK/client1.json" 2>"$WORK/client1.err" || { echo CLIENT-FAILED; cat "$WORK/client1.err"; }
cat "$WORK/client1.json"
wait_status "$T1" completed 30; echo "task1 status: $S (expect completed)"
curl -sf $BASE/v1/tasks/$T1 -H "$ADMIN" | python3 -c "import json,sys; d=json.load(sys.stdin); print('assignments:', [(a['id'][:13], a['status']) for a in d['assignments']])"

echo "=== task 2: cancel while stages are serving ==="
BEFORE_A=$(grep -c "serving on" "$WORK/node-a-run1.log" || true)
BEFORE_B=$(grep -c "serving on" "$WORK/node-b-run1.log" || true)
RESP=$(create_task '{"model_id":"lastcore/tiny-cpu","num_layers":4,"layer_memory_mb":512,"timeout_seconds":120}')
T2=$(echo "$RESP" | head -1 | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
for i in $(seq 1 30); do
  NOW_A=$(grep -c "serving on" "$WORK/node-a-run1.log" || true)
  NOW_B=$(grep -c "serving on" "$WORK/node-b-run1.log" || true)
  [ "$NOW_A" -gt "$BEFORE_A" ] && [ "$NOW_B" -gt "$BEFORE_B" ] && break
  sleep 0.5
done
echo "both stages serving (fresh listeners for task2)"
curl -s -X POST $BASE/v1/tasks/$T2/cancel -H "$ADMIN" -o /dev/null -w "cancel: HTTP %{http_code}\n"
wait_status "$T2" cancelled 30; echo "task2 status: $S (expect cancelled)"
sleep 2
grep -c "stopped locally (task-terminal)" "$WORK/node-a-run1.log" "$WORK/node-b-run1.log"

echo "=== restart agents with same state dirs ==="
stop_agents
start_agents run2
wait_available; echo "nodes available after restart: $AV (expect 2, no duplicate registration)"

echo "=== task 3: deadline (10s) with no client request ==="
RESP=$(create_task '{"model_id":"lastcore/tiny-cpu","num_layers":4,"layer_memory_mb":512,"timeout_seconds":10}')
T3=$(echo "$RESP" | head -1 | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
wait_status "$T3" failed 40; echo "task3 status: $S (expect failed)"
sleep 3

echo "=== final node count (expect 2) ==="
curl -sf $BASE/v1/nodes -H "$ADMIN" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['nodes']))"

echo "=== agent log evidence ==="
grep -hE "registered|reusing persisted|started assignment|serving on|served request|completed|stopped locally|failed:" "$WORK"/node-a-run1.log "$WORK"/node-b-run1.log "$WORK"/node-a-run2.log "$WORK"/node-b-run2.log
echo "=== errors in agent logs (expect none) ==="
grep -hE "ERROR|Traceback" "$WORK"/node-a-run1.log "$WORK"/node-b-run1.log "$WORK"/node-a-run2.log "$WORK"/node-b-run2.log || echo "(none)"
