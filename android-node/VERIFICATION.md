# Android 节点可执行验证步骤（LC-13 phase 1）

业务时间：2026-10-11（Asia/Shanghai）。配套设计见 `FEASIBILITY.md`。

分三部分：**A 无设备即可执行（已在本机跑过，附真实输出）**、**B 需要真机/SDK（列出前置与命令，本环境缺设备未执行）**、**C 证据表与判定标准**。

本轮（返工后）对应审核六项的实现与验证见 **A0**。

---

## A0. 返工对应（LC-44 审核六项）

| 审核发现 | 修正 | 覆盖验证 |
| --- | --- | --- |
| 1 默认路径会返回模拟推理结果（`backend.py` 默认候选含 `ReferenceBackend`） | `default_adapters(include_reference=False)` 不再含模拟后端；模拟后端仅测试注入或 CLI `--reference-backend` 显式开启；无真实运行时即抛 `BackendUnavailable` | `test_default_adapters_exclude_the_simulated_double`、`test_production_default_fails_closed_without_a_real_runtime`、`test_no_default_adapter_is_used_when_none_is_configured` |
| 2 默认准入预算不代表真实分片（玩具形状/0 字节/tokens=1） | 移除 `DEFAULT_SHAPE`；形状、分片权重字节、上下文长度必填，缺失抛 `MissingModelMetadata` 拒绝；CLI `--weights-bytes`/`--tokens` 改必填正整数（`1` 合法），缺失/`0`/负数/非整数非零退出且不 accept | `test_missing_metadata_refuses_admission`、`tests/test_cli.py`（真实子进程负例） |
| 3 sysfs `Not charging` 被当作充电，低电量误放行 | `_CHARGING_STATES` 仅 `charging`/`full`；`Not charging`/`Discharging`/未知不享受豁免 | `test_sysfs_charging_mapping`（5 种状态）、`test_unknown_charging_state_does_not_grant_the_exemption`、CLI 断言用例 |
| 4 `load()` 在 `try/finally` 之外，加载异常不释放 | `load()` 移入 `try`，加载抛错也 `release()`；取消发生在 load 前则不获取资源 | `test_load_failure_still_releases` |
| 4b 清理失败遮蔽主要 load/run/取消异常（LC-61） | `release()` 失败不再替换主错误：主错误（load/run/取消）照常抛出，清理错误作为 `add_note`/`cleanup_error` 诊断附加；**仅**无主错误的 release 失败才向上传播 | `test_load_failure_not_masked_by_release_failure`、`test_run_failure_not_masked_by_release_failure`、`test_cancellation_not_masked_by_release_failure`、`test_release_failure_without_primary_failure_is_not_swallowed` |
| 5 ≤1 秒取消承诺超出约束（`step_seconds` 无上限、`load` 无边界） | 强制 `step_seconds ≤ poll_seconds`；load 前/后与每次 run 前后都有 guard；文档与测试范围限定为「模拟后端 + 短步长」 | `test_step_seconds_cannot_exceed_the_poll_interval`、`test_cancelled_before_start_raises_without_loading`、契约测试的取消/期限用例 |
| 6 验证脚本 `|| true` 吞掉 CLI 失败 | `verify_offdevice.sh` 改为解析结构化 `decision` 并同时断言退出码，不符即 FAIL；无 `|| true` | 脚本本身（本轮 6 个准入用例全部断言通过：3×accept、3×reject_*，见 A3） |

---

## A. 无设备即可执行的部分（本轮已执行）

### A1. 单元测试

```bash
cd android-node
python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements-dev.txt
./.venv/bin/python -m pytest -q
```

实测结果（2026-10-12，LC-62 返工后）：`55 passed, 8 skipped`（8 个 skip 是需 node-agent 检出的契约测试；本轮新增 `tests/test_cli.py` 9 个真实子进程负例与 `test_executor.py` 4 个清理异常用例）。

### A2. 与 node-agent 真实 Executor 契约的联调（跨组件，仍无需设备）

把 Engineer-A 的 `node-agent/` 检出目录作为 `LASTCORE_NODE_AGENT_PATH`；基线固定为
`agent/engineer-a-a/b33d0fac979a`（`ba52895ae829dcf0c8c605de46103e3b25fad650`，即 LC-52/LC-54 固定的 node-agent 版本）：

```bash
LASTCORE_NODE_AGENT_PATH=/path/to/node-agent ./.venv/bin/python -m pytest -q
```

实测结果：`63 passed`。这一条**证明的是真实接口而非模拟**：

