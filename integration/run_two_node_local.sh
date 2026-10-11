#!/usr/bin/env bash
# LC-11 集成启动/编排/清理脚本（一条命令跑通本机双节点分层推理）。
#
#   integration/run_two_node_local.sh
#
# 四步：生成 tiny 资产 → 启动 Controller + 两个 Node Agent（真实 inference-engine
# StageServer 执行器）→ 建任务并把 prompt 送进首段节点、与单机结果对照 → 只清理本脚本
# 自己启动的 PID/端口，并写出证据汇总 JSON。
#
# 前置：仓库内四个模块 + 现场保存的 venv（不重建环境）。venv 位置用 LC11_VENV_ROOT 指定
# （默认取仓库上一级的 .venv-*）。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_ROOT="${LC11_VENV_ROOT:-$ROOT/..}"
CONTROLLER_PY="${CONTROLLER_PY:-$VENV_ROOT/.venv-controller-server/bin/python}"
NODE_PY="${NODE_PY:-$VENV_ROOT/.venv-node-agent/bin/python}"
ENGINE_PY="${ENGINE_PY:-$VENV_ROOT/.venv-inference-engine/bin/python}"
RUN_DIR="${LC11_RUN_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/lastcore-lc11-XXXXXX")}"
PROMPT="${LC11_PROMPT:-1 2 3 4}"
MODEL_LAYERS="${LC11_LAYERS:-4}"
LOG_DIR="$RUN_DIR/logs"
PID_FILE="$RUN_DIR/pids"
SUMMARY="$RUN_DIR/summary.json"
PORTS=""
ALL_OK=false

mkdir -p "$LOG_DIR" "$RUN_DIR/state-a" "$RUN_DIR/state-b"
: > "$PID_FILE"

log() { printf '[lc11] %s\n' "$*" >&2; }

pick_port() {
  "$ENGINE_PY" - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
}

