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

## 常驻内部演示（start / stop）

`run_scenarios` 是一次性验证：跑完即清理，用户无法手工访问系统。若要让用户自己
起一套真实 Controller + 两个真实 Node Agent、手工提交任务、用完再回收，
用 `integration.demo`（同样只启动和观测既有组件，不改产品代码）：

```bash
# 一次性：建三个隔离 venv（controller / node / integration）
python3 -m integration.demo setup
# 启动：真实 Controller + node-a + node-b，detached 常驻，记录 PID/端口/日志
.venv-integration/bin/python -m integration.demo start
# 查看记录到的进程是否仍归本演示所有、监听端口
.venv-integration/bin/python -m integration.demo status
# 一次真实链路：HTTP 建任务 + PipelineClient 走 TCP，与单机参照对照，写结果 JSON
.venv-integration/bin/python -m integration.demo request
# 停止：核对归属后只终止本演示记录到的 PID，并断言端口已释放
.venv-integration/bin/python -m integration.demo stop
```

`--workdir` 是顶层参数，必须写在子命令**之前**；写成 `… start --workdir DIR`
会被 argparse 以退出码 2 拒绝。正确位置示例：

```bash
.venv-integration/bin/python -m integration.demo --workdir DIR start
```

上面的命令都在**已获取本仓库固定源码的仓库目录**内执行（`-m integration.demo`
需要仓库里的 `integration` 模块和已建好的 venv）；`DIR` 指专用演示目录。

`--workdir DIR`（默认 `integration/demo-run`，已 gitignore）指定状态目录，只应
指向专用空演示目录；`start` 会在其中生成 tiny 权重、日志和 `demo-state.json`。
该目录本身可以是空的，但命令仍须在已获取本仓库固定源码的仓库目录内运行，不能
在未取得 `integration` 模块的纯空目录里直接执行。

`start` 每次都会重置目录内的演示系统：清空 `node-a/`、`node-b/`、`logs/` 与
`controller.sqlite3*` 后重建。它只作用于这四个子路径，目录内其余文件保留，也不
会删除目录外的文件；但正因如此，`--workdir` 只应指向专用空演示目录，切勿指向
含用户数据的目录。

`demo-state.json` 保存本次运行的 bearer token，以 0600 写入、不打印、不提交；
其余命令不带 token 参数，凭据只从该文件读。

子命令一览：

| 子命令 | 作用 | 退出码 |
| --- | --- | --- |
| `setup` | 建 `.venv-controller` / `.venv-node` / `.venv-integration` 并装依赖 | 0 成功 |
| `start` | 起 Controller + 两个 Node Agent（`--executor stage`），等注册后在 `demo-state.json` 记录 pid/pgid/lstart/argv/端口/日志 | 0 成功，1 已存在活动演示 |
| `status` | 列出记录的进程、归属校验结果、lsof 实际监听端口 | 0 |
| `request` | 提交一次任务并跑一次管道请求，写 `demo-result.json` | 0 全通过，1 有断言不通过 |
| `stop` | 归属校验后 SIGTERM 进程组，核对端口释放 | 0 全部回收，1 有残留 |

与 `run_scenarios` 的两点差别：演示进程 detached 常驻（由 `stop` 回收），并且
`request` 等待 StageServer 就绪时只用 Node Agent 日志与 `lsof`，不发起裸 TCP
探测——裸探测会被 StageServer 当接入并记成一次失败对端、随后空转 `serve_poll`
(0.25s)，会把随后真实请求的时延抬高约 0.25s。因此本演示的 `latency_seconds`
是不含该空转的真实一次前传往返，数值明显小于 `run_scenarios` 的
`normal_e2e`（后者含该空转，见 [fault_matrix.md](fault_matrix.md)）。

