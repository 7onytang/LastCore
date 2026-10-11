# LC-8 输入 → 执行 → 结果 闭环差距清单（准备阶段）

角色：Engineer-B（DeepSeek-V4.1-Flash）｜业务时间 Asia/Shanghai
基线（只读复用，不修改）：
- 控制平面 PR #1 `ee57405f925a6aba44a9b3a9402599cf55b04d61`（Reviewer-A 已通过）
- inference-engine / communication：Engineer-B 固定分支 `agent/engineer-b-b/44d462377632` @ `fd6a9cf`（含 PR #2 `dcd189e`；`fd6a9cf` 为新提交，PR #2 审查针对 `dcd189e`）
- node-agent：Engineer-A 分支 `agent/engineer-a-a/b33d0fac979a` @ `672b4f7`
- 参考项目 exo 结论复用 LC-1 公附 `exo-architecture-review-v2.md`（不再重复检索）

本文只做**对照与差距**，不代表接口已冻结。LC-5 尚未交付获批契约，故本清单中的“建议方向”一律为**提案**，不实施。

## 1. 现状链路（事实 + 证据）

```
提交方(prompt/token)
   └─(in-process, 无 HTTP 入口)─> PipelineClient ──> 首段 StageServer
                                                        │ hidden
                                                        ▼
                                                    末段 StageServer ──> logits 原路回传
节点侧：node-agent 轮询 assignment ──> Executor.run(ExecutionContext)
                                        └─ StageExecutor ──> load_assigned_stage + StageServer.serve_forever
上报：node-agent → POST /v1/nodes/{id}/assignments/{aid}/report {status,error}
读取：GET /v1/tasks/{id} → task{status,error,request} + assignments[{status,error}]
```

关键事实（均带 file:line）：

| # | 事实 | 证据 |
| --- | --- | --- |
| F1 | Controller 任务请求只有模型标识与规模，**无 prompt / seed / 生成参数 / session** | `controller-server/app/models.py:23-28`（`TaskRequest`） |
| F2 | 无“提交输入”的 HTTP 入口；PROTOCOL 提案说输入直接 POST 到首节点，但该端点未实现 | `node-agent/PROTOCOL.md:48-53`；实际入口是 `PipelineClient.run`（`inference-engine/inference_engine/pipeline.py:187-204`）与 CLI `node run`（`node.py:124-130`） |
| F3 | 首段做 token/position embedding；末段做 final layer norm + unembedding；中间段只跑 block | `inference_engine/stage.py:94-107`、`model.py:31-41`、`model.py:75-77`、`config.py:14-16` |
| F4 | 一段一次 forward 整段序列，输出 logits `[batch, seq, vocab]`；**无 decode 循环、无 KV cache、无 sampling** | `stage.py:94-107`；README「已知限制」`inference-engine/README.md:102` |
| F5 | next_token 由调用方用 argmax 计算，属调用方而非任何模块 | `inference_engine/node.py:57`（`_summary`） |
| F6 | **无 tokenizer**；CLI 直接把 prompt 里的空格分隔数字当 token id | `inference_engine/node.py:44-48`（`_tokens`） |
| F7 | Executor 硬契约：`run(ExecutionContext)`，每 assignment 新实例，阻塞至片段完成，取消≤1s 响应，deadline 自行保证 | `node-agent/PROTOCOL.md:27-40`；`lastcore_node_agent/executor.py:16-40` |
| F8 | `StageExecutor` = Executor 契约到引擎的适配层 | `lastcore_node_agent/inference_adapter.py:19-96` |
| F9 | assignment → 本地段的映射（层区间/上下游 endpoint/deadline）在引擎侧 | `inference_engine/assignment.py:62-108` |
| F10 | assignment 视图由 store 组装（含 previous/next node、task_status、request、deadline） | `controller-server/app/store.py:123-141`、`169-182` |
| F11 | 终态上报只有 status/error，**无结果载荷** | `controller-server/app/models.py:31-33`（`AssignmentReport`） |
| F12 | 重复终态上报幂等（同 status 直接返回） | `controller-server/app/store.py:192-193`；`tests/test_controller.py:76` |
| F13 | 任务取消后 assignment 终态化，迟到上报 → 409 | `store.py:194-195`、`207-215`；`tests/test_controller.py:113,128` |
| F14 | node 本地已 settle（journal + 内存）的 assignment 不再执行 | `agent.py:182-192`、`281-286` |

## 2. Executor/ExecutionContext ↔ StageServer 对照

