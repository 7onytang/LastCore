# LC-8 测试输入与预期（准备阶段）

本文给出三类场景的**固定输入**与**可判定预期**，以及复现命令。用于后续接入获批契约后的回归基线；当前结论均基于基线 SHA（见 `gap-analysis.md` 首段），不代表接口已冻结。

## 0. 环境与固定输入

| 项 | 值 |
| --- | --- |
| 运行时 | Python 3.14.7（macOS arm64 本次实测） |
| 模型 | inference-engine 内置 tiny：`model_id=lastcore/tiny-cpu`、`n_layers=4`、`d_model=32`、`n_heads=4`、`d_ff=64`、`vocab_size=64`、`max_seq_len=16` |
| checkpoint | `generate_weights(tiny_config(), seed=7)`，53 个张量 / 155392 字节，确定性可复现 |
| 切分 | `[0,2)` 首段（embedding + block0-1）；`[2,4)` 末段（block2-3 + final norm + head） |
| prompt（token id） | `[1, 2, 3]`（`seq=3 <= max_seq_len`，均在 `[0,64)`） |
| 生成步数 | 3（贪心，每步把已生成 token 追加后再送整段） |
| task_id | `task-lc8` |

数组契约（`inference-engine/PROTOCOL.md` §2）：首段输入 `tokens int64 [batch,seq]`；段间 `hidden float32 [batch,seq,32]`；末段输出 `logits float32 [batch,seq,64]`，未 softmax。

## 1. 场景一：多步生成 + 单机对照

**输入**：同上；每步重发全序列（无 KV cache 的 MVP 语义）。
**预期**：

| 步 | 送出的 token id | 期望输出 shape | 期望 next_token（argmax，seed=7） |
| --- | --- | --- | --- |
| 1 | `[1,2,3]` | `[1,3,64]` | 39 |
| 2 | `[1,2,3,39]` | `[1,4,64]` | 16 |
| 3 | `[1,2,3,39,16]` | `[1,5,64]` | 5 |
| 生成序列 | — | — | `[39, 16, 5]` |

- 每步 distributed logits 与单机 `forward_full` **逐字节相同**（`np.array_equal`，且 sha256 相同）。三个复现值见 `results-loop.json`（`d05af942…`、`4156d35d…`、`0a3603a9…`）。
- 末段被服务次数 = 2 次运行 × 3 步 = 6（`last_stage_served=6`）。

> 注意：`[39,16,5]` 是**本次 fixed seed=7 的确定性产物**，不是“有意义的文本”；本轮无 tokenizer（G-05），token↔文本映射未实现。

**复现**：

```bash
EXT=<本任务目录>            # 含 venv 与 wt-engine / wt-node / wt-ctrl2 三个隔离 checkout
LC_REPO_ROOT=$(pwd) \
LC_ENGINE_DIR=$EXT/wt-engine/inference-engine \
LC_COMM_DIR=$EXT/wt-engine/communication \
LC_NODE_AGENT_DIR=$EXT/wt-node/node-agent \
  $EXT/venv/bin/python docs/lc8-input-result-loop/input_result_loop_harness.py --out $EXT/results-loop.json
```

## 2. 场景二：结果幂等

**定义**：同一 `model_id` + 同一 checkpoint + 同一输入，重复执行必须得到相同摘要。
**输入**：连续执行两次场景一（同一进程、同一 checkpoint）。
**预期**：
- 两次运行的逐步 `logits_sha256` 列表相同；
- 两次运行的 `generated_tokens` 相同（`[39,16,5]`）；
- `idempotent_repeat=true`（见 `results-loop.json`）。

**Controller 侧幂等（现状事实）**：同一 assignment 的 `completed` 上报重复提交返回 200 且任务仍为 `completed`（`store.py:192-193`）。
**预期命令**：

```bash
LC_REPO_ROOT=$(pwd) LC_CONTROLLER_DIR=$EXT/wt-ctrl2/controller-server \
  $EXT/venv/bin/python docs/lc8-input-result-loop/controller_terminal_checks.py --out $EXT/results-controller.json
```

预期字段：`completed_report_idempotent=true`。
**未覆盖**：任务级幂等（同一请求重发不生成新任务）当前**不成立**（G-14）——重发 `POST /v1/tasks` 一定新建任务；该预期在契约冻结后补。

## 3. 场景三：取消与迟到结果

### 3a. 执行侧（本模块 + node Executor 契约）

**输入**：场景一执行中/后对 `ExecutionContext` 调用 `request_cancel("task-terminal")`。
**预期**：
- `StageExecutor.run` 抛 `ExecutionCancelled`，`reason == "task-terminal"`；
- 监听端口在宽限期内释放；
- 之后对首段的任何请求都不得返回成功（无“迟到成功”）；本次实测以 `ChannelClosed` 失败，`late_success_observed=false`。

### 3b. 控制平面侧（现状事实，已复核）

**输入**：任务 A 上报 `running`→`completed`；任务 B 上报 `running`→`cancel`→（迟到）`completed`。
**预期**：
- 任务 A：`completed` 重复上报 200 幂等；
- 任务 B：`cancel` 后任务=`cancelled`；迟到 `completed` 上报 **409**；`GET /v1/tasks/{B}` 仍为 `cancelled`（终态不回退）。
- 实测字段：`late_completed_status=409`、`late_completed_rejected=true`、`terminal_status_preserved=true`。

### 3c. 节点侧（现状代码路径，未新增用例）

**预期（读码结论）**：node-agent 察觉任务终态后置 `cancel`（`agent.py:177-180`），执行以 `cancelled-local` 结束并**只本地 settle、不上报**（`agent.py:267-271`），因此正常路径不产生迟到 `completed`。若节点在察觉前已完成，上报 `completed` 会收到 409，但本地 `_settled=completed` 与 Controller `cancelled` 会分歧——该对账属 LC-7（G-15），本任务不实现。

## 4. 既有回归基线（复用，非本轮新增）

```bash
cd $EXT/wt-engine/inference-engine && $EXT/venv/bin/python -m pytest -q   # 87 passed
cd $EXT/wt-engine/communication  && $EXT/venv/bin/python -m pytest -q   # 40 passed
cd $EXT/wt-node/node-agent       && $EXT/venv/bin/python -m pytest -q   # 36 passed
cd $EXT/wt-ctrl2/controller-server && $EXT/venv/bin/python -m pytest -q # 16 passed
```

## 5. 与契约的衔接（待 LC-5 冻结后执行）

一旦 LC-5 冻结输入/结果出口（G-01/G-12），本节三类用例应升级为：
1. 由 Controller 提交含 prompt/seed 的任务 → 从结果出口读取 token/文本并关联 task/session；
2. 相同请求的任务级幂等键；
3. 端到端“取消后不产生迟到结果”的用例，覆盖 KV 隔离与多步 decode（当前均未实现）。
