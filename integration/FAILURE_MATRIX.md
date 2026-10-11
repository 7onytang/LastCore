# LC-11 系统集成故障验收矩阵（准备阶段）

基线：`ba52895ae829dcf0c8c605de46103e3b25fad650`（node-agent 已完成 StageServer 执行器对接；四模块同树）。
一条命令：`integration/run_two_node_local.sh`（启动 Controller + 两个 Node Agent，真实 `StageServer` 执行器，
建任务并把 prompt 送进首段节点，与本机单机参考对照，最后只清理自己启动的 PID/端口）。

状态列含义：**已实测**＝本脚本/本仓库真实跑过并留下输出；**模块级已覆盖**＝该场景由模块自身测试覆盖，
但系统级（Controller+双节点）本轮未跑通；**缺输入**＝依赖尚未固定/未实现，不能验收；**阻塞**＝本轮发现缺陷。

| # | 场景 | 状态 | 证据 / 命令 | 缺口或说明 |
| --- | --- | --- | --- | --- |
| 1 | 正常路径：Controller + 两个真实 Node 完成一次分层推理并与单机一致 | **阻塞（D1）** | `bash integration/run_two_node_local.sh`：controller healthy → 两节点注册 → 建任务 → 两节点各自领取 assignment → 执行失败。节点日志：`ShardError: assignment endpoint 'http://127.0.0.1:59250/' is not a host:port URL` | 见 D1。启动/编排/任务下发/assignment 领取均已实测通过，tensor 闭环未到达 |
| 2 | 启动与资源回收（进程/端口） | **已实测** | 同一条命令的 `summary.json`：`still_alive: []`、`ports_still_bound: []`；`rss.txt` 记录各 PID 的 RSS（controller≈1.2 MB，node≈43 MB） | 只清理本脚本记录的 PID，未按名称匹配 |
| 3 | 指标采集（TTFT / tokens/s / 传输字节 / 峰值内存） | 部分（缺输入） | `integration/pipeline_client.py` 输出 `ttft_seconds/total_seconds/tokens_per_second/payload_bytes`；RSS 由脚本用 `ps -o rss=` 采样 | 场景 1 未跑通前没有真实 TTFT/tokens/s；传输字节目前是 hidden/logits 的解析估算，不是线缆计数（communication 未暴露计数器） |
| 4 | 多步生成（逐 token / KV cache） | 缺输入 | — | 引擎当前一次前向返回整段 logits；无 KV cache / 生成循环（LC-8 结果闭环、LC-10 KV 预算未完成） |
| 5 | 重复 / 迟到消息 | 模块级已覆盖 | Controller：`controller-server/tests/test_controller.py`（同状态 report 幂等，终态 409）；node-agent：`tests/test_agent.py`（按 assignment id 去重） | 系统级（真实双节点 + 重复 prompt/迟到帧）未跑；需场景 1 先通 |
| 6 | 取消（管理员 cancel → 节点停止并回收） | 模块级已覆盖 | Controller 取消后 assignment 变 cancelled；引擎 `CANCEL` 帧 → `TaskCancelled`（`inference-engine/tests/test_cancellation_and_timeout.py`）；node-agent 取消停止 | 系统级未跑；LC-7 的"终态窗口/迟到命令"测试尚未固定提交 |
| 7 | 断网 / 心跳过期 | 模块级已覆盖 | node-agent `tests/test_agent.py`（心跳失败计数、TTL 停止）；Controller（心跳过期 → 任务 failed） | 系统级（真杀网络/杀节点）未跑；LC-7 的断网/重启回归未固定 |
| 8 | 超时（deadline） | 模块级已覆盖 | `inference-engine/tests/test_cancellation_and_timeout.py::test_deadline_expiry_stops_the_stage`、`test_serve_forever_stops_instead_of_polling_an_expired_deadline` | 系统级未跑；注意本基线树（`ba52895`）尚未含 deadline 到期停服修复（在 PR #2 的 `fd6a9cf`） |
| 9 | 重启（Controller / 节点重启后恢复） | 模块级已覆盖 | Controller SQLite 持久化与重启测试；node-agent 凭据持久化 + 状态目录锁测试 | 系统的"重启后继续同一任务"语义未定义（PR #1 明确不做自动重试/迁移），需要 LC-5/LC-10 决定 |
| 10 | 坏 tensor（截断/篡改/超长） | 模块级已覆盖 | `communication/tests/test_codec.py`、`test_protocol.py`、`test_channel.py`（sha256 缺失/篡改、长度/形状不符、流式对端下取消） | 系统级注入未跑（可在场景 1 通后加：把首段 endpoint 换成坏帧发送器） |
| 11 | 鉴权失败 | 模块级已覆盖 | Controller：注册/管理/节点三级 token 与 401/404/409 行为（PR #1 审查已复现）；node-agent：`tests/test_controller_client.py` | 场景 1 未跑；节点间传输目前**无鉴权**（LC-5 待冻结，禁止公网暴露） |
| 12 | 真机/两物理设备 | 缺输入 | — | 无第二台设备；本轮只做本机双进程 |
| 13 | 三平台（macOS/Windows/Android） | 缺输入 | — | LC-12/13/14/15 未完成 |