port_open() { # port_open <port> -> 0 when something accepts a TCP connection
  "$ENGINE_PY" - "$1" <<'PY'
import socket, sys
sock = socket.socket()
sock.settimeout(0.5)
try:
    sock.connect(("127.0.0.1", int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

stop_all() {
  [ -s "$PID_FILE" ] || return 0
  while read -r name pid; do
    kill -TERM "$pid" 2>/dev/null || true
  done < "$PID_FILE"
  sleep 1
  while read -r name pid; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
      log "SIGKILL $name pid=$pid"
    fi
  done < "$PID_FILE"
}

write_summary() { # write_summary <exit_code> ; only ever inspects this run's own PIDs/ports
  "$ENGINE_PY" - "$PID_FILE" "$PORTS" "$RUN_DIR" "$SUMMARY" "$ALL_OK" "$1" <<'PY'
import json, os, socket, sys, time
from pathlib import Path

pid_file, ports, run_dir, summary_path, all_ok, code = sys.argv[1:7]
entries = []
for line in Path(pid_file).read_text().splitlines():
    if line.strip():
        name, pid = line.split()
        entries.append([name, int(pid)])

def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

def port_free(port):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()

port_list = ports.split()
busy = port_list
for _ in range(10):
    busy = [port for port in port_list if not port_free(port)]
    if not busy:
        break
    time.sleep(0.5)

summary = {
    "run_dir": run_dir,
    "ports": port_list,
    "pids": [{"name": name, "pid": pid} for name, pid in entries],
    "still_alive": [{"name": name, "pid": pid} for name, pid in entries if alive(pid)],
    "ports_still_bound": busy,
    "all_commands_exit_ok": all_ok == "true",
    "exit_code": int(code),
}
Path(summary_path).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
print(json.dumps(summary, indent=2, ensure_ascii=False))
PY
}

cleanup() {
  local code=$?
  log "tearing down (own pids only)"
  stop_all
  write_summary "$code" >&2
  exit "$code"
}

trap cleanup EXIT INT TERM

ADMIN_TOKEN="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)"
REGISTRATION_TOKEN="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)"
CPORT="$(pick_port)"; A_PORT="$(pick_port)"; B_PORT="$(pick_port)"
PORTS="$CPORT $A_PORT $B_PORT"
BASE_URL="http://127.0.0.1:$CPORT"
log "run_dir=$RUN_DIR ports: controller=$CPORT node-a=$A_PORT node-b=$B_PORT"

# 1) tiny 资产（纯本地）
PYTHONPATH="$ROOT/inference-engine:$ROOT/communication" "$ENGINE_PY" \
  -m inference_engine.node generate --write-config "$RUN_DIR/tiny.json" \
  --out "$RUN_DIR/tiny.npz" --seed 7 > "$LOG_DIR/assets.log" 2>&1 \
  || { log "asset generation failed"; exit 1; }

# 2) Controller（真实 uvicorn 进程；--app-dir 避免子 shell 记错 PID）
env CONTROLLER_ADMIN_TOKEN="$ADMIN_TOKEN" \
    CONTROLLER_REGISTRATION_TOKEN="$REGISTRATION_TOKEN" \
    CONTROLLER_DATABASE_PATH="$RUN_DIR/controller.sqlite3" \
    "$CONTROLLER_PY" -m uvicorn app.main:create_app --factory \
    --app-dir "$ROOT/controller-server" --host 127.0.0.1 --port "$CPORT" \
    > "$LOG_DIR/controller.log" 2>&1 &
echo "controller $!" >> "$PID_FILE"

for _ in $(seq 1 60); do
  if curl -fsS "$BASE_URL/healthz" > /dev/null 2>&1; then break; fi
  sleep 0.5
done
curl -fsS "$BASE_URL/healthz" > /dev/null || { log "controller not healthy"; exit 1; }
log "controller healthy on $CPORT"

# 3) 两个 Node Agent，执行器为真实 StageServer
for spec in "node-a $A_PORT state-a" "node-b $B_PORT state-b"; do
  set -- $spec
  name="$1"; port="$2"; state="$3"
  env NODE_AGENT_CONTROLLER_URL="$BASE_URL" \
      NODE_AGENT_REGISTRATION_TOKEN="$REGISTRATION_TOKEN" \
      NODE_AGENT_ENDPOINT="http://127.0.0.1:$port" \
      NODE_AGENT_NAME="$name" \
      NODE_AGENT_STATE_DIR="$RUN_DIR/$state" \
      NODE_AGENT_AVAILABLE_MEMORY_MB=4096 \
      NODE_AGENT_COMPUTE_WEIGHT=1 \
      NODE_AGENT_POLL_INTERVAL=0.5 \
      NODE_AGENT_HEARTBEAT_INTERVAL=5 \
      NODE_AGENT_EXECUTOR=stage \
      NODE_AGENT_MODEL_CONFIG="$RUN_DIR/tiny.json" \
      NODE_AGENT_WEIGHTS="$RUN_DIR/tiny.npz" \
      NODE_AGENT_COMPLETE_AFTER=1 \
      PYTHONPATH="$ROOT/node-agent:$ROOT/inference-engine:$ROOT/communication" \
      "$NODE_PY" -m lastcore_node_agent > "$LOG_DIR/$name.log" 2>&1 &
  echo "$name $!" >> "$PID_FILE"
done
log "node agents started"

count=0
for _ in $(seq 1 60); do
  nodes="$(curl -fsS -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE_URL/v1/nodes" 2>/dev/null || echo '')"
  count="$(printf '%s' "$nodes" | "$ENGINE_PY" -c 'import json,sys
try:
    print(len(json.load(sys.stdin).get("nodes", [])))
except Exception:
    print(0)')"
  [ "$count" = "2" ] && break
  sleep 0.5
done
[ "$count" = "2" ] || { log "nodes did not register (count=$count)"; exit 1; }
log "two nodes registered"

# 4) 建任务 → 等首段 StageServer 就绪 → 送 prompt
task="$(curl -fsS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"model_id\":\"lastcore/tiny-cpu\",\"num_layers\":$MODEL_LAYERS,\"layer_memory_mb\":64,\"max_nodes\":2,\"timeout_seconds\":120}" \
  "$BASE_URL/v1/tasks")" || { log "task creation failed"; exit 1; }
printf '%s\n' "$task" > "$RUN_DIR/task.json"
first_endpoint="$(printf '%s' "$task" | "$ENGINE_PY" -c 'import json,sys
task = json.load(sys.stdin)
first = [a for a in task["assignments"] if a["layer_start"] == 0]
print(first[0]["endpoint"] if first else "")')"
[ -n "$first_endpoint" ] || { log "no first-stage assignment"; exit 1; }
first_port="$("$ENGINE_PY" -c 'import sys
from urllib.parse import urlparse
value = sys.argv[1] if "://" in sys.argv[1] else "//" + sys.argv[1]
parsed = urlparse(value)
print(parsed.port or "")' "$first_endpoint")"
[ -n "$first_port" ] || { log "cannot parse first-stage endpoint: $first_endpoint"; exit 1; }
for _ in $(seq 1 120); do
  port_open "$first_port" && break
  sleep 0.25
done
port_open "$first_port" || { log "first stage listener never opened on $first_port"; exit 1; }
log "first stage listener ready: $first_endpoint"

PYTHONPATH="$ROOT/node-agent:$ROOT/inference-engine:$ROOT/communication" "$ENGINE_PY" \
  integration/pipeline_client.py --endpoint "$first_endpoint" \
  --config "$RUN_DIR/tiny.json" --weights "$RUN_DIR/tiny.npz" --prompt "$PROMPT" \
  --out "$RUN_DIR/result.json" > "$LOG_DIR/client.log" 2>&1 || true
cat "$LOG_DIR/client.log" >&2
grep -q '"bit_identical_to_single_machine": true' "$RUN_DIR/result.json" || { log "client comparison failed"; exit 1; }
ALL_OK=true

{
  echo "peak_rss_kb:"
  while read -r name pid; do
    kill -0 "$pid" 2>/dev/null && printf '  %s: %s\n' "$name" "$(ps -o rss= -p "$pid" | tr -d ' ')"
  done < "$PID_FILE"
} > "$RUN_DIR/rss.txt"
cat "$RUN_DIR/rss.txt" >&2

log "done: artifacts in $RUN_DIR"
exit 0
