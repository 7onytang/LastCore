# LastCore Node Agent

节点侧常驻程序：对接 `controller-server/`（PR #1）的控制平面接口，负责设备入网、凭据持久化、心跳、任务轮询/去重、执行生命周期（上报、取消、超时停止），并通过 `Executor` 接口把实际计算委托给推理引擎（由 `inference-engine/` 提供，见 `PROTOCOL.md`）。

## 职责边界

| 归属 | 内容 |
| --- | --- |
| node-agent | 注册、凭据保存、心跳、轮询 assignments、任务去重、running/completed/failed 上报、取消/超时/心跳断链时的本地停止、执行日志 |
| inference-engine / communication | 模型片段加载、逐层计算、节点间 tensor 传输（实现 node-agent 定义的 `Executor` 接口与 `PROTOCOL.md` 协议） |
| controller-server | 节点与任务状态、调度分配、取消/超时判定（Agent 无法被强制终止，需遵守协议主动停止） |

## 目录结构

```
node-agent/
├── lastcore_node_agent/
│   ├── __main__.py      # CLI 入口
│   ├── config.py        # 配置（CLI > 环境变量 > 默认值）
│   ├── credentials.py   # 凭据、执行日志与待确认终态上报的原子持久化
│   ├── controller.py    # Controller HTTP 客户端
│   ├── executor.py      # Executor 接口、ExecutionContext、demo 执行器
│   └── agent.py         # 主循环：心跳/watchdog 线程 + 轮询 + 执行编排
├── tests/
├── PROTOCOL.md          # 执行与 tensor 协议（提案，待 Engineer-B 确认）
├── requirements.txt
└── pytest.ini
```

## 安装与运行

Python 3.12+：

```bash
cd node-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

首次入网需要注册 token（环境变量，不要写进命令行参数）：

```bash
export NODE_AGENT_REGISTRATION_TOKEN="<controller 的注册 token>"
python -m lastcore_node_agent \
  --controller-url http://127.0.0.1:8000 \
  --endpoint http://127.0.0.1:9000 \
  --available-memory-mb 8192 \
  --compute-weight 3.0
