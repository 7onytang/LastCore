# inference-engine

分布式分层推理引擎：把一个 decoder-only transformer 按层切成连续片段，每个节点只加载并执行自己那一段，activation 由 `communication/` 沿 `next_node` 逐跳传递，最后一跳的 logits 原路返回。执行协议见 [PROTOCOL.md](PROTOCOL.md)。

一期目标不是性能，而是**可验证的正确性**：CPU 上跑一个 tiny 模型，证明“拆开跑”与“单机跑”的结果**逐字节一致**。

## 组成

| 文件 | 职责 |
| --- | --- |
| `inference_engine/config.py` | `ModelConfig`（层数、宽度、头数、序列上限）与 JSON 读写 |
| `inference_engine/model.py` | 算子实现：embedding、causal attention、MLP、layer norm、unembed |
| `inference_engine/weights.py` | 确定性权重生成、`.npz` checkpoint、**按段加载**`load_shard` |
| `inference_engine/stage.py` | `StageRuntime`（一段的执行语义）、`segment_plan`（均分切层） |
| `inference_engine/reference.py` | 单机全模型前向，作为分布式结果的对照基准 |
| `inference_engine/pipeline.py` | `StageServer`（监听/执行/转发/回传）、`PipelineClient`（把 prompt 送进首段） |
| `inference_engine/assignment.py` | 把 Controller 的 assignment 映射成本地位段、上下游 endpoint、deadline |
| `inference_engine/node.py` | 命令行：`generate` / `serve` / `run` / `reference` |
| `examples/two_node_local.py` | 一键本地双节点（或多节点）联调与对照 |

## 模型与切分

- tiny decoder-only transformer：默认 4 层、`d_model=32`、4 头、`d_ff=64`、词表 64、最大序列 16，共 53 个参数张量 / 155392 字节（float32），单机 CPU 毫秒级完成。
- 层区间**左闭右开、从 0 开始**，与 Controller 完全一致：`[0,2)` 是第一段，`[2,4)` 是最后一段。
- 首段额外做 token/position embedding，末段额外做 final layer norm 与 unembedding，中间段只跑 transformer block。embedding 与 unembedding 权重独立，因此**每段权重自洽**，节点永远不加载别人的层。
- 权重由 `generate_weights(config, seed)` 确定性生成，任何人可用同一种子复现同一条链路。

## 快速开始

这两个包没有打包安装（仓库内直接运行），因此 `inference_engine` 与 `communication` 都需要在 `PYTHONPATH` 上；测试由各自 `pytest.ini` 配置好，命令行运行需显式设置：

```bash
cd inference-engine
pip install -r requirements-dev.txt
export PYTHONPATH="$PWD:$PWD/../communication"        # 或每次执行前加 PYTHONPATH=... 前缀

# 1) 生成 checkpoint（同时写出模型配置）
python -m inference_engine.node generate --write-config tiny.json --out tiny.npz --seed 7

# 2) 单机参考结果
python -m inference_engine.node reference --config tiny.json --weights tiny.npz --prompt "1 2 3 4"
```

未设置 `PYTHONPATH` 时 `python -m inference_engine.node` 会报 `ModuleNotFoundError: No module named 'communication'`（或 `inference_engine`）；从仓库根运行则用 `PYTHONPATH=inference-engine:communication`。`examples/two_node_local.py` 只依赖标准库，会自己为子进程设置这两个路径，因此不需要额外配置。

## 本地双节点联调

一条命令拉起两个独立进程（末段先监听，首段指向它），跑通后与单机结果对照（不需要 `PYTHONPATH`，示例会为子进程自动设置）：

```bash
python examples/two_node_local.py                 # [0,2) + [2,4)
python examples/two_node_local.py --layers 0:1,1:3,3:4 --prompt "5 6 7"
```

实测输出（Python 3.14.7 / macOS arm64，2026-10-11）：

