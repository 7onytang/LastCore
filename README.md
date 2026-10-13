# LastCore

将日常设备组织为分布式 AI 推理网络。仓库当前包含云端调度服务器（`controller-server/`）与节点侧程序（`node-agent/`）。

## 已实现

- Python + FastAPI HTTP API，SQLite 持久化节点、任务、分层分配及状态。
- 节点注册、独立节点凭据、心跳、在线/忙碌/排空/离线状态。
- 按可用内存及计算权重分配连续模型层，返回上下游节点地址。
- 节点轮询任务、回报执行状态；取消、超时和节点离线导致任务终止。
- SQLite 写事务防止并发任务重复预留节点；重启保留任务与预留状态。
- 非 root Docker 镜像、Compose 数据卷和健康检查，以及自动化测试。
- Node Agent：凭据持久化、心跳、任务轮询/去重、running/completed/failed 上报、取消/超时/心跳断链时的本地停止，执行通过 `Executor` 接口委托推理引擎（对接协议见 `node-agent/PROTOCOL.md`）。
- 推理引擎与通信层原型（`inference-engine/` + `communication/`）：把自研 tiny decoder-only transformer 按层切成连续片段，每个节点只加载并执行本段，activation 沿 `next_node` 逐跳传输，链路结果与单机逐字节一致。

首版是控制平面、节点生命周期管理与一个可验证的 tiny 推理引擎原型：分层切分与节点间 tensor（activation）传输已能跑通，并与单机结果逐字节一致。尚未实现真实大模型（如 Qwen）的加载/分片与权重映射、模型下载/缓存、量化/压缩、KV cache 与 batching、NAT 穿透或离线后的自动迁移。任务完成表示所有节点上报完成，Controller 不保存模型输出。当前内存预算按层均匀估算；提交方应预留权重、激活、KV cache 等开销。计算权重由节点注册时上报，尚无自动性能测量。

## 本地运行

Python 3.12 或更高版本：

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

Compose 默认仅绑定云服务器的 `127.0.0.1:8000`。公网节点接入通过服务器上的 HTTPS 反向代理转发到该端口。无需购买域名时，可使用 [IP HTTPS 部署说明](ops/public-ip-https/README.md) 中固定 Caddy 和 Let’s Encrypt 短期 IP 证书方案；必须验证实际受信任证书和自动续期管理，不能把本地测试或暂存 CA 证书当作上线证明。在受信任的 VPN/内网中，可将 `.env` 的 `CONTROLLER_BIND_ADDRESS` 设置为服务器内网地址；仅在明确需要时绑定 `0.0.0.0`。认证令牌通过 TLS 或受信任的加密隧道传输。

持久化数据在 `controller-data` 数据卷。`docker compose down` 停止服务且保留数据；`docker compose down -v` 会删除数据，不能用于常规更新。更新使用 `docker compose up -d --build --wait`。首版只部署一个 Controller 实例，数据库放本机文件系统，不放 NFS。备份使用 SQLite backup API 或停服后复制整个数据卷，不能只复制正在写入的 `.sqlite3` 文件。

### 数据库只读诊断

`/healthz` 仅表示进程健康。检查已有 Controller 数据库的物理完整性、外键以及必需表、字段和外键声明时，使用：

```bash
cd controller-server
python -m app.diagnostics --database data/controller.sqlite3 --timeout-seconds 10
# Docker 中使用现有数据卷，不需要管理员或注册 token
docker compose exec -T controller python -m app.diagnostics --database /data/controller.sqlite3
```

命令输出一份脱敏 JSON，时间显式使用北京时间 `Asia/Shanghai`。退出码 0 表示所列检查通过，1 表示发现完整性、外键或结构问题，2 表示无法打开、文件损坏、锁等待或扫描超时。预算须大于 0 且不超过 300 秒；SQLite 锁等待最多为预算与 1 秒中的较小值，长扫描用进度回调中断，耗时不是硬实时保证。不会输出节点凭据、任务内容、SQL 错误文本或违规行。

