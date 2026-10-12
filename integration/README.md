# integration

系统级验证：一条命令拉起真实 Controller + 两个真实 Node Agent（各自运行
inference-engine 的 `StageServer`），跑通一次分片推理，并在同一个进程里采集
时延、吞吐、内存与传输字节证据；随后按场景注入故障并记录实际行为。

不改动产品代码，只启动和观测既有组件。覆盖范围与未验证项见
[fault_matrix.md](fault_matrix.md)。

## 前置

三个模块的依赖彼此冲突（`pytest` 9.0.2 与 9.1.1），因此按模块各自创建虚拟环境：

```bash
python3 -m venv .venv-controller && .venv-controller/bin/pip install -r controller-server/requirements-dev.txt
python3 -m venv .venv-node && .venv-node/bin/pip install -r node-agent/requirements-dev.txt
.venv-node/bin/pip install numpy==2.5.4                 # node-agent 的 stage 执行器需要
python3 -m venv .venv-integration && .venv-integration/bin/pip install numpy==2.5.4
```

`.venv-integration` 只给观测脚本用（`numpy` + 标准库）；Controller 与 Node Agent
各自用自己那套依赖运行。

## 运行

```bash
# 在仓库根目录执行
python -m integration.run_scenarios \
  --controller-python .venv-controller/bin/python \
  --node-python .venv-node/bin/python
```

或者用环境变量代替参数：`LASTCORE_CONTROLLER_PYTHON`、`LASTCORE_NODE_PYTHON`。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--scenario NAME` | 只运行指定场景，可重复；默认全部 |
| `--workdir DIR` | 场景工作目录（默认 `integration/runs`，已 gitignore） |
| `--report FILE` | 报告路径（默认 `integration/runs/report.json`） |
| `--keep` | 保留上一次的工作目录（调试用；会复用凭据与数据库） |

每个场景都会真实启动进程、记录观测值、然后清理（SIGTERM 进程组、关闭 relay、
断言端口已释放）。任何场景异常都不会中断其余场景；有失败时退出码为 1。

## 测量的指标与口径

| 指标 | 来源 | 口径与限制 |
| --- | --- | --- |
| 端到端时延（`latency_seconds`） | 观测进程内 `PipelineClient.run` 前后 `time.monotonic()` | 这是一次 full-forward / prefill 往返的墙钟时间：当前协议一次请求=一次完整 prefill 前向，无流式返回，客户端要等整段前向结束后才拿到结果。它**不是**首 token 实测时延；本仓库既不把它称作真实 TTFT，也不把它声明为经证实的上界。真实 decode 阶段的 TTFT 需要流式/逐 token 回传，属后续（LC-8/LC-10 未开始）。报告 JSON 中的 `ttft_seconds` 是与 `latency_seconds` 同值的历史兼容字段，为兼容旧字段保留，同样不是首 token 实测 |
| tokens/s（`prefill_tokens_per_second`） | prompt token 数 ÷ 端到端秒数 | 这是 **prefill** 吞吐（tiny 模型、无 KV cache、无 batching），**不是生成 / decode 速率**，不代表生成吞吐 |
| 每段计算耗时 | Node Agent 日志 `stage served request: {...}`（`local_seconds`、`output_shape`、`parameter_bytes`） | 只统计本段前向；含转发等待的端到端时间另见时延 |
| 峰值内存（`peak_rss_kb` / `metrics.peak_rss_mb`） | 每 100ms 轮询 `ps -o rss= -p <pid>` 取最大值 | 单进程 RSS 的**采样峰值**（100ms 间隔轮询的最大值），不等于进程真实峰值；因为每个 Node Agent 是独立进程，只代表该节点进程自身 |
| 传输字节 | 客户端→首段这一跳由透明 TCP relay 双向计数 | **relay 只覆盖客户端→首段一跳**；**A→B 一跳无法用 relay 计数**：节点的注册 endpoint 按设计就是 `StageServer` 绑定的地址，无法在中间插桩，A→B 字节目前只能按协议帧布局估算，真实计数需要 communication v1 暴露计数器（LC-5 提案，未实现）。relay 自身的监听端口不在报告的 `ports_released` 之列（见 [fault_matrix.md](fault_matrix.md) 的历史证据缺项） |

## 故障注入

`TCPRelay` 支持 `close_after_up_bytes=N`：在客户端→首段方向转发超过 N 字节后同时关闭
两端 socket，用于复现“对端在帧中途断开”。`run_scenarios` 目前的故障路径通过
真实进程操作注入（杀进程、取消任务、过期 deadline、错误令牌），不依赖对协议打补丁。

## 扩展场景

在 `run_scenarios.py` 里用 `@scenario("name", "purpose")` 注册一个函数即可，函数接收
`Ctx` 并返回 dict；返回 dict 的 `pass` 决定该场景状态，`cleanup`/`ports_released`
由调用方补齐，其余键原样写进报告。