```

注册成功后凭据写入状态目录，此后重启自动复用，不再重复注册；`NODE_AGENT_REGISTRATION_TOKEN` 仅在状态目录没有凭据时使用。

### 配置

| CLI 参数 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `--controller-url` | `NODE_AGENT_CONTROLLER_URL` | 必填 | Controller 地址 |
| `--endpoint` | `NODE_AGENT_ENDPOINT` | 必填 | 其他节点可达的本机通信地址，注册时上报 |
| `--available-memory-mb` | `NODE_AGENT_AVAILABLE_MEMORY_MB` | 必填 | 可用内存上报值 |
| `--compute-weight` | `NODE_AGENT_COMPUTE_WEIGHT` | `1.0` | 相对算力权重 |
| `--name` / `--platform-name` | `NODE_AGENT_NAME` / `NODE_AGENT_PLATFORM` | 主机名 / `platform.platform()` | 注册展示信息 |
| `--state-dir` | `NODE_AGENT_STATE_DIR` | `~/.lastcore/node-agent` | 凭据与执行日志目录（0600/0700） |
| `--heartbeat-interval` | `NODE_AGENT_HEARTBEAT_INTERVAL` | `10` | 心跳间隔秒数，需小于 TTL（默认 30） |
| `--poll-interval` | `NODE_AGENT_POLL_INTERVAL` | `1` | assignments 轮询间隔秒数 |
| `--executor` | `NODE_AGENT_EXECUTOR` | `demo` | `demo`、`stage`（真实推理）或 `模块路径:工厂名` |
| `--model-config` | `NODE_AGENT_MODEL_CONFIG` | 内置 tiny | `stage` 模式的模型配置 JSON |
| `--weights` | `NODE_AGENT_WEIGHTS` | 状态目录内自动生成 | `stage` 模式的 checkpoint 路径 |
| `--complete-after` | `NODE_AGENT_COMPLETE_AFTER` | `1` | `stage` 模式服务完 N 个请求后上报 completed；`0` = 直到取消 |
| `--no-accept` | `NODE_AGENT_NO_ACCEPT` | 关 | 排空模式，心跳 `accepting_tasks=false` |
| `--request-timeout` | - | `10` | 单次 HTTP 超时秒数 |

退出码：`0` 正常停止；`2` 持久化凭据被 Controller 拒绝（401，禁止自动重新注册，避免产生重复节点身份，需人工处理）；`3` Controller 错误；`4` 本地状态/配置错误。

## 生命周期语义

- **凭据持久化**：`node_id`/`node_token` 原子写入状态目录（临时文件 + fsync + rename，0600），文件损坏时报错退出而不是静默重新注册。
- **单进程独占**：状态目录通过 `agent.lock` 文件锁独占，同一凭据目录同时只允许一个 Agent 进程，防止重复注册与同一任务的重复执行；第二个进程启动时报错退出（码 4）。
- **入网重试**：启动时 Controller 不可达会每秒重试注册/首次心跳，直到成功或收到停止信号，不会因此退出。
- **心跳**：独立线程按间隔发送可用内存与接单意愿；断网只重试并计数，不改变任务执行。HTTP 客户端不使用环境/系统代理（`trust_env=False`），避免本机代理拦截 Controller 地址。
- **凭据值校验**：注册响应与本地凭据使用同一校验：节点 ID 和令牌必须是非空字符串；`heartbeat_ttl_seconds` 必须是 5–3600 的 JSON 整数，与 Controller 配置范围一致。布尔、零、负数、字符串、浮点及非有限数均拒绝，不转型或静默替换为默认值；仅旧记录或响应中完全缺失 TTL 时保留 30 秒默认值。异常本地记录会原样保留并在任何网络请求前停止；异常注册响应不会落盘，也不会继续心跳或领任务。应核对原 Controller/备份后恢复，不删除原身份或 pending 日志绕过检查。校验复用标准库 `dataclasses` 和 [CPython JSON](https://github.com/python/cpython/blob/main/Doc/library/json.rst)（PSF 许可）；已核对 Pydantic 的严格类型方案，但为此增加节点运行依赖与冻结包体积没有必要。
- **轮询与去重**：按 `assignment_id` 去重，同一分配只启动一次执行；同一时刻本地最多一个任务；优先启动最早创建的合格任务（先处理旧任务）。
- **启动条件**：`task_status` 为 queued/running、当前时间早于 deadline。先上报 `running` 成功才开始执行；Controller 返回 409（任务已终态）则静默跳过。
- **执行日志（journal）**：执行结束（completed/failed/stopped）先写本地日志再上报 Controller，保证重启后不会重复执行同一 assignment；本地崩溃窗口内的重复执行无法完全避免（无分布式事务）。
- **去重日志损坏保护**：只有缺失的 `journal.json` 或明确的 `{"settled":[]}` 表示没有历史记录。已有文件必须保持 `{"settled":[{"id":"…","status":"…"}]}` 结构，最多 512 条；状态允许 `completed`、`failed`、`stopped`、`skipped-terminal`，重复 ID 保留最后状态。空对象、`settled:null`、非法条目、未知状态/字段及超长列表会触发 `StateError`。启动时在注册、心跳和任务轮询之前停止并发出 `recovery-required` 本地事件；运行中写去重日志发现损坏也停止，不过滤条目或覆盖原证据。保留整个状态目录（含凭据及 pending 回报），从可信备份恢复并核对任务归属后再启动；不能删除或清空日志来绕过恢复。实现复用 [CPython 标准库 JSON](https://github.com/python/cpython/blob/main/Lib/json/decoder.py)（PSF 许可）及现有原子写入，无新增运行依赖；引入 Celery 等完整任务框架会改变现有 HTTP 协议与恢复存储，不适合此修复。
- **取消/超时停止**：轮询发现任务 cancelled/failed、或进程收到停止信号时，设置执行器取消事件并等待其退出（宽限期 `stop_grace`，默认 10s）。deadline 与心跳 TTL 由独立的本地 watchdog 线程按秒检查，assignments 轮询失败或阻塞（含整机断网、心跳同时失败）期间仍然运行，超限后有界停止执行器，不依赖网络恢复。本地停止后不再上报（Controller 侧已终态或会因心跳过期而失败），该 assignment 记为 `stopped`，永不重复执行。
- **执行失败**：执行器抛异常时上报 `failed` 并携带错误摘要，Controller 会终止整个任务。
- **上报重试**：终态上报（含 executor 不可用路径）先持久化待确认记录（`pending-reports.json`，0600），成功落盘后才写入执行日志 journal 进入不可再执行终态（两次写入间崩溃重启仍可只补报；仅 legacy 状态型回报在 pending 落盘失败时暂不 settle、保留旧的可重执行规则；π/日志 typed 数值结果持久化失败则进入恢复状态并停止新执行，不发送未落盘结果，不降格为 legacy 回报或重新计算，已有证据保留），再最多重试 3 次；legacy 状态型回报收到成功 ACK 或 404/409（Controller 已终态）后清除记录；新 π/日志数值回报须完整匹配 ACK，未知响应或 404/409 保留记录并阻止新执行（详见后文）；重试耗尽保留记录，连接恢复或进程重启后只重放上报（Controller 对同状态上报幂等），不重新执行。pending 中的 assignment 参与执行去重，journal 淘汰旧记录后也不会被重新选择执行。

## 与推理引擎对接

无需真实节点或模型的恢复回归：在 `node-agent/` 安装 `requirements-dev.txt` 后执行 `python -m pytest -q tests/test_credentials.py tests/test_settled_recovery.py tests/test_credential_recovery.py`。测试覆盖损坏文件原字节保留、凭据与 TTL 校验、启动前禁止网络/执行、运行中停止、合法旧状态及 512 条淘汰；不代表真实设备验收。

终态报告收到 Controller ACK 后，只有磁盘 pending journal 清理成功才移除内存重放记录；若清理暂时失败，下次轮询按原 payload 重报，typed 结果在此期间继续阻止新任务。纯 Fake 验证：`python -m pytest -q tests/test_workload_contract.py -k ack_cleanup_failure`，覆盖普通状态与 typed 结果的清理失败、原 journal 保留及恢复后的幂等重报。

`executor.py` 定义了唯一接入点：

```python
class Executor:
    def run(self, context: ExecutionContext) -> None: ...