- `AndroidExecutor` 是 `lastcore_node_agent.executor.Executor` 的真子类（node-agent 的 `isinstance` 门禁会接受它）；
- `load_executor("lastcore_android_node.executor:executor_factory")` 能返回可用实例（与 `node-agent --executor` 参数一致）；
- 用**真实的** `ExecutionContext` 驱动 run / cancel / deadline：
  - 正常路径 `load=1, run=1, release=1`，段被释放；
  - 取消/期限发生在启动前 → `load=0`（未获取资源，无需释放）；
  - 运行中取消（独立线程）→ 在 1 s 契约内抛出并 `release=1`；
  - 两个 assignment 各自新实例，状态不串；
- 边界检查：`android-node/` 不 import `inference_engine` / `communication` / node-agent 编排模块（不复制引擎）。

> 注意顺序约束：`lastcore_android_node.executor` 在**导入时**绑定 `Executor` / `ExecutionCancelled` 基类，因此必须先让 `node-agent` 可导入。真实 node-agent 宿主进程天然满足（它先导入自己的包，再 `--executor` 加载本模块）；测试用 `conftest.py` 复现该顺序。

### A3. 准入/预算判定（CLI）

```bash
# 可接受
LASTCORE_ANDROID_MACHINE=arm64 LASTCORE_ANDROID_PLATFORM=Android LASTCORE_ANDROID_IS_ANDROID=true \
LASTCORE_ANDROID_TOTAL_MEMORY_MB=6144 LASTCORE_ANDROID_AVAILABLE_MEMORY_MB=2560 \
LASTCORE_ANDROID_THERMAL=nominal \
./.venv/bin/python -m lastcore_android_node plan --layers 0:12 --layers-total 24 \
  --d-model 896 --heads 14 --vocab 151936 --weights-bytes 616000000 --tokens 2048
```

实测结果：

| 场景 | 输入 | 决定 | 理由 |
| --- | --- | --- | --- |
| 6GB 机、2.5GB 可用，段 `[0,12)` 588MB fp16 | 见上 | `accept` | peak 803MB ≤ 1536MB 上限 |
| 同机器，段 `[12,24)` 328MB fp16 | — | `accept` | peak 544MB ≤ 1536MB 上限 |
| 仅 700MB 可用 | 同上段 | `reject_memory` | 含 headroom 需 924MB > 上限 420MB |
| 温度 `serious` | `LASTCORE_ANDROID_THERMAL=serious` | `reject_thermal` | 前台满载继续执行只会拖垮任务 |
| 电量 10% 且未充电 | `LASTCORE_ANDROID_BATTERY_PERCENT=10 CHARGING=false` | `reject_battery` | 未确认正在充电，不满足充电豁免 |
| 电量 10% 且充电中 | `... BATTERY_PERCENT=10 CHARGING=true` | `accept` | 充电中不拦；对照上一行证明确实是「充电状态」在起作用 |

一键跑齐（含上面所有步骤 + 1 GiB 检查）：

```bash
LASTCORE_NODE_AGENT_PATH=/path/to/node-agent scripts/verify_offdevice.sh
```

脚本对准入命令**同时断言结构化 `decision` 与退出码**（`accept`→0、`reject_*`→1），不再用 `|| true` 吞错误；上一轮六个用例（含 `Not charging` 低电量必须 `reject_battery`、充电中同电量 `accept`）全部通过。本轮新增 **3b 元数据必填负例**：缺权重、缺 tokens、两者皆缺、显式 `0`、负数共 7 条断言「非零退出且不输出 accept」（`rc=2`，argparse 拒绝），并断言显式正整数 `--tokens 1` 作为真实上下文长度仍 `accept rc=0`。LC-61 复现命令 `--layers 0:1 --layers-total 4 --d-model 32 --heads 4 --vocab 64` 现为 `rc=2` 且无 `accept`。

### A4. 1 GiB 内存上限检查

```bash
scripts/check_memory_ceiling.sh suite
```

实测（2026-10-12，Darwin）：

```
-- memory guarantee: MEASURED only. Darwin rejects lowering RLIMIT_AS
   (ulimit -v -> EINVAL / 'cannot modify limit: Invalid argument'), so this run is NOT hard-capped
-- command exit code: 0
-- peak RSS: 38256640 bytes (36 MiB), ceiling 1024 MiB
-- OK: peak RSS under the 1 GiB ceiling
```

即：本机**不能**施加 1 GiB 硬限（与 LC-44 审核复现的 EINVAL 一致），脚本因此退化为
**测峰值 RSS 并断言 < 1 GiB**，并在输出里明示「非硬限」。内核支持 `RLIMIT_AS` 时（Linux）
脚本会自动走硬限路径。二者结论不得互相替代。

---

## B. 需要真机 / Android SDK（本环境缺失，未执行）

### B0. 前置检查（先跑，缺项即阻塞）

```bash
command -v adb sdkmanager gradle java          # 本机全部缺失（java 仅存桩）
adb devices                                    # 需出现 "device"（已授权），emulator 不算
```

工具链（择一获取；本项未安装，未验证）：

