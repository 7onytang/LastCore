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

## 建连预算与取消

`Channel.connect` 使用 Python 标准库非阻塞 socket、`DefaultSelector` 和 `SO_ERROR`，沿用地址解析结果顺序尝试，每个候选只发起一次连接；不新增依赖或修改帧协议。`connect_timeout`（默认 10 秒，可为 `None`）是整个建连调用的共享单调时钟预算，不会在每个地址上重新获得 10 秒。它与 epoch `deadline` 取更早的边界；取消和期限在解析前后、连接等待每个至多 0.25 秒的切片以及交付前检查。成功后恢复原有 socket 超时模式，帧收发的 `Channel.timeout` 不变。失败、取消、超时及 socket 配置异常都会关闭尚未交付的 socket。

系统 `getaddrinfo` 仍为同步 DNS 解析，无法在本函数中抢占；其耗时计入预算，返回后先检查取消与期限再创建 socket。因此不保证 DNS 卡住时也能在 0.25 秒内退出。调度/I/O 延迟也不构成实时保证；本次不增加解析线程或重试连接来隐藏此限制。

选型依据：[Python socket 与连接模式](https://docs.python.org/3/library/socket.html#notes-on-socket-timeouts)、[selectors](https://docs.python.org/3/library/selectors.html)及 [CPython 地址回退实现](https://github.com/python/cpython/blob/3.12/Lib/socket.py)。复用现有标准库，避免新网络框架和协议兼容成本；本次未复制上游模块源码。

只运行纯 Fake socket/selector/clock/event 夹具，无 DNS、TCP、模型或设备任务：

```sh
python -m pytest -q tests/test_connect_budget.py
```
