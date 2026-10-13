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
umask 077
export TZ=Asia/Shanghai

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
RSS_MAX="$RUN_DIR/rss_max.json"
RSS_STOP="$RUN_DIR/rss.stop"
RSS_SAMPLER_PID=""
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
  while read -r name pid; do
    wait "$pid" 2>/dev/null || true
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

remaining = [{"name": name, "pid": pid} for name, pid in entries if alive(pid)]
cleanup_ok = not remaining and not busy
summary = {
    "run_dir": run_dir,
    "ports": port_list,
    "pids": [{"name": name, "pid": pid} for name, pid in entries],
    "still_alive": remaining,
    "cleanup_ok": cleanup_ok,
    "ports_still_bound": busy,
    "all_commands_exit_ok": all_ok == "true",
    "exit_code": int(code),
}
Path(summary_path).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
print(json.dumps(summary, indent=2, ensure_ascii=False))
raise SystemExit(0 if cleanup_ok else 1)
PY
}

write_rss() { # 真实峰值 RSS：持续采样 + 终止前终态采样，保留已退出 PID 的最后观测值
  stop_rss_sampler
  "$ENGINE_PY" - "$PID_FILE" "$RSS_MAX" "$RUN_DIR/rss.txt" <<'PY'
import json, os, subprocess, sys
from pathlib import Path

pid_file, maxima_path, out_path = sys.argv[1:4]
try:
    maxima = {str(k): int(v) for k, v in json.loads(Path(maxima_path).read_text()).items()}
except Exception:
    maxima = {}

def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

def rss_kb(pid):
    try:
        proc = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    text = proc.stdout.strip()
    return int(text) if text.isdigit() else None

lines = ["max_rss_kb:"]
for line in Path(pid_file).read_text().splitlines():
    parts = line.split()
    if len(parts) != 2:
        continue
    name, pid_text = parts
    try:
        pid = int(pid_text)
    except ValueError:
        continue
    value = maxima.get(name)
    # 进程仍存活时补一次终态采样；已退出的 PID 保留持续采样期间的峰值而不是丢弃。
    if alive(pid):
        sample = rss_kb(pid)
        if sample is not None:
            value = max(value or 0, sample)
    if value is None:
        lines.append(f"  {name}: null (pid={pid}, never sampled)")
    else:
        lines.append(f"  {name}: {value} (pid={pid}, alive={str(alive(pid)).lower()}, "
                     f"source=max-of-continuous-and-terminal)")
Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
PY
  cat "$RUN_DIR/rss.txt" >&2
}

start_rss_sampler() { # 后台持续采样本脚本自己启动的 PID，记录各自历史最大 RSS
  rm -f "$RSS_STOP"
  "$ENGINE_PY" - "$PID_FILE" "$RSS_MAX" "$RSS_STOP" > "$LOG_DIR/rss_sampler.log" 2>&1 <<'PY' &
import json, os, subprocess, sys, time
from pathlib import Path

pid_file, out_path, stop_path = sys.argv[1:4]
maxima: dict[str, int] = {}

def rss_kb(pid):
    try:
        proc = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    text = proc.stdout.strip()
    return int(text) if text.isdigit() else None

while not os.path.exists(stop_path):
    try:
        lines = Path(pid_file).read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        parts = line.split()
        if len(parts) != 2:
            continue
        name, pid_text = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        value = rss_kb(pid)
        if value is not None:
            maxima[name] = max(maxima.get(name, 0), value)
    Path(out_path).write_text(json.dumps(maxima, sort_keys=True), encoding="utf-8")
    time.sleep(0.2)
PY
  RSS_SAMPLER_PID=$!
  echo "rss-sampler $RSS_SAMPLER_PID" >> "$PID_FILE"
}

stop_rss_sampler() {
  [ -n "$RSS_SAMPLER_PID" ] || return 0
  touch "$RSS_STOP"
  for _ in $(seq 1 20); do
    kill -0 "$RSS_SAMPLER_PID" 2>/dev/null || { wait "$RSS_SAMPLER_PID" 2>/dev/null || true; RSS_SAMPLER_PID=""; return 0; }
    sleep 0.1
  done
  kill -TERM "$RSS_SAMPLER_PID" 2>/dev/null || true
  sleep 0.2
  kill -KILL "$RSS_SAMPLER_PID" 2>/dev/null || true
  wait "$RSS_SAMPLER_PID" 2>/dev/null || true
  RSS_SAMPLER_PID=""
}

cleanup() {
  local code=$?
  trap - EXIT INT TERM
  log "tearing down (own pids only)"
  write_rss
  stop_all
  if ! write_summary "$code" >&2; then
    [ "$code" -ne 0 ] || code=1
    log "cleanup verification failed"
  fi
  exit "$code"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_rss_sampler

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

# 4) 建任务 → 等每个 assignment endpoint 监听就绪 → 送 prompt → 核对 Controller 闭环
task="$(curl -fsS -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"model_id\":\"lastcore/tiny-cpu\",\"num_layers\":$MODEL_LAYERS,\"layer_memory_mb\":64,\"max_nodes\":2,\"timeout_seconds\":120}" \
  "$BASE_URL/v1/tasks")" || { log "task creation failed"; exit 1; }
