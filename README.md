# LastCore

将日常设备组织为分布式 AI 推理网络。仓库包含 Controller 服务、多节点 Agent，以及可现场操作的分布式计算演示看板，代码位于 `controller-server/`。

## 已实现

- Python + FastAPI HTTP API，SQLite 持久化节点、任务、分层分配及状态。
- 节点注册、独立节点凭据、心跳、在线/忙碌/排空/离线状态。
- 按可用内存及计算权重分配连续模型层，返回上下游节点地址。
- 节点轮询任务、回报执行状态；取消、超时和节点离线导致任务终止。
- 内置 `lastcore/linear-demo-v1` 线性推理任务：Agent 分别计算输出向量片段，Controller 持久化并汇总为完整输出。
- 内置确定性 Monte Carlo 圆周率估算和分布式日志级别统计任务，共用 Controller 调度、Agent 领取和状态汇总流程。
- Controller 根路径提供演示看板，可查看节点和任务，并提交圆周率或日志统计任务。
- 一条命令启动 Controller 和三个独立 Agent，演示分片推理并与单节点参考输出比对。
- SQLite 写事务防止并发任务重复预留节点；重启保留任务与预留状态。
- 非 root Docker 镜像、Compose 数据卷和健康检查，以及自动化测试。

内置模型是可复现的 tanh(Wx+b) 演示模型，不是预训练大模型。Agent 启动时运行短 CPU 基准生成相对算力权重，并每 5 秒采集系统可用内存供调度使用；也可通过 CLI 参数手动覆盖。当前未测 GPU、网络速度或运行中 CPU 负载，通用模型加载、跨节点 tensor 流水传输、NAT 穿透和离线迁移尚未实现。内存估算仍需为模型权重及运行开销留出余量。

圆周率和日志统计是无需模型下载的 CPU 演示负载。Controller 把采样区间或日志行区间分配给在线 Agent，再合并各节点结果；这用于展示调度、并行执行和归并，不代表系统已经实现失败任务自动重试。

## 本地运行

Python 3.12 或更高版本：

```bash
cd controller-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

### 运行多节点核心演示

在 `controller-server/` 目录、完成上面的依赖安装后运行：

```bash
python -m app.demo
```

演示会启动临时 Controller 和三个 Agent，打印各节点基准分数及内存、分配范围，计算 12 维输出向量，并验证分布式输出与单节点结果一致。进程退出后临时数据库和演示凭据会清理。

### 本地启动 Controller

分别生成两个随机 token，保存到 shell 环境；不要提交凭据：

```bash
export CONTROLLER_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export CONTROLLER_REGISTRATION_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

另一个终端访问 `http://127.0.0.1:8000/healthz`，浏览器访问 `http://127.0.0.1:8000/docs`。接口文档的 Authorize 输入框填写所需 Bearer token。默认数据库为 `controller-server/data/controller.sqlite3`；可用 `CONTROLLER_DATABASE_PATH` 覆盖。缺少 token、token 少于 32 字符或两者相同，服务会拒绝启动。

浏览器访问 `http://127.0.0.1:8000/` 打开演示看板，输入管理员 token 后可以运行两个分布式示例。部署到 ECS 后，经 HTTPS 反向代理访问域名根路径即可展示。

### 连接真实设备运行 Agent

在每台设备上安装依赖并执行以下命令。将 Controller URL、节点名称和注册 token 替换为实际值。Agent 会将节点凭据保存在 `~/.lastcore/node.json`，重启后复用原节点身份。圆周率和日志统计示例只需要 Worker 主动访问 Controller，不要求 Worker 开放入站端口；`endpoint` 是登记字段，这两个示例不会访问它。

```bash
CONTROLLER_REGISTRATION_TOKEN="<registration-token>" \
python -m app.agent \
  --controller "https://your-ecs-domain.example.com" \
  --name "device-1" \
  --endpoint "http://device-1.local:9000"
```

默认会自动测算相对 CPU 权重并采集系统可用内存；需要固定调度输入时，可设置 `--compute-weight` 或 `--memory-mb` 覆盖。同一台机器启动多个 Worker 时，为每个进程设置不同的 `--name` 和 `--state-file`，避免复用同一节点身份。演示前至少启动一个 Worker；推荐启动 2-3 个以观察分片派发。Agent 支持 `lastcore/linear-demo-v1`、`lastcore/pi-monte-carlo-v1` 和 `lastcore/log-statistics-v1`，任务及结果格式见 [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md)。

## Docker 部署