**能做 / 不能做**：能做——真实 Controller 调度、两个独立 Node Agent 进程各自
StageServer 按注册 endpoint 监听、HTTP 建任务与查终态、客户端 TCP 送 token 取
logits、与单机参照逐字节一致、PID/端口归属与回收。不能做——它不是两台物理设备
（同机多进程）、不加载真实/预训练模型（只有自研 tiny，`lastcore/tiny-cpu`，4 层、
155392B 参数）、无 decode/KV/流式（一次请求=一次 prefill 前传）、HTTP 建任务与
TCP 数据面仍是两条独立路径（`POST /v1/tasks` 不会触发 TCP 请求）。指标口径与
`## 测量的指标与口径` 一节相同：`latency_seconds` 是一次 full-forward/prefill
往返，不是真实 TTFT；`prefill_tokens_per_second` 是 prefill 吞吐而非生成速率；
RSS 为 `ps` 采样值。

## CI：内部演示真实链路 smoke

`.github/workflows/integration-demo.yml`（新增，与既有三个模块 workflow 并存）在
push / pull_request 命中 `integration/**`、`inference-engine/**`、
`communication/**`、`controller-server/**`、`node-agent/**` 或该 workflow 自身时，
在 `ubuntu-latest` 上用 Python 3.12 跑一次**真实正常链**，不是模块 pytest、也不是
`--help` 之类的静态检查：

1. `python -m integration.demo setup`（建三个模块各自的 venv 并装依赖）
2. `.venv-integration/bin/python -m integration.demo --workdir integration/demo-run start`
3. 同上 `request`（HTTP 建任务 + PipelineClient 走 TCP，与单机参照对照）
4. 即使前面失败也执行 `stop`，按归属校验回收记录的 PID/进程组并核对端口释放

`start` / `request` / `stop` 必须用 `.venv-integration/bin/python`（普通解释器没有
numpy 与 harness 模块，会在 `main()` 里以退出码 2 拒绝），只有 `setup` 用系统 `python`。

判定以**退出码**为准：`start`、`request`、`stop` 的 rc 记进 `rc.txt`，任一步非 0 即
job 失败；`request` 为 0 后还会再读 `demo-result.json`，要求 `pass`、`bit_identical`
为 true 且 `task_terminal_status` 为 `completed`。

`permissions: contents: read`，不新建 token/secret、不提高权限、不付费 runner。
`timeout-minutes`：job 20，`setup` 10，`start`+`request` 6，`stop` 3；因此 `stop`
总有时间执行。失败时上传 token-free 日志（`integration/demo-run/artifacts/` 与
`integration/demo-run/logs/`，含 `start`/`request`/`stop` 的 stdout、`rc.txt` 与三个
真实进程日志），保留 7 天。`demo-state.json`（0600，含 bearer token）**不在**上传
路径内；实测三个日志文件中不含任何 token。

本地等价复现（Python 3.12；本仓库在 3.12.14 上实测）：

```bash
python3.12 -m integration.demo setup
.venv-integration/bin/python -m integration.demo --workdir integration/demo-run start
.venv-integration/bin/python -m integration.demo --workdir integration/demo-run request
.venv-integration/bin/python -m integration.demo --workdir integration/demo-run stop
```

`integration/demo-run/` 已 gitignore，CI 产物不落进提交。本 workflow 只做上述 demo
smoke，不重复既有模块单测、不部署、不取权重。

## 故障注入

`TCPRelay` 支持 `close_after_up_bytes=N`：在客户端→首段方向转发超过 N 字节后同时关闭
两端 socket，用于复现“对端在帧中途断开”。`run_scenarios` 目前的故障路径通过
真实进程操作注入（杀进程、取消任务、过期 deadline、错误令牌），不依赖对协议打补丁。

## 扩展场景

在 `run_scenarios.py` 里用 `@scenario("name", "purpose")` 注册一个函数即可，函数接收
`Ctx` 并返回 dict；返回 dict 的 `pass` 决定该场景状态，`cleanup`/`ports_released`
由调用方补齐，其余键原样写进报告。
