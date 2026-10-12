# 双节点系统验证与故障回归矩阵

范围：LC-11 的整系统启动/清理、故障矩阵、结果采集与指标测量。被测组合为
**真实 Controller（controller-server）+ 两个真实 Node Agent 进程（node-agent，
`--executor stage`）+ inference-engine StageServer/TCP v1**，本机 macOS arm64
单机多进程（两个 Agent 是真正的两个独立进程，但不是两台物理设备）。

复现命令（仓库根目录）：

```bash
python -m integration.run_scenarios \
  --controller-python .venv-controller/bin/python \
  --node-python .venv-node/bin/python \
  --workdir integration/runs --report integration/runs/report.json
```

实测环境与版本：macOS 15.8.1 arm64，Python 3.14.7，numpy 2.5.4；被测代码为
本分支 `agent/engineer-b-b/a47b8821123f`（见交付评论中的固定 SHA）。

## 一、结果矩阵

| # | 场景 | 注入方式 | 预期行为 | 实测结果 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | 正常生成（单请求，`[0,3)+[3,4)`） | 无 | 任务 completed；logits 与单机逐字节一致 | `bit_identical=true`，`task=completed`，端到端 0.260s，客户端跳 457B↑/1546B↓ | 通过 |
| 2 | 多步生成（同任务 3 次请求） | 无（`complete-after=0` 持续服务） | 3 次都一致，服务计数 3 | 3/3 `bit_identical=true`，Node A/B 各服务 3 次，`local_seconds` 0.0005–0.0014s | 通过 |
| 3 | 取消 | 请求后 `POST /v1/tasks/{id}/cancel` | 任务 cancelled；两个 Agent 本地停止 | `cancel_http_status=200`，`task=cancelled`，日志 `stopped locally`（`shutdown` / `task-terminal`） | 通过 |
| 4 | 超时（deadline） | `timeout_seconds=10` 且不发推理请求 | 任务 failed（`task deadline exceeded`）；Agent 到点停止本地执行 | `task=failed`，`error=task deadline exceeded`，两个 Agent 均 `stopped locally (deadline)` | 通过 |
| 5 | 节点断网/离线 | SIGTERM 杀掉末段节点（Node B） | 客户端请求失败；Controller 在心跳 TTL 后把任务置 failed | 客户端 `PeerError: cannot connect to 127.0.0.1:56449: Connection refused`；任务 `failed`，`error=assigned node heartbeat expired` | 通过 |
| 6 | Controller 重启 | SIGTERM 重启 uvicorn（同一 SQLite 文件） | 节点凭据复用、身份不变；重启后新任务照常完成 | `nodes_before == nodes_after`（同一对 node id）；重启后新任务 completed 且逐字节一致 | 通过 |
| 7 | 鉴权失败 | 错误 admin / registration / node token | 全部 401 | `bad_admin_create_task=401`，`bad_registration_token=401`，`bad_node_token_heartbeat=401` | 通过 |
| 8 | 模型不符 | 客户端按 `wrong/model` 连接首段 | 首段拒绝，客户端报错 | `PeerError: upstream runs model 'wrong/model', local stage runs 'lastcore/tiny-cpu'` | 通过 |
| 9 | 坏 tensor | 篡改 payload 尾字节 / 截断 payload / 伪造 shape | 解码前拒绝 | 三种畸形帧均为 `ProtocolError: tensor checksum mismatch` / `declared N bytes but received M` / `shape ... does not match` | 通过 |
| 10 | 资源回收 | 每个场景结束 | 进程退出、监听端口释放 | 7/7 场景 `ports_released=true`；峰值 RSS 见下 | 通过 |

资源占用（各场景进程峰值 RSS，轮询 `ps` 取最大）：Controller ≈ 52–53 MB，
每个 Node Agent ≈ 42–43 MB。传输字节（客户端→首段一跳，双向）：
首请求 457B↑/1546B↓，后续请求 423–439B↑/1289–1802B↓。

## 二、被测组件已覆盖、本矩阵未重复的场景