```

- 每个 assignment 创建一个新的 `Executor` 实例（`--executor 模块:工厂名`），保证任务间 tensor/模型状态隔离。
- `run()` 阻塞执行；必须至少每秒检查一次 `context.cancel`，置位后在宽限期内抛出 `ExecutionCancelled(context.cancel_reason)` 返回。
- `context` 提供 `task_id`、`assignment_id`、`model_id`、`[layer_start, layer_end)`、上下游节点、`deadline`，以及 `raw`（Controller 返回的原始 assignment dict）。
- `SliceDemoExecutor`（`demo`）是参考实现；`stage` 执行器对接 inference-engine v1（见下节）。协议细节见 `PROTOCOL.md`。

## 真实推理（--executor stage，对接 inference-engine v1）

```bash
# inference-engine 与 communication 的包目录加入 PYTHONPATH（相对仓库根）
PYTHONPATH=../inference-engine:../communication \
python -m lastcore_node_agent --controller-url http://127.0.0.1:8000 \
  --endpoint http://127.0.0.1:9101 --state-dir ~/.lastcore/node-a \
  --available-memory-mb 4096 --compute-weight 2.0 --executor stage --verbose
```

- StageServer 绑定 `--endpoint` 指定的地址：**注册给 Controller 的 endpoint 就是本段真实监听地址**，相邻节点按 assignment 的 `next_node.endpoint` 直连。
- 模型：默认 inference-engine 的 tiny 模型（`lastcore/tiny-cpu`，4 层，确定性权重自动生成到状态目录）；`--model-config <json>` 与 `--weights <npz>` 可替换为自定义 checkpoint，任务 `model_id`/`num_layers` 必须与之一致。
- 完成语义：`--complete-after N`（默认 1）——服务完 N 个推理请求后上报 completed；`0` = 持续服务直到取消/超时/退出。
- 调用方（demo/未来 frontend）用 `inference_engine.PipelineClient(<首段 endpoint>).run(tokens)` 发起推理。
- 依赖：numpy 与 `inference-engine/`、`communication/` 模块（`pip install numpy`）。

## 验证

```bash
cd node-agent
source .venv/bin/activate
pytest -q
```

本地双节点联调（真实 Controller + 两个 Agent 进程）：

```bash
# 终端 1：Controller
cd controller-server && source .venv/bin/activate
export CONTROLLER_ADMIN_TOKEN=... CONTROLLER_REGISTRATION_TOKEN=...
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

# 终端 2/3：两个节点（各自 state-dir、endpoint）
cd node-agent && source .venv/bin/activate
export NODE_AGENT_REGISTRATION_TOKEN=...
python -m lastcore_node_agent --controller-url http://127.0.0.1:8000 \
  --endpoint http://127.0.0.1:9101 --state-dir ~/.lastcore/node-a \
  --available-memory-mb 4096 --compute-weight 2.0 --verbose
python -m lastcore_node_agent --controller-url http://127.0.0.1:8000 \
  --endpoint http://127.0.0.1:9102 --state-dir ~/.lastcore/node-b \
  --available-memory-mb 2048 --compute-weight 1.0 --verbose

