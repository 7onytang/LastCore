# 执行与 tensor 协议（v1，已与 inference-engine 对齐）

状态：**v1 生效**。§1 执行接口是 node-agent 的硬契约；§2 tensor 传输已由 Engineer-B 的 `communication/` v1 实现，本文的提案版本已被取代，以 `inference-engine/PROTOCOL.md` 与 `communication/PROTOCOL.md` 为准。

## 1. 执行接口（node-agent 侧契约）

### 1.1 分工

```
controller-server ──HTTP──> node-agent（生命周期编排）
                               │ new Executor() per assignment
                               ▼
              inference-engine（StageServer / load_assigned_stage）
                               │ communication v1 帧（HELLO/TENSOR/RESULT/ERROR/CANCEL/BYE）
                               ▼
                        相邻节点 StageServer
```

### 1.2 Executor 契约

```python
class Executor:
    name: str
    def run(self, context: ExecutionContext) -> None: ...
```

1. **实例隔离**：每个 assignment 一个新实例。禁止跨任务共享实例状态。
2. **阻塞语义**：`run()` 阻塞；正常返回即"本片段完成"（node-agent 上报 completed）。
3. **取消**：至少每秒检查 `context.cancel`，置位后尽快抛 `ExecutionCancelled(context.cancel_reason)` 并释放资源。原因：`task-terminal` / `deadline` / `heartbeat-stall` / `shutdown`。
4. **期限**：deadline 由 node-agent 统一监视（Controller 侧 epoch 秒），到期即请求取消。
5. **异常**：其他异常视为片段失败，node-agent 上报 `failed`。
6. **上下文**：`ExecutionContext` 含 `raw`（Controller 返回的原始 assignment dict，直接可交给 `load_assigned_stage`）。

### 1.3 内置执行器

| id | 说明 |
| --- | --- |
| `demo` | `SliceDemoExecutor`：时间片模拟，验证生命周期与取消契约 |
| `stage` | `StageExecutor`：对接 inference-engine v1（见 §2） |
| `模块:工厂` | 任意自定义执行器工厂 |

## 2. StageExecutor —— inference-engine v1 对接

- `load_assigned_stage(context.raw, model_config, weights_path)` 加载本段；层区间沿用 Controller 左闭右开约定。
- StageServer **绑定本节点注册的 endpoint 地址**（`parse_endpoint(config.endpoint)`），注册地址即监听地址；下游地址取 assignment 的 `next_node.endpoint`。
- 完成语义：`--complete-after N`（默认 1）——本段服务完 N 个请求后正常返回并上报 completed；`0` 表示持续服务直到取消/超时/退出。
- 容错：单个请求失败不终止服务（重试进入下一轮）；连续 3 次失败才上报 failed。
- 权重：默认用 inference-engine 的 `tiny_config()` + 确定性种子生成 checkpoint 存到状态目录；`--model-config` / `--weights` 可替换。
- 依赖：`inference-engine/` 与 `communication/` 目录需在 PYTHONPATH，且需要 numpy（见 node-agent README）。

## 3. 与 B 的接口差异记录（待 LC-5 契约评审确认）

1. **deadline 单位不一致**：`StageServer.deadline` 语义是 `time.monotonic()`（见 `communication/channel.py` 的 deadline 运算），而 Controller assignment 的 `deadline` 是 epoch 秒。适配器目前**不向 StageServer 传 deadline**（传 `None`），deadline 由 node-agent 统一监视并触发 cancel。建议 B 侧明确换算或改收 epoch。
2. **serve_forever 的单请求异常会终止服务循环**：`pipeline.serve_forever` 中 `_serve` 抛出的异常直接向上传播。适配器在外层重试（连续 3 次失败才失败），建议 B 把 serve_forever 改为单请求容错（记录并继续服务）。
3. **完成语义**：B 的接口只有"服务直到停止"；node-agent 以 `complete_after` 个已服务请求数定义"片段完成"，用于 Controller 的 completed 上报。
4. **PipelineClient 的 deadline 参数**同样是 monotonic 语义，调用方（demo 脚本/未来 frontend）需注意。

## 4. 历史提案说明

本文原 §2（节点间传输提案）已被 Engineer-B 的 `communication/PROTOCOL.md` v1 实现取代：帧格式 `magic("LC")|version|type|header 长度|payload 长度|JSON header|tensor 字节`、dtype 白名单小端 C 连续、长度与 SHA-256 校验先于内存分配、握手校验 model_id 与层区间接续。原提案中"prompt 由提交方直发首段节点"与 v1 数据流一致（`PipelineClient` → 首段 StageServer）。
