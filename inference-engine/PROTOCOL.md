# inference-engine —— 执行与 tensor 协议（v1）

本文件是 `inference-engine/` 与 `node-agent/`（Engineer-A）之间的接口契约，也是本模块对外承诺的语义。改动其中任何一条都要提升协议版本号并在两个模块的 PR 中同步说明。

## 1. 模型与层编号

- 模型是 CPU 上可验证的 tiny decoder-only transformer（`ModelConfig`：`vocab_size/d_model/n_heads/n_layers/d_ff/max_seq_len/layer_norm_eps`），权重由 `generate_weights(config, seed)` 以固定种子确定性生成并存成单个 `.npz`。真实权重可后续替换，接口不变。
- **层区间沿用 Controller 的约定：左闭右开、从 0 开始**。`[0, 2)` 表示第 0、1 个 transformer block；PR #1 返回的 `layer_start` / `layer_end` 可直接使用。
- `n_layers` 只数 transformer block。embedding 属于 `layer_start == 0` 的那一段；final layer norm 与 unembedding 属于 `layer_end == n_layers` 的那一段。
- 不使用权重共享（embedding 与 unembedding 权重独立），因此每段权重自洽：**一个节点只加载自己区间的参数，不需要任何其他段的权重**。

## 2. tensor 契约

| 位置 | 名称 | dtype | shape | 说明 |
| --- | --- | --- | --- | --- |
| 首段输入 | `tokens` | `int64` | `[batch, seq]` | token id，取值 `[0, vocab_size)`，`seq <= max_seq_len` |
| 段间 | `hidden` | `float32` | `[batch, seq, d_model]` | 上游 activation |
| 末段输出 | `logits` | `float32` | `[batch, seq, vocab_size]` | 未做 softmax |

线格式（帧、校验、超时、取消）由 `communication/` 定义，见 `communication/PROTOCOL.md`。因为单机参考实现与分布式实现执行**同一份算子序列**，同一 checkpoint + 同一 prompt 下分布式 logits 与单机 logits **逐字节相同**（测试断言 `np.array_equal` 与 payload 摘要一致，不是 `allclose`）。

## 3. 对 node-agent 暴露的接口

```python
from inference_engine import AssignmentPlan, load_assigned_stage, StageServer, PipelineClient

# 1) Controller 轮询到的 assignment 直接交给引擎：解析层区间、上下游 endpoint、deadline
runtime, plan = load_assigned_stage(assignment, config, weights_path)   # -> (StageRuntime, AssignmentPlan)

# 2) 本节点对外监听地址 = 注册时上报的 endpoint；next 段地址取 plan.next_endpoint
server = StageServer(runtime, address=(host, port), downstream=plan.next_endpoint,
                     timeout=30.0, deadline=plan.deadline, cancel=cancel_event)
server.serve_forever(max_requests=None, should_stop=stopping)           # 领取并执行任务

# 3) 首段节点还需要一个调用方把 prompt 送进来（联调/自测用）
logits = PipelineClient(server.address, model_id=config.model_id,
                        deadline=plan.deadline, cancel=cancel_event).run(tokens)
```

`assignment` 只需是 PR #1 `/v1/nodes/{id}/assignments` 返回的 dict（`id/task_id/layer_start/layer_end/previous_node/next_node/request/deadline`）；`http://host:port` 形式的 endpoint 会被转成 `(host, port)`。缺字段、区间越界、端点不可解析、`model_id` 与本地配置不符都会抛 `ShardError`，不会带着错误配置启动计算。

`deadline` 是 **epoch 秒**（`time.time()` 基准），与 Controller 的 `assignment.deadline` 和 node-agent 的 `ExecutionContext.deadline` 同基准，不需要调用方做时钟换算。`StageServer.serve_forever` 在 deadline 到期后抛 `ChannelTimeout` 并停止等待（不再继续轮询已被 Controller 判失败的任务）；`serve_once` 在 deadline 到期时同样抛 `ChannelTimeout`。

### node-agent 需要负责的部分（不在本模块内）

1. 注册、凭据持久化、心跳、轮询 assignment，并按 assignment id 去重；
2. 计算前检查 `task_status ∈ {queued, running}`、`now < deadline`；
3. 上报 `running` / `completed` / `failed`；
4. 收到 `cancelled` / `failed` / deadline 到期时调用 `cancel_event.set()` 并等待 `serve_forever` 返回，再启动下一个任务；
5. **一个节点同时只跑一个任务**（`StageServer` 本身是串行的，`serve_forever` 只在一个线程内 accept → 执行 → 回包）。

### 本模块不做的事

不调用 Controller HTTP 接口、不下载或切分模型文件、不持久化凭据、不做设备发现。这些分别属于 `node-agent/`、`model-manager/` 与 `scheduler/`。

## 4. 错误与终止语义

| 情况 | 抛出 | 对上游的可见结果 |
| --- | --- | --- |
| 上游区间与本地不接续 / 模型不符 / task 不符 | `ShardError` | `ERROR` 帧（code=`ShardError`） |
| 本段执行失败（token 越界、shape 不符、seq 超限） | `ShardError` | `ERROR` 帧 |
| 下游失败 | `PeerError` | 原样透传 `ERROR` 帧 |
| 单次读超时 / 到达 deadline | `ChannelTimeout` | `ERROR` 帧；节点释放连接 |
| 对端取消或本地 cancel 事件 | `TaskCancelled` | `CANCEL` 帧 |

任一异常都会关闭连接并丢弃该任务的缓冲；节点不需要额外清理。

## 5. 版本与兼容

- 协议版本：`v1`（本文件与 `communication/PROTOCOL.md` 同号）。
- 破坏性变更（帧字段、层语义、接口签名）→ 提升版本号并同时更新两个模块的 PR。
- 纯实现内部重构（不改变上述语义）不升版本。

## 6. 验证

`../README.md` 的「本地双节点联调」一节给出可复现命令；`tests/test_two_node_e2e.py` 用两个独立进程跑 `[0,2)` 与 `[2,4)` 并比对单机 logits 的 SHA-256。
