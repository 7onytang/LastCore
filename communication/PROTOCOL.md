# communication —— 节点间 tensor 传输协议（v1）

`communication/` 只做一件事：把上游节点算出的 activation（tensor）可靠、可校验、可取消地送到下游节点，并把最终结果沿原路带回。它不解析模型语义，也不依赖除 `numpy` 之外的三方库。

## 一帧的格式

```
偏移  长度  字段
0     2     magic = "LC"
2     1     协议版本 = 1
3     1     帧类型（见下表）
4     4     header 长度，大端 uint32
8     4     payload 长度，大端 uint32
12    H     header：UTF-8 JSON 对象
12+H  P     payload：tensor 原始字节
```

| 类型 | 值 | 方向 | 用途 |
| --- | --- | --- | --- |
| `HELLO` | 1 | 双向 | 握手，交换层区间、模型、task/assignment |
| `TENSOR` | 2 | 上游 → 下游 | 传输本段输入（token id 或 hidden state） |
| `RESULT` | 3 | 下游 → 上游 | 回传流水线最终 logits |
| `ERROR` | 4 | 双向 | 本段执行失败，附带 `code` 与 `message` |
| `CANCEL` | 5 | 双向 | 任务被取消，收到方立即停止 |
| `BYE` | 6 | 双向 | 正常结束 |

单帧上限：header 64 KiB，payload 512 MiB；超过即 `ProtocolError`，不会分配内存。

## tensor 编码

- header 必填字段：`kind="tensor"`、`name`、`dtype`、`shape`、`bytes`、`sha256`。
- `dtype` 白名单：`float32`、`float16`、`int32`、`int64`，一律按**小端、C 连续**写出。
- `sha256` 是 payload 的摘要：接收方先校验长度与摘要，再 `np.frombuffer(...).copy()`，因此截断、粘包错位和内容损坏都会在分配前被拒绝。
- 非连续数组（切片）在发送前自动压缩为连续内存，`shape` 以压缩后的为准。

## 握手与一次任务的数据流

每个节点向 Controller 注册的 `endpoint` 就是本节点 `StageServer` 的监听地址。一次任务在每一跳上是一条全新的 TCP 连接，天然与 task/assignment 绑定：

```
调用方 ── HELLO(role=client) ──▶ 首段节点
        ◀─ HELLO(role=first, layer_start/end) ──
        ── TENSOR(name=tokens, int64[batch,seq]) ──▶
                                            首段节点 ── HELLO ──▶ 下一段节点
                                                      ── TENSOR(name=hidden, float32[b,s,d_model]) ──▶
                                                      ◀──────── RESULT(name=logits, float32[b,s,vocab]) ──
        ◀──────────── RESULT(name=logits) ──────────
        ── BYE ──▶
```

握手校验（任一条不满足即回 `ERROR` 并断开，绝不静默降级）：

1. `model_id` 一致；
2. 上游 `layer_end` == 本地 `layer_start`（层区间必须首尾相接）；
3. `task_id` 一致（若双方都携带）。

## 超时、取消与资源回收

| 机制 | 触发 | 结果 |
| --- | --- | --- |
| `timeout` | 单次读/写超过该秒数 | `ChannelTimeout`，连接关闭 |
| `deadline` | 任务的绝对截止时间到达（**epoch 秒**，与 Controller 的 `assignment.deadline`、node-agent 的 `context.deadline` 同基准） | `ChannelTimeout`，连接关闭 |
| `cancel`（`threading.Event`） | 操作者在本地调用 `runtime.cancel()` | `TaskCancelled`，连接关闭 |
| 上游 `CANCEL` 帧 / 上游断开 | 对端取消或崩溃 | `TaskCancelled` / `ChannelClosed` |

cancel 事件与 deadline 在**每次收/发迭代**都会重新检查（发送按 0.25 s 分片写出，而不是一次阻塞式 `sendall`），而不是只在阻塞读写超时时检查：对端持续发包（哪怕是恶意或异常的超长帧）或停止读取都无法推迟取消、无法越过 deadline。`timeout` 是单次读写操作的预算，`timeout=None` 表示只要还在推进就一直等，取消与 deadline 仍然按 0.25 s 粒度生效。

