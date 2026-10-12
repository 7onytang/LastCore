# LastCore Android Node (LC-13, phase 1)

Android 端最小节点：在真机上以**前台**方式加入 Controller、执行被分配的模型片段，并对外暴露可被终端界面调用的本地适配接口。本目录是 Engineer-B 在 LC-3 下的独立交付目录，**只新增、不修改** `controller-server/`、`node-agent/`、`inference-engine/`、`communication/`。

## 本阶段范围（phase 1：可行性与最小适配）

LC-13 明确：无 Android 真机/SDK/兼容后端时**先交可验证设计与具体阻塞，不用模拟器结果代替真机**。本目录交付：

1. `FEASIBILITY.md` —— 后端 / 模型格式 / 许可 / arm64 / 资源与热预算的决策矩阵，以及具体阻塞清单（本阶段主要交付物）。
2. `VERIFICATION.md` —— 可执行验证步骤：无设备部分（已跑）、真机部分（前置+命令+判定，缺设备未跑）、证据表。
3. `lastcore_android_node/` —— 可运行的 `BackendAdapter` 最小方案：
   - `device.py`：免权限设备探测（架构 / CPU / 内存 / 电量 / 温度）；真机上再尽力读 `/proc/meminfo`、`/sys/class/power_supply`、`/sys/class/thermal`，读不到就保持 `None`/`unknown`，绝不猜；
   - `budget.py`：权重 + 激活 + KV 的内存预算与接单准入判定；
   - `backend.py`：`BackendAdapter` 协议、能力描述与后端选择；**默认失败关闭**（无真实后端运行时即拒绝接单，模拟后端只在显式 opt-in 时出现）；
   - `backends/`：参考（模拟）后端 + ONNX Runtime Mobile 探测后端；
   - `executor.py`：实现 node-agent `Executor` 契约的接入点，含取消/期限/资源释放；**缺真实模型元数据即拒绝准入**，不退回玩具形状。
4. `scripts/` —— `verify_offdevice.sh`（无设备全覆盖验证，含结构化作与退出码断言）、`probe_device.sh`（adb 真机事实采集 + 准入判定）、`check_memory_ceiling.sh`（1 GiB 上限：能硬限则硬限，否则测量峰值 RSS 并如实标注）。
5. `tests/` —— 单元测试 + 与真实 node-agent `Executor` 契约的联调测试（需 `LASTCORE_NODE_AGENT_PATH`）。

**不包含**（留待获批后）：真机推理 demo、模型转换落地、后台常驻、权限申请、桌面后端改动。

## 目录边界

| 归属 | 目录 | 本次是否改动 |
| --- | --- | --- |
| Controller / 云端调度 | `controller-server/` | 否 |
| 节点生命周期编排 | `node-agent/`（Engineer-A） | 否（仅按 `PROTOCOL.md` 的 `Executor` 契约对接） |
| 分层推理引擎 / 传输 | `inference-engine/`、`communication/`（Engineer-B） | 否（不复制引擎，只声明接入点） |
| Android 最小节点 | `android-node/`（本目录） | 新增 |

## 运行与验证

核心逻辑零三方依赖（Python 3.11+），测试只需 `pytest`：

```bash
cd android-node
scripts/verify_offdevice.sh                       # 单测 + 准入断言 + 1GiB 检查（无设备）
LASTCORE_NODE_AGENT_PATH=/path/to/node-agent scripts/verify_offdevice.sh   # 加真实契约联调
```

单独跑测试（node-agent 基线固定为 `agent/engineer-a-a/b33d0fac979a` = `ba52895`）：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                     # 42 passed, 8 skipped
LASTCORE_NODE_AGENT_PATH=/path/to/node-agent pytest   # 50 passed
scripts/check_memory_ceiling.sh suite      # 1 GiB 上限检查
```

打印一份接单可行性/预算报告（无需设备）：

```bash
python -m lastcore_android_node plan \
  --layers 2:4 --layers-total 4 --d-model 896 --heads 14 --vocab 151936 \
  --weights-bytes 320000000 --tokens 128 --battery 45 --thermal nominal
```

**默认失败关闭**：本机没有 ONNX Runtime 等真实运行时，`plan` 会把后端报成
`unavailable: no backends are registered`（而不是拿模拟后端顶上）。只做链路演示时才加
`--reference-backend`，它明确标注为模拟实现、不产生真实推理结果。

真机事实采集（需要 `adb` + 已授权设备；缺设备会明确报阻塞并 exit 2）：

```bash
scripts/probe_device.sh --layers 0:12 --layers-total 24 \
  --d-model 896 --heads 14 --vocab 151936 --weights-bytes 616000000 --tokens 2048
```

## 与现有契约的对接

- 执行入口实现 node-agent 的 `Executor` 契约（`name` / `run(context)` / 每 ≤1s 检查 `context.cancel` / 抛 `ExecutionCancelled` / 异常路径释放资源），`--executor lastcore_android_node.executor:executor_factory`。
- **导入顺序**：`lastcore_android_node.executor` 在导入时绑定 node-agent 的 `Executor` / `ExecutionCancelled` 基类，因此必须先让 `lastcore_node_agent` 可导入（真实宿主进程天然满足；`node-agent` 是通过 `--executor` 加载本模块的一方）。
- **取消粒度**：执行器只在两次 adapter 调用之间检查取消（`load` 之前一次、每次 `run` 前后），因此可保证的轮询间隔是 `max(一次 load, step_seconds, 一次 run)`。`step_seconds ≤ poll_seconds` 被强制校验；真机适配器必须让每次 `load`/`run` 在 `poll_seconds` 内返回，阻塞整段的调用无法取消。已测的 1 秒只覆盖模拟后端与短步长，**不覆盖真实后端与长加载**。
- **准入前置**：必须提供真实模型形状、本分片权重字节与上下文长度（来源应为 LC-9 manifest + LC-5 契约）；缺任一即抛 `MissingModelMetadata` 拒绝接单，不使用占位形状。
- **释放语义**：`load()` 被包在 `try/finally` 内，`load` 抛错也会调用 `release()`；适配器必须保证 `release()` 幂等且可在加载失败后调用。取消发生在任何 `load` 之前时不获取资源，也就不需要释放。
- 层区间沿用 Controller 与引擎一致的**左闭右开、从 0 开始**：`[layer_start, layer_end)`。
- 不实现自己的调度、不持有 Controller 管理 token；屏幕界面只应调用本地适配接口。

## 已知限制

- 参考后端为确定性**模拟**实现，只验证适配器生命周期与取消语义，**不产生真实推理结果**；它不在生产默认候选里，仅由测试显式注入或 `--reference-backend` 显式开启。
- 未在 Android 真机/模拟器验证；`FEASIBILITY.md` 已单列具体阻塞。
- 电量/发热在真机上目前只能读内核接口（机型差异大），框架 API 通道（`PowerManager` / `BatteryManager`）尚不存在；温度→状态阈值是启发式，待校准。sysfs 的 `Not charging` 不视为充电，因而不享受低电量豁免。
- ONNX Runtime Mobile 后端仅做可用性探测，环境无该依赖时抛 `BackendUnavailable`。
- 1 GiB 上限检查在 macOS 上**无法硬限**（`ulimit -v` EINVAL），只能测量峰值 RSS；硬限路径仅在支持 `RLIMIT_AS` 的内核上生效。