`ExecutionContext`（`executor.py:16-33`）由 node-agent 从 assignment 行构造（`agent.py:218-229`），`raw=row`。

| ExecutionContext 字段 | StageExecutor 的实际用法 | StageServer/引擎对应 | 差异 |
| --- | --- | --- | --- |
| `task_id` | 仅经 `raw` 落到 `StageRuntime.task_id` | `_hello().task_id`、`_validate_upstream` 校验 | 一致 |
| `assignment_id` | 仅经 `raw` 落到 `StageRuntime.assignment_id` | `_hello().assignment_id`、tensor 帧字段 | 一致 |
| `model_id` | **未使用**（`load_assigned_stage` 不传 `model_id`） | 改用 `config.model_id` 与 `request.model_id` 比对 | 字段存在但被绕过 |
| `position` | 未使用 | 无 | 引擎不关心 rank |
| `layer_start` / `layer_end` | **未使用**（实际用 `context.raw` 的区间） | `assignment_plan` 取 `raw["layer_start"/"layer_end"]` | 双份真相：若 context 与 raw 不一致，引擎按 raw 执行 |
| `previous_node` / `next_node` | 未直接使用；下游取 `plan.next_endpoint`（来自 raw） | `StageServer.downstream` | 一致（但同样经 raw） |
| `deadline` | **未用**｜`StageServer(deadline=None)` 硬编码 | 引擎支持 `deadline`（epoch 秒）并在到期时抛 `ChannelTimeout`（`pipeline.py:157-162`） | **断链**：引擎的 deadline 机制在 node 路径上是死代码，node 侧另有 `_watch_active` 取消（`agent.py:171-172`） |
| `cancel`（`threading.Event`） | 传入 `StageServer.cancel` + `serve_forever(should_stop=...)` | `listener.accept` / `_recv_exactly` / `_forward` 均轮询取消 | 基本一致 |
| `raw` | 必需；为 None 直接 `ShardError` | 供 `load_assigned_stage` | 引擎侧事实来源 |

补充：`StageServer.handle` 在 `runtime.execute(batch)` 期间**不检查 cancel**（`pipeline.py:92-108`）；取消只在“等待接受/收发”边界生效。tiny 模型毫秒级无影响，真实模型长时间 forward 时不满足 node PROTOCOL §1.2.3 的“≤1s 响应取消”。

## 3. 归属结论（第一段 embedding / 末段 norm+head / sampling / tokenizer）

| 项 | 归属（现状事实） | 状态 |
| --- | --- | --- |
| 第一段 embedding（token+position） | inference-engine 首段（`layer_start==0`），`stage.py:95-98` | **明确** |
| 末段 final layer norm + unembedding(head) | inference-engine 末段（`layer_end==n_layers`），`stage.py:107`、`model.py:75-77` | **明确** |
| sampling（temperature/top-k/seed/停止条件） | **无人实现**；仅调用方 argmax 求 next_token（`node.py:57`） | **缺失** |
| tokenizer（文本↔token id） | **无人实现**；CLI 以空格分隔整数代填（`node.py:44-48`） | **缺失** |

## 4. 差距清单（按输入 / 执行 / 结果三段）

编号 `G-xx`；“依赖”指解除所需前置。

### 输入段

| ID | 差距 | 影响 | 建议方向（提案，未实施） | 依赖 |
| --- | --- | --- | --- | --- |
| G-01 | 任务请求无输入载荷：`TaskRequest` 无 prompt/token、seed、生成参数（F1） | “可提交输入”不成立；无法由 Controller 驱动一次真实生成 | 契约层新增输入字段或独立提交端点；LC-5 冻结后再实现 | LC-5 |
| G-02 | 无输入 HTTP 入口（F2）：输入只能由进程内客户端/CLI 送入 | 无持久、可关联 task/session 的提交面；节点地址不能直接当业务入口（缺鉴权） | 首节点 peer 入口或 Controller 转发，二选一并定鉴权 | LC-5 + 安全 |
| G-03 | `ExecutionContext.model_id`、`layer_start/end` 被 `raw` 绕过（§2） | 两处事实可能不一致；诊断困难 | 单一真相：要么从 context 构造 plan，要么断言二者相等并 fail-fast | 本模块 + A 协商 |
| G-04 | `ExecutionContext.deadline` 未接入引擎（`inference_adapter.py:48-49` 硬编码 `None`） | 引擎 deadline 路径在 node 链路未实测；两套超时并存 | 传 `plan.deadline` 或 `context.deadline`（同基准 epoch 秒），并补回归 | 本模块 + A |
| G-05 | 无 tokenizer（F6） | 无法接受文本 prompt，无法产出文本结果 | 独立 tokenizer 归属（引擎或独立模块） | LC-5 |
| G-06 | 无 seed/生成参数透传 | “同 seed 可复现生成”不可达 | 随 G-01 进入契约 | LC-5 |