- Android SDK platform-tools（提供 `adb`）：`developer.android.com/tools/releases/platform-tools`
- NDK **r28b+**（16 KB 页对齐、LiteRT-LM 构建要求）：`developer.android.com/ndk/downloads`
- JDK 17（Gradle/AGP 需要）

### B1. 设备事实采集 + 准入判定

```bash
scripts/probe_device.sh --layers 0:24 --layers-total 24 \
  --d-model 896 --heads 14 --vocab 151936 --weights-bytes 1230000000 --tokens 2048
```

脚本会 `adb` 读取：`ro.product.cpu.abi`、`ro.build.version.sdk/release`、`ro.product.model`、`ro.soc.model`、
`getconf PAGE_SIZE`、`/proc/meminfo`（MemTotal/MemAvailable）、`/sys/class/power_supply/battery/{capacity,status}`、
`/sys/class/thermal/thermal_zone*/temp`，并据此设置 `LASTCORE_ANDROID_*` 后调用准入规划器。

判定标准：`abi=arm64-v8a`；`page_size=16384` 时自带的 `.so` 必须 16 KB 对齐；决定为 `accept` 且理由行显示真实内存/温度。

### B2. 真机执行阶段（**阻塞中**：需先有 Android 应用壳/后端适配器）

LC-13 要求覆盖：**前台执行、取消、网络变化、资源不足、应用退出清理**，并给出耗时/内存/耗电观察。
当前不存在可安装的 Android 运行壳，也不存在真机 `BackendAdapter`（引擎无 ONNX/ExecuTorch 导出，且后端选择属重大事项待批），因此以下命令是**待具备后的执行清单**，本轮未执行、不声明已完成：

| 用例 | 前置 | 命令/操作 | 通过判定 | 状态 |
| --- | --- | --- | --- | --- |
| 前台小模型执行 | app 壳 + tiny 检查点 + ONNX 段 | 启动前台任务，`adb logcat -s LastCoreNode` 观察 | 片段完成并回传结果；与单机 fp32 基线误差 ≤ 标定上界 | 阻塞（无壳/无导出） |
| 取消 | 长片段 | 界面/API 取消后 `adb logcat` 看 `ExecutionCancelled` | ≤1 s 内停止，`release()` 后 RSS 回落 | 阻塞 |
| 资源不足 | 人为放大 `--weights-bytes` | 观察拒绝与提示 | 显式 `reject_memory`，不静默改为后台/不崩 | 阻塞 |
| 网络变化 | 关 Wi-Fi/切飞行模式 | 观察入网状态与重连 | 显式断连提示；恢复后重连；不假成功 | 阻塞 |
| 应用退出清理 | 前台任务中杀进程 | `adb shell am force-stop` + `dumpsys meminfo` | 进程内无残留线程/句柄，RSS 释放 | 阻塞 |
| 内存/耗电观测 | 真机 | `dumpsys meminfo <pkg>`、`dumpsys batterystats` | 记录峰值 RSS 与耗电，与预算对比 | 阻塞 |
| 热/电量策略 | 真机 | 真机温度变化或 `LASTCORE_ANDROID_THERMAL=serious` 注入 | 达到 serious/低电量时拒绝新任务 | 部分可注入验证，真机曲线阻塞 |

### B3. sysfs 与框架 API 的差异核对（真机）

在真机上比较内核读数与框架 API：

```bash
adb shell dumpsys thermalservice | head -40      # 权威 ThermalStatus
adb shell dumpsys battery | grep -E 'level|status'
adb shell 'cat /sys/class/thermal/thermal_zone*/temp' | sort -n | tail -3
```

如果两者不一致（常见：sysfs 不含 skin/SoC 判据），必须改用框架 API 并校准 45/50/60 ℃ 启发式阈值。
**本项未执行**（无设备）。

---

## C. 证据表（交付时逐条填写）

| 声明 | 证据类型 | 本轮状态 |
| --- | --- | --- |
| BackendAdapter 最小方案可运行、取消/释放语义正确 | 无设备单测 + 真实 node-agent 契约联调（命令与输出见 A1/A2） | ✅ 已执行 |
| 内存/热/电量准入策略正确 | CLI 判定表（A3） | ✅ 已执行 |
| 许可/工具链/格式事实 | 公开资料核实（`FEASIBILITY.md` §1、§3，含来源与置信度） | ✅ 已核实；具体 release 待选定后固定 |
| Android 真机执行结果、耗时/内存/耗电 | 真机日志 + `dumpsys` | ❌ 未执行（无真机/SDK/后端导出） |
| 手机分布式推理可用 | — | ❌ 不声明（本阶段明确不做） |

证据留存建议：真机日志用 `adb logcat -d > evidence/<date>/logcat.txt`，内存 `dumpsys meminfo` 同目录留存；本文件与 `FEASIBILITY.md` 一并附件交付。
