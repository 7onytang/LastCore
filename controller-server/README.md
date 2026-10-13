# Controller Server

云端调度服务器：管理节点注册/心跳，按内存与算力把模型层分配成流水线，跟踪任务与分配的完整生命周期。对外契约（HTTP API、错误码、状态机）以 PR #1 审查版本 `ee57405` 为固定基线，本目录内的拆分是纯内部重构，不改变行为。

## 模块结构

```
controller-server/
├── app/
│   ├── main.py        # FastAPI 路由与鉴权依赖；ControllerError → HTTP 响应的统一翻译
│   ├── store.py       # 组合根：装配 Repository 与领域服务，保持既有 Store 门面
│   ├── nodes.py       # 节点领域服务：注册、凭据散列/校验、心跳（NodeService）
│   ├── tasks.py       # 任务领域服务：创建（调度调用）、读取、回报、取消（TaskService）
│   ├── repository.py  # SQLite 持久化：schema、事务、行访问、读模型（无 HTTP/调度依赖）
│   ├── scheduler.py   # 纯算法：连续层区间加权分配（无数据库/网络/HTTP 依赖）
│   ├── models.py      # 请求模型（pydantic）
│   └── errors.py      # ControllerError 领域错误与 HTTP 翻译辅助
└── tests/test_controller.py  # 16 项行为基线（固定接口回归）
```

## 文件依赖图

```
main.py ──> store.py ──> nodes.py ───> repository.py
                │              │             ▲
                │              └─────────────┘
                └──> tasks.py ──> repository.py
                       │      └──> nodes.py（认证复用）
                       └──> scheduler.py（纯函数，无 I/O）
models.py <── main.py / nodes.py / tasks.py
errors.py <── main.py / nodes.py
```

- `scheduler.py` 不导入本应用任何模块，仅标准库；分配策略与结果由测试对照固定。
- `repository.py` 仅标准库（sqlite3/json/contextlib/pathlib）；无 fastapi、无调度依赖。
- 领域服务（`nodes.py`、`tasks.py`）通过 `Repository` 的事务句柄操作持久化，不自行开连接。
- `tasks.py` 目前沿用 `fastapi.HTTPException` 表达 404/409（与拆分前 `Store` 行为逐字段一致）；`nodes.py` 使用 `ControllerError`，两者都由 `main.py` 的异常处理器统一翻译为相同 JSON 结构（`{"detail": ...}`）。统一到 `ControllerError` 列为后续清理项，不在本重构内。

## 事务与状态机（行为保持）

- `Repository.transaction()`：`BEGIN IMMEDIATE` + 过期扫描 + 提交后重开事务，保证并发任务创建时节点预留的原子性（16 基线中的并发用例覆盖）。
- 任务状态机 queued → running → completed/failed/cancelled；任一分配 failed、节点心跳过期或到达 deadline 使整个任务 failed；终态不回退。
- 节点 token 只在注册响应出现一次，库内仅存 SHA-256 摘要；认证使用常数时间比较。

## 验证

```bash
cd controller-server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q          # 16 个测试用例（含 5 个输入校验参数化用例）
```

`pytest -q` 覆盖固定接口回归、调度纯函数和 SQLite 行为；CI 的对应入口是
`.github/workflows/controller.yml`，还会校验 Compose 配置并执行容器
`/healthz` smoke test。测试使用模拟节点，不等同于真实多机推理验证。

### 发布口径

- 仓库根 README 的「本地运行」只提供服务启动、`/healthz` 和 `/docs` 的
  smoke test；完整的注册、预留、回报、取消、超时和重启持久化复现命令
  不在本文件或根 README 中维护，不能把该段文字当作已有命令附件。
- `/v1/tasks*` 返回的任务 `status` 为 `queued`、`running`、`completed`、
  `failed` 或 `cancelled`；任务 `error` 是失败/取消原因。节点回报请求的
  `status` 只接受 `running`、`completed`、`failed`，`error` 仅作为失败
  原因回传。HTTP 异常统一为 `{"detail": "..."}`。
- `model_id` 只是任务标识，`layer_memory_mb` 是调度估算值。本次拆分和
  本 README 不声称已实现模型加载、分片传输、真实推理输出、KV cache、
  多机/真机联调或自动迁移；这些边界以仓库根 README 为准。