### 执行段

| ID | 差距 | 影响 | 建议方向（提案，未实施） | 依赖 |
| --- | --- | --- | --- | --- |
| G-07 | 无多步 decode（F4/F5）：只有整段单次 forward | 不能生成文本；KV 隔离/逐 token 对照无处落地 | 在引擎侧新增 decode 循环（MVP 可整段重发，无需 KV） | LC-5 |
| G-08 | 无 KV cache（F4） | 多步生成 O(n²) 重算；KV 隔离不可验证 | MVP 先整段重发，KV 作为后续；契约预留 | LC-5 |
| G-09 | sampling 无归属（§3） | 无法复现 token 序列；无法对照单机 token | 明确归属与参数集（argmax/temperature/top-k/seed） | LC-5 |
| G-10 | `handle` 计算期不检查 cancel（`pipeline.py:92-108`） | 真实模型长 forward 时取消不及时，可能产生迟到结果 | 在层循环内检查 cancel 或按时间片让出 | 本模块 |
| G-11 | `complete_after` 语义：首/中段“服务 N 次”即 completed（`inference_adapter.py:30,60-92`） | “片段服务完成”≠“本次生成完成”，Controller 无法判断链路整体完成 | 与终态/结果契约一起定义（链式完成信号） | LC-5 / LC-7 |

### 结果段

| ID | 差距 | 影响 | 建议方向（提案，未实施） | 依赖 |
| --- | --- | --- | --- | --- |
| G-12 | 结果无读取面（F11）：report/GET task 只有 status/error，无文本/token/logits | **“完成仅在结果可读取后成立”当前不成立** | 定义结果出口（token/文本 + 关联 task/assignment）；见 `controller_terminal_checks.py` 的 `result_payload_fields` | LC-5 |
| G-13 | 无 task/session 关联的结果存储 | 无法把一次生成归到某个会话/任务 | 结果存储与关联键（task_id 已有；session 待定） | LC-5 |
| G-14 | 输入级幂等缺失：`create_task` 无幂等键，重发即新任务 | “重复结果幂等”只在 assignment-report 层成立（F12） | 任务级幂等键或请求去重 | LC-5 |
| G-15 | 取消与终态竞争：节点若在察觉取消前完成，会上报 completed→409，本地 `_settled=completed` 而 Controller=`cancelled` | 两端状态分歧；无取消确认/对账 | 取消确认（ack）或终态对账；LC-7 范围 | LC-7 |
| G-16 | 迟到结果：Controller 侧已被 409 拒绝（F13，本次实测复核） | 结果层当前无“迟到成功”可观测（因结果本身不存在） | 结果出口落地后需补“取消后不含迟到结果”的端到端用例 | LC-5/LC-11 |

## 5. 本次实测复核（不是新功能）

运行环境：Python 3.14.7 / macOS arm64；venv 见本任务目录（未提交）。
- 既有回归基线：inference-engine `87 passed`、communication `40 passed`、node-agent `36 passed`、controller `16 passed`（命令见 `expectations.md`）。
- `input_result_loop_harness.py`：3 步贪心 decode，逐步 distributed logits 与单机参考**逐字节一致**（sha256 相同），重复运行 token/摘要一致，取消后 executor 以 `ExecutionCancelled(reason=task-terminal)` 结束、端口释放、后续请求失败（无迟到成功）。见 `results-loop.json`。
- `controller_terminal_checks.py`：completed 上报重复幂等（200）、取消后迟到 completed 被 409 拒绝、任务读回仍 `cancelled`。见 `results-controller.json`。

## 6. 明确未验证 / 未完成

- 未接入 Controller 的端到端“提交→执行→读取”：本次样例的输入由进程内客户端送入（G-01/G-02），不是 Controller 任务。
- 未实现多步 decode 的 KV 隔离、真实 tokenizer、sampling（G-07/G-08/G-09/G-05）。
- 未做两物理设备、三平台真机验证（LC-4/LC-3 范围）。
- deadline 经 node 路径的端到端行为未实测（G-04）。
- 本清单未冻结任何接口；字段名与出处以被测 SHA 为准。