```
checkpoint: 53 parameters, 155392 bytes
single process: shape [1, 4, 64] next_token=5 sha256 f94f323f05748159...
stage [2, 4) listening on 127.0.0.1:55976
stage [0, 2) listening on 127.0.0.1:55977
distributed:    shape [1, 4, 64] next_token=5 sha256 f94f323f05748159...
stage [2, 4): served 1 task(s), loaded 76800 parameter bytes (only this segment)
stage [0, 2): served 1 task(s), loaded 78592 parameter bytes (only this segment)
bit-identical to the single-machine result: True
```

三节点（`0:1 + 1:3 + 3:4`）同样逐字节一致，三段参数字节 44416 + 68352 + 42624 = 155392，恰好等于整份 checkpoint。

手工分步也可以（便于对接 node-agent 时观察每一跳）：

```bash
python -m inference_engine.node serve --config tiny.json --weights tiny.npz \
    --layers 2:4 --listen 127.0.0.1:0 --stats-out stats-b.json           # 末段，打印 READY <host> <port>
python -m inference_engine.node serve --config tiny.json --weights tiny.npz \
    --layers 0:2 --listen 127.0.0.1:0 --downstream 127.0.0.1:<portB>      # 首段
python -m inference_engine.node run --first-stage 127.0.0.1:<portA> --prompt "1 2 3 4"
```

## 与 node-agent 对接

```python
from inference_engine import load_assigned_stage, StageServer, PipelineClient

runtime, plan = load_assigned_stage(assignment, config, weights_path)   # 来自 /v1/nodes/{id}/assignments
server = StageServer(runtime, address=(host, port), downstream=plan.next_endpoint,
                     timeout=30.0, deadline=plan.deadline, cancel=cancel_event)
server.serve_forever(should_stop=lambda: stopping)
```

节点上报给 Controller 的 `endpoint` 必须就是 `StageServer` 的 `(host, port)`；`plan.next_endpoint` 直接取自 assignment 的 `next_node.endpoint`。取消、超时、错误、上下游校验的行为见 [PROTOCOL.md](PROTOCOL.md) 第 3、4 节。

## 验证

```bash
cd inference-engine
pytest -q            # 89 项，含两个独立进程的端到端联调
cd ../communication
pytest -q            # 42 项协议与通道测试
```

inference-engine 侧覆盖：配置校验、参数集按段划分且互不重叠、只加载本段权重、单机前向确定性、attention 因果性、1/2/3/4 段切分的链路结果与单机逐字节一致、多层链路的 role 与参数字节核算、assignment 解析（endpoint/区间/deadline/model 不符拒绝）、**assignment 的 epoch deadline 传到 StageServer 并真实生效**、TCP 上的 2/3/4 段链路、非末段缺下游拒绝、区间不接续拒绝、模型不符拒绝、**token 与 hidden 的 batch 上界（超界在分配前拒绝、等于上界可正常推理）**、cancel 事件与 CANCEL 帧传播、读超时、deadline、**deadline 到期后不再空转轮询**、**deadline 未到期时正常服务**、静默对端、空闲等待不被 kill、本段失败以 `ERROR` 回传、两个独立进程的端到端比对与三段链路比对。

## 已知限制

- 只支持 decoder-only、无权重共享的自研 tiny 模型结构；接入真实模型（如 Qwen）需要替换 `model.py` 的算子与权重映射，切分与传输协议无需改动。
- 一次请求一条连接、一段一次任务，无 batching、无流水并行（micro-batch）、无 KV cache 复用。
- 无重试、无迁移：任务失败即上抛，由 Controller 决定后续，与 PR #1 当前语义一致。
- 不加密、不鉴权（见 `communication/PROTOCOL.md`）；云端部署仍需反向代理/TLS 与 node-agent 的凭据体系。
- 未做 activation 量化与压缩（仅支持 float32/float16），带宽是后续优化项。
