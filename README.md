# LastCore

将日常设备组织为分布式 AI 推理网络。仓库当前实现云端调度服务器 MVP，目录为 `controller-server/`。

## 已实现

- Python + FastAPI HTTP API，SQLite 持久化节点、任务、分层分配及状态。
- 节点注册、独立节点凭据、心跳、在线/忙碌/排空/离线状态。
- 按可用内存及计算权重分配连续模型层，返回上下游节点地址。
- 节点轮询任务、回报执行状态；取消、超时和节点离线导致任务终止。
- SQLite 写事务防止并发任务重复预留节点；重启保留任务与预留状态。
- 非 root Docker 镜像、Compose 数据卷和健康检查，以及自动化测试。

## 实现状态与核心文件

当前仓库的可运行交付是 **Controller 控制平面 MVP**。节点、任务、心跳、分层分配和状态回报由 HTTP API 管理；任务进入 `completed` 只表示所有分配节点都已上报完成，不包含模型生成内容或 logits。模型标识目前只是任务字段，Controller 不下载、加载或运行模型。

| 文件 | 职责 | 状态 |
| --- | --- | --- |
| `controller-server/app/main.py` | FastAPI 应用、配置校验、Bearer 鉴权和 HTTP 路由 | 已实现 |
| `controller-server/app/models.py` | 请求/回报数据模型及输入限制 | 已实现 |
| `controller-server/app/scheduler.py` | 按权重、层数和内存上限分配连续层区间 | 已实现 |
| `controller-server/app/store.py` | SQLite 持久化、凭据摘要、心跳超时、任务和分配状态 | 已实现 |
| `controller-server/tests/test_controller.py` | Controller API、调度、并发、终态和重启行为测试 | 有自动化测试；不代表真实设备或推理已验证 |
| `controller-server/Dockerfile`, `controller-server/compose.yaml` | 单实例容器运行、健康检查与数据卷 | 已配置；云端部署未由本仓库材料证明 |

**尚无实现证据的部分**：Node Agent、模型加载/分片、节点间张量传输、真实推理输出、prefill/decode KV 交接、NAT 穿透、离线自动迁移、跨平台真机验证及云端部署。开源项目 exo 是参考材料，不代表 LastCore 已集成其代码或能力。当前内存预算按层均匀估算；提交方应预留权重、激活、KV cache 等开销。计算权重由节点注册时上报，尚无自动性能测量。更多实现边界见 [AI_USAGE.md](AI_USAGE.md) 与 [ai-logs/](ai-logs/)。

## 本地运行

本地运行要求 Python 3.12 或更高版本。Docker 镜像固定为 `python:3.12-slim`；Python 依赖使用仓库中的版本固定值（`fastapi==0.135.2`、`uvicorn==0.42.0`；开发依赖另含 `httpx==0.28.1`、`pytest==9.0.2`）：

```bash
cd controller-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

分别生成两个随机 token，保存到 shell 环境；不要提交凭据：

```bash
export CONTROLLER_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export CONTROLLER_REGISTRATION_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

另一个终端访问 `http://127.0.0.1:8000/healthz`，浏览器访问 `http://127.0.0.1:8000/docs`。接口文档的 Authorize 输入框填写所需 Bearer token。默认数据库为 `controller-server/data/controller.sqlite3`；可用 `CONTROLLER_DATABASE_PATH` 覆盖。缺少 token、token 少于 32 字符或两者相同，服务会拒绝启动。

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

提交任务示例（model_id 仅为标识，不会自动下载模型）：

```json
{
  "model_id": "Qwen/example",
  "num_layers": 32,
  "layer_memory_mb": 512,
  "max_nodes": 8,
  "timeout_seconds": 600
}
```

每个分配含 `id`、`task_id`、`node_id`、`layer_start`、`layer_end`、`previous_node`、`next_node`。层区间统一使用 **左闭右开、从 0 开始**：`[0, 10)` 表示第 0 至第 9 层。下游与上游对象包含节点 ID、endpoint 和该节点的 assignment ID；流水线第一段的上游和最后一段的下游为 null。

调度优先考虑算力高的在线空闲节点，并按算力权重分配层数，受每个节点内存上限约束；若算力优先的组合装不下模型，则选择容量最大的节点组合。所有层必须完整覆盖且不重叠，否则返回 HTTP 409，不创建任务。一个节点同时只能属于一个活动任务，提前完成自己的片段后仍为整个流水线保留。

## Node Agent 对接约定

1. 使用注册 token 入网并保存返回的凭据。持续发心跳，并建议每秒轮询 assignments。
2. 根据 assignment ID 去重；相同任务的重复轮询不得启动第二次计算。启动前检查 `task_status` 为 queued/running、当前时间早于 `deadline`，且上游/下游可达。
3. 根据层区间加载模型片段；上报 `{"status":"running"}`，完成整次推理中本片段的工作后上报 `{"status":"completed"}`。错误上报 `{"status":"failed","error":"原因"}`。
4. 持续检查任务状态。收到 cancelled/failed、到达 deadline 或无法续约心跳达到 TTL 时，停止本地执行并释放资源。先处理旧任务的终止，再启动新的任务，避免取消后仍继续占用资源。节点自身应在本地禁止并行执行两个任务，并对 task/assignment ID 隔离 tensor 数据。

**Controller 无法强制终止远端进程。** 当前取消或失败后会立即释放调度预留，实际计算停止依赖上述 Agent 协议。首版尚无强制 fencing、节点取消确认、命令保留游标和离线恢复，因此不适用于不遵守协议的节点。返回的最近 100 条记录用于原型接入；大规模使用需增加可靠命令队列。

任务 queued → running → completed。任一分配失败、任一已分配节点心跳过期或任务到期，整个任务变为 failed，其余未完成分配也变为 failed；管理员可将活动任务变为 cancelled。相同状态的回报可重试，终态不接受新的状态。超时清理在 API 访问数据库时执行；没有请求时不运行后台扫描。节点回归不会恢复已失败的任务，需要创建新任务，暂不自动重试以避免重复计算。

## 验证

```bash
cd controller-server
source .venv/bin/activate
pytest -q
```

测试覆盖两个节点的连续分层、算力权重及内存限制、鉴权和节点隔离、回报幂等、并发预留、断线/超时/取消/执行失败、数据库重启持久化和输入校验。GitHub Actions 使用 Python 3.12，并额外执行 Compose 配置校验、镜像构建和容器健康检查。测试使用模拟节点状态，不代表真实分布式推理已验证。

## 后续集成

- 接入真实 Node Agent 和推理引擎，在两个节点验证模型加载、tensor 传输及输出。
- 为云端部署配置 SSH 身份、目标服务器、域名/TLS 和允许的访问范围。
- 后续增加模型元数据、非均匀内存估算、可靠命令队列、取消确认、结果存储、自动恢复和调度性能优化。