云服务器需要 Docker Engine 和 Compose v2：

```bash
cd controller-server
cp .env.example .env
# 在 .env 中填写两个不同的随机 token
docker compose config --quiet
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8000/healthz
docker compose logs --tail 100 controller
```

Compose 默认仅绑定云服务器的 `127.0.0.1:8000`。公网节点接入通过服务器上的 HTTPS 反向代理转发到该端口，配置域名、证书和防火墙。在受信任的 VPN/内网中，可将 `.env` 的 `CONTROLLER_BIND_ADDRESS` 设置为服务器内网地址；仅在明确需要时绑定 `0.0.0.0`。认证令牌通过 TLS 或受信任的加密隧道传输。

持久化数据在 `controller-data` 数据卷。`docker compose down` 停止服务且保留数据；`docker compose down -v` 会删除数据，不能用于常规更新。更新使用 `docker compose up -d --build --wait`。首版只部署一个 Controller 实例，数据库放本机文件系统，不放 NFS。备份使用 SQLite backup API 或停服后复制整个数据卷，不能只复制正在写入的 `.sqlite3` 文件。

## API 与权限

当前接口的字段、认证、状态流转、轮询限制和错误语义详见 [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md)。

所有受保护接口使用 `Authorization: Bearer <token>`。注册 token 仅用于入网，管理员 token 用于创建和查看任务，注册返回的节点 token 仅管理该节点。节点 token 只返回一次，服务器保存 SHA-256 摘要；节点程序需要持久化 `node_id` 和 `node_token`。断线后使用原凭据重新发心跳，避免重复注册。

| 方法 | 路径 | 凭据 | 用途 |
| --- | --- | --- | --- |
| GET | `/healthz` | 无 | 进程健康检查 |
| POST | `/v1/nodes/register` | 注册 token | 注册设备，返回节点 ID 和 token |
| GET | `/v1/nodes` | 管理员 | 查看资源及节点状态 |
| POST | `/v1/nodes/{id}/heartbeat` | 对应节点 token | 更新可用内存及接单意愿 |
| GET | `/v1/nodes/{id}/assignments` | 对应节点 token | 查询最近 100 个相关任务的分配及终止状态 |
| POST | `/v1/nodes/{id}/assignments/{assignment_id}/report` | 对应节点 token | 上报 running/completed/failed |
| POST | `/v1/tasks` | 管理员 | 原子预留节点并创建分层任务 |
| GET | `/v1/tasks` | 管理员 | 最近 100 个任务 |
| GET | `/v1/tasks/{id}` | 管理员 | 完整任务计划和状态 |
| POST | `/v1/tasks/{id}/cancel` | 管理员 | 取消任务 |

节点注册示例：

```json
{
  "name": "Mac Mini A",
  "platform": "macOS-arm64",
  "endpoint": "http://10.0.0.11:9000",
  "available_memory_mb": 8192,
  "compute_weight": 3.0
}
```

`endpoint` 是其他节点可访问的通信地址，由节点提供；Controller 不主动连接它，也不验证其可达性。首版要求节点彼此互通，单纯连接公网 Controller 不会解决手机/电脑间的 NAT 问题。`compute_weight` 只用于相对算力比较。

心跳示例（建议每 10 秒一次，默认 30 秒超时）：

```json
{"available_memory_mb": 8192, "accepting_tasks": true}
```

设 `accepting_tasks=false` 可暂停接收新任务，当前任务仍继续运行。心跳不替代任务回报；节点轮询也不替代心跳。

提交内置线性推理任务示例（权重由 seed 确定性生成，不下载外部模型文件）：

```json
{
  "model_id": "lastcore/linear-demo-v1",
  "num_layers": 12,
  "layer_memory_mb": 1,
  "max_nodes": 3,
  "timeout_seconds": 600,
  "input_vector": [0.25, -0.5, 1.0, 0.75, -0.125],
  "weight_seed": 2026
}
```

对内置模型，`num_layers` 是输出向量长度，分配范围是输出单元索引；输入向量和 seed 会随 assignment 提供给 Agent。节点独立计算各自输出片段，Controller 按分配顺序拼接结果。这个演示没有跨节点传输中间激活，也不代表预训练大模型推理。

每个分配含 `id`、`task_id`、`node_id`、`layer_start`、`layer_end`、`previous_node`、`next_node`。层区间统一使用 **左闭右开、从 0 开始**：`[0, 10)` 表示第 0 至第 9 层。下游与上游对象包含节点 ID、endpoint 和该节点的 assignment ID；流水线第一段的上游和最后一段的下游为 null。

