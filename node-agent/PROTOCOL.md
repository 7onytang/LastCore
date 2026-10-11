# 执行与 Tensor 协议（提案 v0）

状态：**提案**。由 Engineer-A（node-agent）起草，供 Engineer-B（`inference-engine/`、`communication/`）确认或修订。确认后本文即为两模块对接契约；双方 PR 均以此为基线。

本文分两部分：**§1 执行接口**是 node-agent 已实现并测试的硬契约（`lastcore_node_agent/executor.py`）；**§2 tensor 传输协议**是给 communication 层的提案，B 确认后落到 `communication/` 文档。

## 1. 执行接口（硬契约）

### 1.1 分工

```
controller-server ──HTTP──> node-agent（生命周期编排）
                               │ new Executor() per assignment
                               ▼
                          inference-engine（计算：加载 [layer_start, layer_end)，逐层前向）
                               │ tensor
                               ▼
                          communication（节点间传输，§2）
```

- node-agent 负责：注册/凭据/心跳/轮询/去重/上报/取消与超时编排。**不理解模型与 tensor。**
- inference-engine 负责：模型片段加载、前向计算、输入输出组装。实现 `Executor`。
- communication 负责：节点间 tensor 传输、上游拉取/下游推送、传输层重试。由 `Executor` 内部调用，或作为 `Executor` 的协作对象。

### 1.2 Executor 契约

```python
class Executor:
    name: str
    def run(self, context: ExecutionContext) -> None: ...
```

1. **实例隔离**：每个 assignment 一个新实例（node-agent 通过 `--executor 模块:工厂名` 构造）。禁止跨任务共享实例状态；模型缓存如需跨任务复用，由引擎自行按 `task_id` 隔离。
2. **阻塞语义**：`run()` 阻塞直到本片段完成整次推理工作，正常返回即代表"本片段完成"（node-agent 随后上报 completed）。
3. **取消**：必须至少每 1 秒检查一次 `context.cancel`；置位后尽快（node-agent 宽限期默认 10s）抛 `ExecutionCancelled(context.cancel_reason)` 并释放计算资源（模型分片、中间 tensor、KV cache）。取消原因：`task-terminal`（任务被取消/失败）、`deadline`、`heartbeat-stall`、`shutdown`。
4. **期限**：`run()` 应自行保证在 `context.deadline`（epoch 秒）前停止；node-agent 会在 deadline 后强制请求取消。
5. **异常**：其他任何异常视为片段失败，node-agent 上报 `failed` 并携带异常摘要；引擎应保证异常路径同样释放资源。
6. **上下文只读**：`ExecutionContext` 字段（`task_id`、`assignment_id`、`model_id`、`position`、`layer_start`、`layer_end`、`previous_node`、`next_node`、`deadline`）视为只读；只允许操作 `cancel`。

层区间统一左闭右开、从 0 开始：`[layer_start, layer_end)`，与 Controller 调度一致。

### 1.3 参考实现

`SliceDemoExecutor`（`demo`）按时间片模拟工作并演示取消契约，用于：node-agent 单测、双节点联调中不依赖真实模型的链路验证。B 的引擎接入后配置 `--executor inference_engine.executor:ExecutorFactory`（示例）。

## 2. 节点间 Tensor 传输（提案）

### 2.1 输入与输出入口

Controller 的任务请求只含模型标识与规模（PR #1 `TaskRequest`），不携带输入文本。约定：

- **输入**：任务提交方（管理员脚本/未来的 frontend）在创建任务后，把 prompt 直接 POST 到流水线**第一个节点**的 peer 端点（地址来自该节点注册的 `endpoint`）。
- **输出**：最后一个节点完成 `lm_head` 后，把结果返回给提交方（MVP 阶段：打印日志 + 存本地文件；结果回传通道是后续工作）。

### 2.2 传输方式（MVP）

节点 `endpoint` 上由 communication 层提供 HTTP 服务：

```
POST /v1/peer/{to_assignment_id}/frames
Authorization: Bearer <共享的传输密钥或任务级凭据>   # 待定，见 §2.5
Content-Type: application/octet-stream

<envelope JSON>\n<raw bytes>
```

envelope（单行 JSON，`\n` 结尾，后跟定长 payload）：

```json
{"seq": 1, "task_id": "...", "from_assignment": "...", "type": "hidden", "dtype": "float32", "shape": [1, 8, 512], "payload_length": 16384}
```

- `type: "prompt"`：仅发往第一段，payload 为 UTF-8 文本，`seq=0`。
- `type: "hidden"`：相邻片段间的 hidden states（float32, `[batch, seq, hidden]`），`seq` 从 1 递增。
- 接收方校验 `to_assignment_id`、`task_id` 与本地活动任务一致，否则拒绝（409），防止串任务写 tensor。

### 2.3 流控与重试

- 发送方推送（push）：下游 5xx/连接失败时指数退避重试（0.5s 起，上限 5s），直到成功、任务取消或 deadline。
- MVP 不做独立 ACK（TCP 可靠传输 + 下一个分段到达即隐式确认）；B 如认为需要显式 ACK（`POST /v1/peer/{id}/acks`），在确认时提出。
- 传输协程同样接收 `ExecutionContext.cancel`，取消时停止收发并关闭连接。

### 2.4 验证模型（建议）

- 目标：CPU 上秒级完成的双节点分层推理，对照单机结果。
- 建议 `sshleifer/tiny-gpt2`（~2.6M 参数，PyTorch CPU，pip 可装）；链路打通后再换 `Qwen2.5-0.5B` 量级。
- 分片约定：第一段额外加载 embedding，最后一段额外加载 `lm_head` + LayerNorm；中间段只含连续 decoder 层；KV cache 按片段驻留各自节点，逐 token 流水。

### 2.5 待 B 确认的问题

1. 传输凭据：任务级 token（Controller 签发）还是节点间预共享密钥？PR #1 尚无签发接口，MVP 可先用部署时配置的共享密钥。
2. hidden states 是否需要 per-token 增量推送（流式 KV cache），还是整段一次推送？MVP 建议整段。
3. ACK 与背压需求。
4. dtype/布局是否统一 float32 + 行主序，还是跟随模型原生 dtype？
5. 输出回传：MVP 是否接受"最后节点落盘 + 日志"？