## 本轮发现（按 LC-11 授权范围只报不改）

**D1（阻塞场景 1，跨模块缺陷）**：Controller 返回的节点 endpoint 是标准 URL 形态（pydantic `HttpUrl`
序列化带尾斜杠，如 `http://127.0.0.1:59250/`），而 `inference-engine` 的
`inference_engine/assignment.py::parse_endpoint` 用 `rpartition(":")` 解析，尾斜杠会进入端口字符串：
`port.isdigit()` 为假 → `ShardError`。node-agent 的 `inference_engine_adapter` 直接把 assignment 的原始
endpoint 交给它，于是**任何真实 Controller + 真实节点的链路都会在领取任务后立刻失败**。

- 最小复现（任意 Python，无需网络）：
  `python -c "from inference_engine.assignment import parse_endpoint; parse_endpoint('http://127.0.0.1:59250/')"`
  → `ShardError: assignment endpoint 'http://127.0.0.1:59250/' is not a host:port URL`
- 影响面：PR #2 全量（`1efa841`…`b533929`）与 `ba52895` 的集成树均受影响；模块自身测试都用不带尾斜杠的
  endpoint，所以没有暴露。
- 建议修复（不属于 LC-11 允许范围，需由 inference-engine/引擎负责人按"审前不 push"流程提交）：
  `parse_endpoint` 改为 `urlparse` + `hostname/port`，接受尾斜杠并保持既有拒绝用例（无端口、非数字端口、
  空串仍拒绝），并在 `communication`/`inference-engine` 测试里补一条带尾斜杠的正例。
- 我不修改该业务代码；本轮只提供真实失败证据与最小复现，供 Mika 决定归属与复核。

**D2（观察）**：两个节点 compute_weight/内存相同，Controller 按 node id 兜底排序，首段可能落在任一节点
（本例落在 node-b）。集成脚本因此从任务响应里解析首段 endpoint，不假设节点顺序。

## 依赖输入（逐项标明，未满足即不能系统验收）

| 依赖 | 需要的输入 | 现状 |
| --- | --- | --- |
| LC-7 | 终态窗口/重复与迟到/断网重启/取消的固定回归版本 | 两个测试文件仍未提交，无固定 SHA（A 本轮接续） |
| LC-8 | 输入与生成结果闭环（提交方接口、结果回传/持久化） | 引擎只把 logits 交给调用方；Controller 任务不含输出 |
| LC-9 | model-manager 的 manifest/哈希/安全缓存 | 未合入集成树（本脚本的 tiny 权重仍由 `inference_engine.node generate` 生成） |
| LC-10 | 模型 manifest 驱动的容量估算 + KV/activation 预算 + 快照并发 | Controller 仍收手填 `num_layers/layer_memory_mb`；无 KV 预算 |
| LC-5 | D1–D6 契约（传输形态/鉴权/握手必填字段等） | 草案层面通过，未冻结；节点间无鉴权 |

## 本轮明确不宣称

不是系统验收：没有跑通 tensor 闭环（阻塞于 D1）、没有多步生成/KV、没有断网/重启/坏 tensor/取消的系统级
注入、没有真机与三平台、没有部署与 CI。场景 2 的进程/端口回收与场景 3 的采集脚本是准备阶段产物。