printf '%s\n' "$task" > "$RUN_DIR/task.json"

"$ENGINE_PY" - "$RUN_DIR/task.json" "$RUN_DIR/endpoints.txt" "$RUN_DIR/first_endpoint.txt" <<'PY'
import json, sys
from urllib.parse import urlparse

task = json.load(open(sys.argv[1], encoding="utf-8"))
assignments = sorted(task.get("assignments") or [], key=lambda item: item["position"])
if not assignments:
    raise SystemExit("task has no assignments")

def split(endpoint):
    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
    if not parsed.hostname or not parsed.port:
        raise SystemExit(f"cannot parse assignment endpoint: {endpoint!r}")
    if ":" in parsed.hostname:
        raise SystemExit(f"IPv6 assignment endpoint unsupported by the IPv4 transport: {endpoint!r}")
    return parsed.hostname, parsed.port

rows, first = [], None
for assignment in assignments:
    host, port = split(assignment["endpoint"])
    rows.append(f"{assignment['position']} {host} {port} {assignment['endpoint']}")
    if first is None and int(assignment.get("layer_start") or 0) == 0:
        first = assignment["endpoint"]
if first is None:
    raise SystemExit("task has no first-stage (layer_start=0) assignment")
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(rows) + "\n")
open(sys.argv[3], "w", encoding="utf-8").write(first + "\n")
PY
[ -s "$RUN_DIR/endpoints.txt" ] || { log "could not enumerate assignment endpoints"; exit 1; }
first_endpoint="$(<"$RUN_DIR/first_endpoint.txt")"
[ -n "$first_endpoint" ] || { log "no first-stage assignment"; exit 1; }

# 必须等每个 assignment endpoint 的监听器都就绪：只看首段会在后段尚未 bind 时误发，
# 首段对 downstream 的一次性 connect 失败会让脚本间歇性失败。
while read -r position host port endpoint; do
  [ -n "$port" ] || continue
  for _ in $(seq 1 120); do
    port_open "$port" && break
    sleep 0.25
  done
  port_open "$port" || { log "assignment listener (position=$position) never opened on $host:$port"; exit 1; }
  log "assignment listener ready: position=$position $host:$port"
done < "$RUN_DIR/endpoints.txt"
log "all assignment listeners ready; first stage: $first_endpoint"

task_id="$(printf '%s' "$task" | "$ENGINE_PY" -c 'import json,sys
print(json.load(sys.stdin)["id"])')"
[ -n "$task_id" ] || { log "task id missing from Controller response"; exit 1; }

PYTHONPATH="$ROOT/node-agent:$ROOT/inference-engine:$ROOT/communication" "$ENGINE_PY" \
  "$ROOT/integration/pipeline_client.py" --endpoint "$first_endpoint" \
  --config "$RUN_DIR/tiny.json" --weights "$RUN_DIR/tiny.npz" --prompt "$PROMPT" \
  --out "$RUN_DIR/result.json" > "$LOG_DIR/client.log" 2>&1 || true
cat "$LOG_DIR/client.log" >&2
grep -q '"bit_identical_to_single_machine": true' "$RUN_DIR/result.json" || { log "client comparison failed"; exit 1; }

# 结果 JSON 相等还不够：必须确认 Controller 已收到任务里每个 assignment 的 completed
# 报告，脚本不能在 Node Agent 上报前就宣布 ALL_OK。
controller_closed=false
verdict="no snapshot"
for _ in $(seq 1 120); do
  curl -fsS -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE_URL/v1/tasks/$task_id" \
    -o "$RUN_DIR/task_state.json" 2>/dev/null || true
  if verdict="$("$ENGINE_PY" - "$RUN_DIR/task_state.json" <<'PY'
import json, sys
from pathlib import Path

try:
    view = json.loads(Path(sys.argv[1]).read_text())
except Exception:
    raise SystemExit(1)
assignments = view.get("assignments") or []
statuses = [str(a.get("status")) for a in assignments]
complete = bool(assignments) and view.get("status") == "completed" and all(
    status == "completed" for status in statuses)
print(f"task={view.get('status')} assignments=[{','.join(statuses)}]")
raise SystemExit(0 if complete else 1)
PY
)"; then
    controller_closed=true
    log "controller closed the task: $verdict"
    break
  fi
  sleep 0.5
done
$controller_closed || { log "controller never reported all assignments completed (last: $verdict)"; exit 1; }

ALL_OK=true

write_rss

log "done: artifacts in $RUN_DIR"
exit 0