| 场景 | 覆盖位置 | 说明 |
| --- | --- | --- |
| 重复/迟到消息（assignment 去重、重复终态上报幂等） | `node-agent/tests`（37 项，含 `test_agent.py` 去重与 journal）、`controller-server/tests`（16 项，含回报幂等与并发预留） | 单进程/进程内已验证；跨进程重复轮询的端到端注入本轮未单独做 |
| 坏 tensor 的更多边界（版本号、dtype、超大 header、分片读） | `communication/tests`（38 项） | 本矩阵第 9 项只覆盖校验和/长度/shape 三类 |
| 链路取消帧传播、静默对端、读超时 | `inference-engine/tests`（84 项） | 协议级证据，不属本矩阵系统级场景 |
| 双进程/三进程逐字节一致 | `inference-engine/examples/two_node_local.py`（本轮实测两段与三段均 `bit-identical: True`） | 不经过 Controller 的数据面基线 |

## 三、明确未验证

| 项 | 原因 |
| --- | --- |
| 两台**物理设备**（真实网络、真实 NAT、跨机时钟） | 本机无第二台设备可支配；本轮只做到同机两个独立 Agent 进程 |
| 三平台（macOS / Windows / Android） | 只有 macOS arm64；矩阵未在 Windows/Android 上执行 |
| 真实预训练模型（如 Qwen） | 只有自研 tiny decoder-only（4 层，155392B 参数）；本报告的“一致”仅指该 tiny 模型 |
| decode 阶段 TTFT / tokens/s、KV cache、流式返回 | 当前协议一次请求=一次 prefill 前传，无逐 token 回传；属 LC-8/LC-10 后续 |
| A→B 一跳真实传输字节 | 节点注册 endpoint 即 `StageServer` 绑定地址，无法插入 relay；需 communication 暴露字节计数（LC-5 提案） |
| 断网后的自动恢复/迁移 | Controller 只把任务置 failed，不自动重试（现状语义，非本任务范围） |

## 四、整系统联调中发现的缺陷（本轮已修，待 Reviewer-A 复审）

**现象**：真实 Controller + 真实 Agent 组合下，两个 Agent 的 assignment 全部
`failed`，客户端拿不到结果；Node Agent 日志：

```
ShardError: assignment endpoint 'http://127.0.0.1:54894/' is not a host:port URL
```

**根因**：Controller 的注册接口把 endpoint 声明为 `HttpUrl`，落库/回传时用
`str()`，URL 被规范化成带尾斜杠的 `http://host:port/`；而
`inference_engine.parse_endpoint` 只接受 `host:port`（`port.isdigit()` 对
`"54894/"` 为假）。此前用 FakeController 的集成测试传的是不带尾斜杠的字符串，
所以没有暴露。

**处置**：在本任务所属的 inference-engine 内做最小兼容：`parse_endpoint` 先
`strip()` 再去尾斜杠（`inference_engine/assignment.py`），并新增回归用例
`test_controller_normalised_endpoint_url_is_accepted`（含裸 `host:port`、
`scheme://host:port`、`http://host:port/` 三种写法，仍拒绝无端口与纯路径）。

**待决**：契约层面仍需 LC-5 明确 Controller 回传 endpoint 的规范形式（建议
协议字段只承载 `host:port`，HTTP 注册再单独校验可访问 URL）；本次修复对两种
写法都兼容，不改变数据面帧格式，但该取舍应由契约负责人确认。

## 五、本轮实际执行的命令与结果

| 命令 | 结果 |
| --- | --- |
| `communication`: `pytest -q` | 38 passed |
| `controller-server`: `pytest -q` | 16 passed |
| `inference-engine`: `pytest -q` | 84 passed |
| `node-agent`: `PYTHONPATH=../inference-engine:../communication pytest -q` | 37 passed |
| `python examples/two_node_local.py` | 两段 `bit-identical: True` |
| `python examples/two_node_local.py --layers 0:1,1:3,3:4 --prompt "5 6 7"` | 三段 `bit-identical: True` |
| `python -m integration.run_scenarios ...` | 7/7 场景通过（`report.json` 随交付附件） |
