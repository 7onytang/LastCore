# 多设备接力推理演示验收

本文描述当前仓库已经实现的接力推理路径。演示目标是证明同一个 tiny decoder-only 模型拆到多个 Worker 后，结果与单机前向逐字节一致；圆周率估算和日志统计适合作为调度看板的辅助负载，不替代这条推理验收。

## 严格策略前置条件

当前严格默认拒绝新建 `lastcore/tiny-cpu` 分层任务，因为可信本地权重／资源 profile 尚未绑定。
旧接力只能在服务端 owner 明确批准的隔离兼容实例中进行，由该实例显式设置
`CONTROLLER_LEGACY_TASK_MODEL_IDS=true`；这不构成严格策略、硬件预算或真实设备验收。
不要为执行本文步骤自动开启现有生产 Controller 的 legacy 模式，也不要伪装成算术模型 ID 绕过策略。
下述本地脚本仅为它自己创建的临时 Controller／临时数据库显式选择兼容模式，不更改已有 ECS。
本轮仍暂停新增真实任务／设备验收；本文是后续授权后的操作说明。

## 运行关系

```text
管理员/演示客户端
        |
        | POST /v1/tasks（管理员 token）
        v
Controller（ECS）
        ^                 ^
        | heartbeat +      | heartbeat +
        | assignments poll | assignments poll
   Worker A              Worker B              Worker C ...
   [0, 2)  --activation--> [2, 4) --> ... --> [last]
                                      logits 返回首段
```

Controller 不主动连接 Worker。每个 Worker 使用自己的节点凭据主动发送心跳并轮询 `/v1/nodes/{node_id}/assignments`，因此 Controller 只需要能够接收 Worker 的 HTTPS 请求。接力计算本身要求相邻 Worker 的 `endpoint` 能互相建立 TCP 连接；公网 ECS、NAT 或防火墙不能把这个地址填成 `127.0.0.1`。

## 本地可复现验收

这条命令会启动一个临时 Controller、两个独立 Node Agent 和两个 `StageServer` 进程，生成确定性 tiny 权重，提交 4 层任务，并把 prompt 送到首段。脚本会等待所有 assignment endpoint 就绪，检查两个 Worker 都回报 `completed`，再比较分布式 logits 与单机参考结果。

```bash
CONTROLLER_PY=/path/to/controller/bin/python \
NODE_PY=/path/to/node/bin/python \
ENGINE_PY=/path/to/inference-engine/bin/python \
LC11_RUN_DIR=/private/tmp/lastcore-relay-evidence \
bash integration/run_two_node_local.sh
```

成功条件：

- 输出包含 `bit_identical_to_single_machine: true`；
- Controller 最终状态为 `task=completed assignments=[completed,completed]`；
- `$LC11_RUN_DIR/summary.json` 中 `cleanup_ok` 为 `true`，且没有残留进程或端口；
- 两个 stage 的层区间连续且不重叠（默认 `[0,2)`、`[2,4)`），参数字节数之和等于完整 checkpoint。

也可以只运行推理引擎的模块级接力测试：

```bash
cd inference-engine
pytest -q tests/test_two_node_e2e.py tests/test_pipeline_over_tcp.py \
  tests/test_assignment_contract.py
```

这些测试覆盖两段和三段独立进程、activation 经 TCP 转发、上下游 assignment 校验、deadline/cancel，以及与单机结果的逐字节比较。若当前环境没有安装 pytest，命令应明确报告依赖缺失，不能把未运行当作通过。

## 接入 ECS 和真实设备

1. 先由服务端 owner 确认使用明确授权的隔离 legacy 兼容实例；严格默认会拒绝 tiny 新建任务。不得自动改动已有生产配置。然后在该 ECS 上运行 Controller，并通过 HTTPS 暴露 `/healthz`、`/v1` 和 `/docs`。
2. 每台设备运行 Node Agent，首次只用注册 token 调用注册接口；成功后持久化返回的 `node_id` 和 `node_token`。
3. `NODE_AGENT_ENDPOINT` 必须是**其他 Worker 可访问且实际监听**的地址。云端设备不能登记 `http://127.0.0.1:9000`，也不能登记只在家庭局域网可见的地址，除非所有 Worker 在同一网络或已经建立 VPN/端口转发。
4. Agent 使用 `--executor stage`、同一份模型配置和 checkpoint。模型 `model_id`、层数和权重必须匹配，否则 assignment 会被拒绝。
5. 管理员创建 `/v1/tasks` 任务后，观察 `/v1/nodes` 的 `available/busy/offline` 状态和 `/v1/tasks/{id}` 的 assignment 状态。首段 Worker 的 `StageServer` 接收 token ids，后续 Worker 接收上游 hidden activation，末段返回 logits。

真实设备验收至少要保存：任务请求、Controller 返回的 assignment 层区间、每台设备的 Agent 日志、任务终态，以及单机参考输出的 hash。当前自动化脚本是本机多进程验收，不等于已经完成两台物理设备或公网带宽验收。

## 看板的状态字段

看板可以直接使用以下已实现 API：

- `GET /v1/nodes`（管理员 token）：节点的 `available`、`busy`、`draining`、`offline` 状态；
- `GET /v1/tasks` 和 `GET /v1/tasks/{id}`（管理员 token）：任务状态、每个 assignment 的层区间、上下游节点和完成状态；
- `GET /v1/nodes/{id}/assignments`（节点 token）：Worker 主动轮询的任务和终止状态。

“busy”表示节点仍被活动流水线保留，即使它已经完成自己的层片段；这样可以避免同一流水线尚未结束时复用节点。Controller 不能强制杀死远端计算，取消和超时必须由 Agent 轮询观察后停止本地 `StageServer`。

## 当前边界

- 引擎支持 tiny decoder-only 模型的分层接力，不是已适配的 Qwen 等生产预训练模型；
- 当前一次请求一次连接，没有 batching、KV cache 或自动迁移/重试；
- communication 层当前不提供加密和节点鉴权，公网部署必须使用 HTTPS/VPN，并限制 Worker endpoint 的网络范围；
- 圆周率、日志统计现已随 `73b3d1` 部署到 `/workloads`，仍是独立的 CPU 分片/归并路径；目前仅代码、CI 和页面/资源可达核验，未实际派发新任务或做新设备验收，不能宣称它们验证了 activation 接力或继承本页历史 tiny 证据。