# 终端 4：管理员创建任务并观察
curl -s -X POST http://127.0.0.1:8000/v1/tasks -H "Authorization: Bearer $CONTROLLER_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model_id":"demo","num_layers":8,"layer_memory_mb":512,"timeout_seconds":120}'
curl -s http://127.0.0.1:8000/v1/tasks/<task_id> -H "Authorization: Bearer $CONTROLLER_ADMIN_TOKEN"
```

取消/超时验证：对运行中的任务调用 `POST /v1/tasks/{id}/cancel`，或用较短 `timeout_seconds` 创建任务，观察两个 Agent 日志出现 `stopped locally` 并释放执行线程。

## 已知限制

- 心跳内存上报为静态配置值，暂无自动探测。
- 执行日志只防"已结束"的任务重复执行，进程崩溃窗口内的工作丢失后会重新执行（at-least-once）。
- 执行器忽略取消超过 `stop_grace` 时，线程被遗弃（daemon），资源释放依赖执行器自身遵守契约。
- `stage` 执行器的模型权重仍需手动/自动生成本地 checkpoint，模型下载与缓存属 `model-manager/`（LC-9）。

## Explicit CPU-demo workload capability

With `--executor demo`, the explicit new `pi` and `log_stats` workloads perform
actual bounded CPU arithmetic (deterministic Monte Carlo shard counts and log
level counts). Legacy demo assignments still simulate lifecycle slices; neither
path is a language model or proves activation relay. No historical task/device
acceptance is transferred to this CPU-demo path.

Only the updated built-in `demo` executor advertises `pi` and `log_stats` in
registration and authenticated heartbeats. Custom/injected factories default
to no new capability; an OS name, online heartbeat or old linear client is not
a capability claim. Existing identity credentials are retained. Deploy the
compatible Controller before enabling the updated executor; an old Controller
rejects the new strict heartbeat field. An old heartbeat clears stale capability
claims, and new workload scheduling rejects an empty compatible pool.

The Controller adds `node_workloads` and `workload_results` without changing
the legacy nine-column nodes or eight-column assignments. Linear `result` stays
list-or-null in its existing table; pi/log use the separate typed `workload_result`.
Old servers do not display these typed results. Before any future downgrade,
stop new admissions and complete/cancel all active pi/log tasks, then downgrade
clients before the server so capability fields are not sent. Keep the same live
database and its new tables; never restore an old snapshot over later writes.
The Controller source is deployed at `73b3d1`; its public page/resource checks
are recorded in [the deployment summary](../submission/evidence/public-deployment-current.md).
No new executor registration or pi/log task/device acceptance was performed.
Existing tiny and targeted-linear execution remain independent.

Pi checks cancellation/deadline at entry, each 256 samples and before return;
log checks each line and before return. New result reports persist the source
binding and require matching assignment acknowledgements. Unknown ACKs retain
pending outcomes and block new work; 404/409 numeric-result conflicts require
recovery and are retained. Legacy status-only and the published linear override
retain their five-argument report call. Use the pure compatibility regressions
under `tests/test_workload_contract.py`; they do not launch a real worker or model.

After stopping admissions, the authorized operator can run the read-only gate
against the current live database (without copying/restoring any snapshot):
`python -m app.workload_downgrade --database /absolute/path/controller.sqlite3`
from `controller-server/`. Exit 0 means no active typed tasks in that snapshot;
1 blocks downgrade and 2 means it could not be verified. Keep admissions stopped
through downgrade; this snapshot gate cannot fence a separate running writer.
Terminal typed records remain in `workload_results`, invisible to the old API;
retain the live tables for re-upgrade. Do not claim full old-image typed support.

The server gate checks only the database snapshot, not remote worker journals.
Before downgrading an affected worker, stop it after cleanup and verify its
pending typed outcome count is zero using the updated source/environment:

```sh
python - <<'PY'
from pathlib import Path
from lastcore_node_agent.credentials import StateError, load_pending_report_results
try:
    pending = load_pending_report_results(Path('/absolute/private/worker-state'))
except (OSError, StateError):
    print('recovery gate: unable to verify; preserve state')
    raise SystemExit(2)
print('pending typed outcomes:', len(pending))
raise SystemExit(1 if pending else 0)
PY
```

Exit 1 or 2 blocks worker downgrade: resolve the records under the updated client
or preserve/quarantine that state without allowing an old client to replay it.
An old client cannot validate the new journal and may discard unknown terminal
records. Do not delete pending state to obtain a zero count. Keep admissions and
workers stopped through the gate/downgrade window; no snapshot here fences an
independent writer.