等待时每 0.25 s 轮询一次 cancel 事件，所以在阻塞读/写中也能在亚秒级响应取消，不会出现“取消后仍占着连接和缓存”的情况。`Channel` 只按连接持有缓冲，关闭即释放；`StageServer` 一次只处理一个任务，符合 Controller 文档中“节点本地禁止并行执行两个任务”的要求。

连接阶段会在解析地址前、解析返回后以及 TCP 连接等待期间检查 cancel 与 deadline。标准库 `socket.getaddrinfo()` 是同步调用，无法被这些机制中断；因此 DNS/名称解析耗时不受 `connect_timeout`、任务 deadline 或 cancel 保证约束。需要严格限制连接建立耗时时，请传入数字 IP 地址。

## 对外接口

```python
from communication import Channel, ChannelListener, encode_tensor, decode_tensor

link = Channel.connect("10.0.0.12:9000", timeout=30, deadline=1700000000.0, cancel=event)
link.send_hello(role="first", layer_end=0)
link.recv_hello()
link.send_tensor("hidden", hidden, task_id=..., assignment_id=...)
logits, header = link.recv_result()
link.close()
```

`encode_tensor` / `decode_tensor` 可单独用于把 tensor 落到消息队列、文件或测试夹具中。

## 现成方案调研与自研理由

在动手前比较了可复用的开源方案（数据取自各仓库的 GitHub API，2026-10-11）：

| 方案 | 许可证 | 维护状态 | 不采用的原因 |
| --- | --- | --- | --- |
| pytorch/pytorch（`torch.distributed.rpc`、`send/recv`） | BSD-3-Clause | 104k star，当日仍有提交 | 引入 PyTorch（数百 MB）才能跑通链路；手机端与轻量节点不现实，与“CPU 上可验证的小模型”目标冲突 |
| grpc/grpc + protocolbuffers/protobuf | Apache-2.0 / BSD-3-Clause | 45k / 72k star，活跃 | 需要 proto 代码生成与 HTTP/2 运行时，对纯 tensor 流偏重；可作为二期替换（见下） |
| zeromq/libzmq | MPL-2.0 | 11k star，活跃 | 需要额外原生库；本身不定义 tensor 语义，仍要自写编解码与超时策略 |
| msgpack/msgpack | Apache-2.0 | 7.5k star，2024 后提交较少 | 只解决序列化；numpy 二进制 payload 直接传输更快，缺少长度/摘要/帧类型 |
| NVIDIA/nccl | BSD-3-Clause | 5.1k star，活跃 | 面向多 GPU 集合通信，需要 CUDA，不覆盖手机/CPU 异构图 |
| `np.save`/pickle over socket | - | - | 无帧边界、无校验、无类型白名单、无超时语义，且 pickle 反序列化有安全风险 |

结论：一期只传 `float32/int64` activation，用标准库 TCP + 自描述帧（长度 + 摘要 + 类型白名单）即可覆盖需求，零依赖、任意设备可跑、CPU 上可验证。协议的边界是 `HELLO/TENSOR/RESULT/ERROR/CANCEL`，将来若需要多路复用、背压或 TLS 双向认证，可在保持帧语义不变的前提下把底层换成 gRPC/ZeroMQ（届时按 Apache-2.0/MPL-2.0 引入）。

## 验证

```bash
cd communication
pip install -r requirements-dev.txt
pytest -q
```

45 项测试覆盖：编解码往返（全部 dtype）、非连续数组、截断/篡改/形状不符、必填 `name`/`sha256` 缺失、magic/版本/未知帧类型、超大 header、非 JSON header、握手往返、`ERROR`→`PeerError`、`CANCEL`→`TaskCancelled`、对端关闭、读超时、绝对 deadline、阻塞读中的 cancel 事件、阻塞写中的 cancel 与 deadline（对端停止读取也不得推迟）、**持续收包中的 cancel 与 deadline（对端一直发包也不得推迟）**、监听端口与端口占用后的连接拒绝。

## 已知限制

- 一期不加密、不鉴权：节点 `endpoint` 只在受信网络（内网/VPN/TLS 隧道）暴露，凭据体系仍归 Controller 与 node-agent 负责。
- 一条连接一个任务，无多路复用与背压；大 activation 无法分块，超限直接报错。
- 不做重试与断点续传：失败交由 Controller 的任务状态与上游节点决策。
- 仅 AF_INET/TCP，尚未接入 PR #1 文档中规划的 BLE / WiFi Direct 二期链路。