源文件使用 SQLite `mode=ro`，同一读事务保持检查快照一致，不初始化数据库、不执行任务到期清理或修复；活动 WAL 数据库按 SQLite 的正常读锁协议读取，可能需要访问或创建共享内存 `-shm` 文件，不使用可能漏读 WAL 的 `immutable` 模式。路径不存在时拒绝检查，不创建空库。通过诊断仅说明所列结构和当前快照可读，不代表精确 schema 迁移版本、安全审计或真实节点推理验收。若需要确定的备份内容，应对离线备份运行诊断。实现复用 [SQLite 官方 PRAGMA](https://www.sqlite.org/pragma.html) 与 Python 标准库，无额外依赖。

## 在线 SQLite 备份

运行中的数据库使用内置在线备份工具，不需要 Controller token，也不启动 API 服务：

```bash
cd controller-server
mkdir -m 700 -p backups
python -m app.backup --database data/controller.sqlite3 \
  --output backups/controller-20261013T120000+0800.sqlite3
```

为每次备份指定新的文件名，示例时间按北京时间（`Asia/Shanghai`），实际使用时替换。源路径必须是已有数据库，目标父目录必须已存在；目标文件或同名 SQLite sidecar 已存在时拒绝执行。工具使用 Python 标准库 [Connection.backup](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup)，正确读取活动 WAL 中已提交的数据，不包含尚未提交的事务。历史开源 [sqlitebck](https://github.com/husio/python-sqlite3-backup) 已归档并指向标准库实现，因此无需新增依赖。

备份先写到目标目录中的私有临时文件，完成 `integrity_check`、关闭数据库并同步文件后，通过 hard link 原子发布，绝不覆盖已有目标。结果是权限 `0600` 的独立 SQLite 文件，不需要随附 WAL/SHM 文件；失败时清理本次临时文件。源连接采用 `mode=ro`，不会创建路径拼错的数据库或修改业务数据，但读取活动 WAL 仍可能使用或初始化 SQLite 的共享内存 sidecar；不要对活动数据库使用 `immutable=1`。

默认总预算 60 秒、连续锁等待 5 秒，可用 `--timeout` 和 `--lock-timeout` 指定有限正秒数。预算在 SQLite 分步复制和完整性检查期间生效；持续写入导致反复重启复制时也会到期。底层阻塞文件系统 I/O 无法由这些检查硬中断。源及目标均应放在受信任的本地文件系统上，目标目录需要支持同目录 hard link；不支持时失败，不降级为覆盖写入。备份是完成时的一致性快照，不保证固定为命令开始时刻。完整性检查验证 SQLite 结构，不代替业务恢复演练。

对于 Compose 数据卷，可在运行中的非 root Controller 容器内备份，再复制到宿主机：

```bash
cd controller-server
# 需要已构建包含 app.backup 的镜像，并且 Controller 正在运行。
docker compose exec -T controller python -c 'from pathlib import Path; Path("/data/backups").mkdir(mode=0o700, exist_ok=True)'
BACKUP_NAME="$(docker compose exec -T controller python -c 'from datetime import datetime, timedelta, timezone; print("controller-" + datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai")).strftime("%Y%m%dT%H%M%S%z") + ".sqlite3")')"
docker compose exec -T controller python -m app.backup \
  --database /data/controller.sqlite3 --output "/data/backups/$BACKUP_NAME"
# 仅在上一条命令退出码为 0 后执行复制；宿主目录同样使用私有权限。
mkdir -m 700 -p backups
docker cp "$(docker compose ps -q controller):/data/backups/$BACKUP_NAME" "backups/$BACKUP_NAME"
```

容器内备份仍在原数据卷，只能防误操作，不能防数据卷丢失；复制完成后按自己的保留策略保存到独立存储。备份包含节点凭据摘要及任务数据，应限制访问并妥善加密。恢复前停止 Controller，把备份恢复到干净的数据目录，避免混入旧 `-wal`/`-shm`/`-journal` 文件，再启动并验证节点和任务状态；此工具不执行恢复或删除旧备份。

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

任务 queued → running → completed。任一分配失败、任一已分配节点心跳过期或任务到期，整个任务变为 failed，其余未完成分配也变为 failed；管理员可将活动任务变为 cancelled。相同状态的回报可重试，终态不接受新的状态。服务启动时清理已过期任务，运行期间默认每秒后台扫描，即使没有 API 请求也会持久化终态并释放预留；API 访问数据库时仍会清理。`CONTROLLER_EXPIRY_INTERVAL_SECONDS` 可设为 0.1 至 60 秒，间隔为两次扫描之间的等待时间，并非实时保证；数据库锁等待及扫描耗时可能延迟清理。后台 SQLite 错误会记录日志并在下个间隔重试，启动清理失败则拒绝启动；关闭服务时等待当前扫描完成，避免遗留数据库写入。节点回归不会恢复已失败的任务，需要创建新任务，暂不自动重试以避免重复计算。后台清理仍不能强制终止远端计算。

## 验证

```bash
cd controller-server
source .venv/bin/activate
pytest -q
python scripts/smoke_idle_expiry.py
```

Controller 测试覆盖连续分层、算力与内存限制、鉴权、节点隔离、回报幂等、并发预留、断线/超时/取消/失败、重启持久化及输入校验。后台扫描测试直接读取 SQLite，验证无请求时 deadline/TTL 清理、启动失败、重试及正常停机；HTTP smoke 使用临时数据库和随机本机端口启动真实 Uvicorn，注册并建任务后停止 HTTP 请求，检查 TTL 终态落盘和资源清理。GitHub Actions 还执行 Compose 校验、镜像构建和容器健康检查。

Controller 测试中的节点状态为模拟数据。`inference-engine/` 的验证覆盖两个或三个独立进程的 tiny 模型链路与单机结果逐字节一致；这与控制平面验证分开，也不代表生产规模模型或物理设备矩阵已验收。

SQLite 初始化和每次事务在退出时显式关闭连接，包括初始化、事务准备和请求执行失败的路径；事务提交、请求失败回滚及已提交的过期清理语义保持一致。实现复用 Python 标准库 [`contextlib.closing`](https://github.com/python/cpython/blob/main/Doc/library/sqlite3.rst)，无需新增依赖。连接生命周期回归测试保留真实连接引用，验证退出后连接已经关闭，并确认数据提交、回滚和过期清理的持久化结果：

```bash
pytest -q tests/test_store_lifecycle.py
```

备份回归覆盖活动 WAL、未提交事务隔离、完整性失败、有限锁等待、不覆盖已有目标，以及恢复真实 Store 后的原节点凭据、任务终态和活动预留。恢复测试注入受控时间以避免测试数据自然过期；生产恢复仍使用真实时间，并按原 TTL/deadline 清理过期任务。CI 还检查数据卷在线备份、独立副本完整性、拒覆盖及容器内诊断，最后删除本次临时容器和数据卷。

## 后续集成

- 接入真实大模型：替换 `inference-engine/model.py` 的算子与权重映射，在两节点验证真实模型的加载、分片、tensor 传输与输出（协议见 `node-agent/PROTOCOL.md`）。
- 按 [IP HTTPS 部署说明](ops/public-ip-https/README.md) 验证实际公开证书、续期和节点接入；保留隔离部署及数据回滚边界。
- 后续增加模型元数据、非均匀内存估算、可靠命令队列、取消确认、结果存储、自动恢复和调度性能优化。

## Isolated real local model runtime

`real-model-runtime/` provides a pinned llama.cpp CPU completion adapter with a verified SmolLM2-135M-Instruct GGUF. See [setup, run and verification instructions](real-model-runtime/README.md). This local prompt-to-text smoke path is independent of the existing Controller and inference protocols; it does not establish distributed model execution or Android/cloud deployment.

## Integrated candidate verification and staging

`integration/run_two_node_local.sh` starts a real local Controller and two real tiny stage agents, compares the distributed logits with a single-machine reference, waits for both completed reports, and verifies that its processes and ports are cleaned up. Set `CONTROLLER_PY`, `NODE_PY`, and `ENGINE_PY` to separate Python environments installed from the respective modules' requirements. `LC11_RUN_DIR` selects a private local evidence directory. It generates tiny weights; this is not pretrained multi-device LLM inference.

```sh
CONTROLLER_PY=/path/to/controller/bin/python \
NODE_PY=/path/to/node/bin/python ENGINE_PY=/path/to/engine/bin/python \
LC11_RUN_DIR=/private/tmp/lastcore-integrated-evidence bash integration/run_two_node_local.sh
/path/to/controller/bin/python integration/verify_backup_recovery.py \
  --run-dir /private/tmp/lastcore-integrated-evidence
```

The recovery command backs up that completed run, starts a real Controller from a new restored copy, and checks that both persisted node credentials are accepted. Keep the evidence directory private because it includes local test credentials. GitHub Actions performs the integrated tiny and recovery checks and builds the isolated Linux staging image; a local pass does not claim the remote CI has run.

For macOS bundle installation, preflight and bounded real bundle verification, see [desktop instructions](node-agent/desktop/README.md). For fixed source packaging, isolated loopback-only Linux staging, runtime acceptance and rollback, see [staging instructions](ops/staging/README.md). Public IP HTTPS and Android physical-device acceptance remain separate release gates; no domain purchase is required by these preparation files.

The optional [real whole-model jobs API](controller-server/REAL_JOBS.md) connects a selected [macOS CLI worker](real-model-runtime/README.md) to the fixed local GGUF adapter. It stores bounded generated text/results under a separate `/v1/inference` namespace and defaults to disabled. Its local acceptance uses an actual Controller and native model backend, covering generated text, idempotency, backend timeout, cancellation acknowledgment and cleanup. A whole-model worker runs the complete model on one device; it does not implement multi-device pretrained layer distribution. Existing `/v1/tasks` behavior and tiny protocols remain independent. Enable/deploy the optional API only after fixed-source review and separately verify the actual public-cloud loop.

## Planning and reference records

[`AI_USAGE.md`](AI_USAGE.md) and [`ai-logs/`](ai-logs/) describe the source and review boundaries for project reference material. [`docs/CLIENT_AGENT_TASKS.md`](docs/CLIENT_AGENT_TASKS.md) is a product and engineering draft for future macOS, Windows, and Android node clients; the terminal prototype is not a production client, and the draft does not claim those clients are implemented. [`lc10-cost-supplement/`](lc10-cost-supplement/) contains an illustrative, source-bound budget example; it is not wired into Controller scheduling or resource reservation.

## License

LastCore's own source is licensed under [MIT](LICENSE), using the [OSI MIT template](https://opensource.org/license/mit). Third-party software and model weights retain their own terms. The real model/backend license texts and manifest are in `real-model-runtime/licenses/`; desktop packaging collects and verifies the frozen runtime's license materials. The top-level license does not replace those third-party notices.