调度优先考虑算力高的在线空闲节点，并按算力权重分配层数，受每个节点内存上限约束；若算力优先的组合装不下模型，则选择容量最大的节点组合。所有层必须完整覆盖且不重叠，否则返回 HTTP 409，不创建任务。一个节点同时只能属于一个活动任务，提前完成自己的片段后仍为整个流水线保留。

## Node Agent 对接约定

仓库提供的 `app.agent` 是可执行内置线性模型的 Python Agent；多节点快速验证见 `python -m app.demo`。以下协议同时约束后续接入其他执行器时的状态管理要求。

1. 使用注册 token 入网并保存返回的凭据。持续发心跳，并建议每秒轮询 assignments。
2. 根据 assignment ID 去重；相同任务的重复轮询不得启动第二次计算。启动前检查 `task_status` 为 queued/running、当前时间早于 `deadline`。
3. 当前 Agent 仅执行三个内置演示负载：`lastcore/linear-demo-v1` 按输出区间计算 `tanh(Wx+b)`，`lastcore/pi-monte-carlo-v1` 按确定性采样区间计算 π 估值，`lastcore/log-statistics-v1` 按日志行区间统计等级。未知 workload 或不匹配的 `model_id` 会明确回报 unsupported 错误。这些演示均不下载预训练模型，也不实现相邻节点间的 tensor 传输。
4. 持续检查任务状态。收到 cancelled/failed、到达 deadline 或无法续约心跳达到 TTL 时，停止本地执行并释放资源。先处理旧任务的终止，再启动新的任务，避免取消后仍继续占用资源。节点自身应在本地禁止并行执行两个任务，并对 task/assignment ID 隔离 tensor 数据。

**Controller 无法强制终止远端进程。** 当前取消或失败后会立即释放调度预留，实际计算停止依赖上述 Agent 协议。首版尚无强制 fencing、节点取消确认、命令保留游标和离线恢复，因此不适用于不遵守协议的节点。返回的最近 100 条记录用于原型接入；大规模使用需增加可靠命令队列。

任务 queued → running → completed。任一分配失败、任一已分配节点心跳过期或任务到期，整个任务变为 failed，其余未完成分配也变为 failed；管理员可将活动任务变为 cancelled。相同状态的回报可重试，终态不接受新的状态。Controller 会在 API 请求中以及后台定期扫描时执行超时清理，因此没有请求时也会释放过期任务的节点预留。节点回归不会恢复已失败的任务，需要创建新任务，暂不自动重试以避免重复计算。

## 验证

```bash
cd controller-server
source .venv/bin/activate
pytest -q
```

自动化测试覆盖两个节点的连续分层、算力权重及内存限制、鉴权和节点隔离、回报幂等、并发预留、断线/超时/取消/执行失败、数据库重启持久化和输入校验。多节点核心演示另行启动 HTTP Controller 与三个 Agent，校验分片输出和单机参考一致。GitHub Actions 使用 Python 3.12，并额外执行 Compose 配置校验、镜像构建和容器健康检查。

## 后续集成

- 接入预训练模型推理后端，在真实设备间验证模型加载、tensor 传输及输出。
- 为云端部署配置 SSH 身份、目标服务器、域名/TLS 和允许的访问范围。
- 后续增加模型元数据、非均匀内存估算、可靠命令队列、取消确认、自动恢复和调度性能优化。

## 多端节点客户端规划

macOS、Windows 与 Android 原生客户端的产品目标、MVP 范围、验收标准及 issue 拆分见 [`docs/CLIENT_AGENT_TASKS.md`](docs/CLIENT_AGENT_TASKS.md)。

### macOS 原生客户端（开发中）

新增 SwiftUI 客户端首版，可注册并恢复节点身份、将节点凭据保存在 macOS 钥匙串、发心跳、暂停接单、轮询 assignment，并执行内置 `lastcore/linear-demo-v1` CPU 演示任务。它不支持预训练模型，也还没有菜单栏常驻、签名安装包或升级流程。

需要 macOS 13+ 和 Xcode 命令行工具。在仓库根目录运行：

```bash
cd macos-client
swift run
```

首次连接时填写 Controller 地址、节点名称、本机可达地址和注册凭据。注册成功后注册凭据会清空，节点凭据写入钥匙串。接入其他设备前，`endpoint` 必须填写其他节点可访问的地址，不能使用 `127.0.0.1`。
