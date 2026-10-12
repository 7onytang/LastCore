# communication

节点间 activation / tensor 传输层。协议定义见 [PROTOCOL.md](PROTOCOL.md)。

```python
from communication import Channel, ChannelListener
```

## 组成

| 文件 | 职责 |
| --- | --- |
| `communication/protocol.py` | 帧封装 `encode_frame` / `parse_prefix`、tensor 编码 `encode_tensor` / `decode_tensor`、`FrameType` |
| `communication/channel.py` | `Channel`（一条连接上的同步收发、timeout / deadline / cancel）、`ChannelListener`（监听与 accept） |
| `communication/errors.py` | `ProtocolError` / `ChannelTimeout` / `ChannelClosed` / `PeerError` / `TaskCancelled` |

## 快速开始

```bash
pip install -r requirements-dev.txt
pytest -q
```

纯标准库 + numpy，无原生依赖；Python 3.12 / 3.14 均已验证。

## 谁在用

`inference-engine` 的 `StageServer` / `PipelineClient` 直接构建在 `Channel` 之上：每一跳一条连接，上游发 `TENSOR`、下游回 `RESULT`，失败送 `ERROR`，取消送 `CANCEL`。node-agent 只需把 Controller 下发的 `next_node.endpoint` 交给 `StageServer(downstream=...)`。

## 设计取舍

- **零依赖**：一期只跑 CPU 小模型链路，不引入 PyTorch / gRPC / ZeroMQ 等运行时可复用的重依赖，理由与调研数据见 [PROTOCOL.md](PROTOCOL.md#现成方案调研与自研理由)。
- **显式帧边界**：长度 + SHA-256 + dtype/shape 校验，先校验后分配，损坏数据不会进入模型。
- **可取消是一等需求**：Controller 取消任务后无法杀死远端进程（PR #1 文档第 117 行），因此读、写、accept 三个阻塞点都轮询 cancel 事件，节点能在亚秒级释放连接与缓冲。cancel 与 deadline 在**每次接收迭代**都重新检查，对端持续发包也无法推迟取消或越过 deadline；`deadline` 为 epoch 秒（`time.time()` 基准），与 Controller `assignment.deadline`、node-agent `ExecutionContext.deadline` 同基准。
