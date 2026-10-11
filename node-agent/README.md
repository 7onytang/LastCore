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
│   ├── credentials.py   # 凭据与执行日志的原子持久化
│   ├── controller.py    # Controller HTTP 客户端
│   ├── executor.py      # Executor 接口、ExecutionContext、demo 执行器
│   └── agent.py         # 主循环：心跳线程 + 轮询 + 执行编排
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
| `--executor` | `NODE_AGENT_EXECUTOR` | `demo` | `demo` 或 `模块路径:工厂名`（B 的引擎接入点） |
| `--no-accept` | `NODE_AGENT_NO_ACCEPT` | 关 | 排空模式，心跳 `accepting_tasks=false` |
| `--request-timeout` | - | `10` | 单次 HTTP 超时秒数 |

退出码：`0` 正常停止；`2` 持久化凭据被 Controller 拒绝（401，禁止自动重新注册，避免产生重复节点身份，需人工处理）；`3` Controller 错误；`4` 本地状态/配置错误。

## 生命周期语义

- **凭据持久化**：`node_id`/`node_token` 原子写入状态目录（临时文件 + fsync + rename，0600），文件损坏时报错退出而不是静默重新注册。
- **单进程独占**：状态目录通过 `agent.lock` 文件锁独占，同一凭据目录同时只允许一个 Agent 进程，防止重复注册与同一任务的重复执行；第二个进程启动时报错退出（码 4）。
- **入网重试**：启动时 Controller 不可达会每秒重试注册/首次心跳，直到成功或收到停止信号，不会因此退出。
- **心跳**：独立线程按间隔发送可用内存与接单意愿；断网只重试并计数，不改变任务执行。HTTP 客户端不使用环境/系统代理（`trust_env=False`），避免本机代理拦截 Controller 地址。
- **轮询与去重**：按 `assignment_id` 去重，同一分配只启动一次执行；同一时刻本地最多一个任务；优先启动最早创建的合格任务（先处理旧任务）。
- **启动条件**：`task_status` 为 queued/running、当前时间早于 deadline。先上报 `running` 成功才开始执行；Controller 返回 409（任务已终态）则静默跳过。
- **执行日志（journal）**：执行结束（completed/failed/stopped）先写本地日志再上报 Controller，保证重启后不会重复执行同一 assignment；本地崩溃窗口内的重复执行无法完全避免（无分布式事务）。
- **取消/超时停止**：轮询发现任务 cancelled/failed、到达 deadline、心跳续约失败超过 TTL、或进程收到停止信号时，设置执行器取消事件并等待其退出（宽限期 `stop_grace`，默认 10s）。本地停止后不再上报（Controller 侧已终态或会因心跳过期而失败），该 assignment 记为 `stopped`，永不重复执行。
- **执行失败**：执行器抛异常时上报 `failed` 并携带错误摘要，Controller 会终止整个任务。
- **上报重试**：终态上报最多重试 3 次；404/409 视为 Controller 已终态，放弃重试。

## 与推理引擎对接

`executor.py` 定义了唯一接入点：

```python
class Executor:
    def run(self, context: ExecutionContext) -> None: ...
```

- 每个 assignment 创建一个新的 `Executor` 实例（`--executor 模块:工厂名`），保证任务间 tensor/模型状态隔离。
- `run()` 阻塞执行；必须至少每秒检查一次 `context.cancel`，置位后在宽限期内抛出 `ExecutionCancelled(context.cancel_reason)` 返回。
- `context` 提供 `task_id`、`assignment_id`、`model_id`、`[layer_start, layer_end)`、上下游节点、`deadline`。
- `SliceDemoExecutor` 是参考实现（按时间片模拟工作，演示取消契约），用于联调与测试。

节点间 tensor 传输协议见 `PROTOCOL.md`（提案，由 `communication/` 实现）。

## 桌面部署（macOS / Windows，LC-12）

macOS 与 Windows 的常驻启动、安装打包方案、跨平台差异核对与入网前环境检查见 `desktop/README.md`：

- `desktop/run_agent.py`：安全读取私有 `NODE_AGENT_*` 配置文件后启动节点；不执行 shell 配置、不把 token 放入进程参数。
- `desktop/preflight.py`：入网前可执行检查（Python/httpx/状态目录/锁/凭据/端口/Controller/资源），任何 FAIL 退出码 1，支持 `--json`。POSIX 新建状态目录使用 0700；已有目录若对 group/other 可访问则失败，需由用户手动收紧权限。配置 `--min-memory-mb` 后若可用内存探测未知或低于门槛也会失败。
- `desktop/com.lastcore.node-agent.plist`：macOS LaunchAgent 模板（登录自启、崩溃自动拉起）。
- `desktop/windows/`：Windows Task Scheduler 安装/卸载与包装脚本（登录自启、失败自动重启）。
- `desktop/env.example`：环境变量模板（注册 token 仅首次入网前使用，注册成功后删除）。
- 状态锁已支持 Windows（`msvcrt`），退出码语义不变。

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

- demo 执行器不产生真实推理输出；真实计算等待 `inference-engine/` 接入 `Executor` 接口。
- 心跳内存上报为静态配置值，暂无自动探测。
- 执行日志只防"已结束"的任务重复执行，进程崩溃窗口内的工作丢失后会重新执行（at-least-once）。
- 执行器忽略取消超过 `stop_grace` 时，线程被遗弃（daemon），资源释放依赖执行器自身遵守契约。
