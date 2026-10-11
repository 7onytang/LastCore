# Android 节点后端可行性与最小方案（LC-13 phase 1）

业务时间：2026-10-11（Asia/Shanghai）。
范围：仅 Android 节点的**后端选型、模型格式、许可/维护、arm64 与资源预算**，以及可验证的最小 `BackendAdapter` 方案与阻塞清单。不含真机 demo、不含桌面后端与云端契约改动。

公开资料核实时间：2026-10-11。每条事实标注来源与置信度；**具体 release 版本号需在选定后固定，并在 LC-5 冻结**，本表不代替负责人对「后端/模型转换重大选择」的批准。

## 0. 结论摘要

- **推荐后端**：`ONNX Runtime Mobile`（MIT）为首选，`ExecuTorch`（BSD-3）为备选；两者都需把引擎的分层模型导出成移动端可加载的格式，**该导出能力当前引擎不存在**，属需 LC-5 冻结 + 负责人批准的事项。
- **推荐模型格式**：导出为 **ONNX（每层片段一个可独立加载的子图）**，量化走 `float16`（保真）或 `int8`（省内存，需误差评估）。
- **arm64**：目标 ABI 仅 `arm64-v8a`（NEON + fp16）；不打包 32 位。
- **工具链**：NDK **r28b 或更高**（r28 起默认 16 KB 页对齐）；若将来要上 Google Play，Android 15+ 目标必须支持 64 位 16 KB 页。
- **内存准入**：以「可用内存 × 0.6 × (1 − headroom)」为上限，权重 + 激活 + KV + 运行时开销一并计入；低电量/发热降级拒绝接单。
- **阻塞**：本环境无 Android 真机/SDK/NDK（`adb`/`sdkmanager`/`gradle`/`cmake`/`java` 均不可用，见 §7 证据）；LC-5 契约未冻结；LC-12 桌面节点固定成果未交付；引擎缺 ONNX/ExecuTorch 导出路径；`communication/` 依赖 numpy + 原始 socket，移动端需专门移植决策。

## 1. 候选后端矩阵（已核实公开事实）

| 后端 | 许可 | 维护 | arm64 | 模型格式 | 量化 | 适配我们分层模型 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ONNX Runtime Mobile | MIT | 活跃（微软） | 是（NEON/fp16/INT8，NNAPI/XNNPACK EP） | ONNX | fp16 / int8 | **可**：按段导出独立 `GraphProto`，运行时按段加载，天然契合 `[start,end)` | 首选：许可宽松、依赖成熟 |
| ExecuTorch (PyTorch) | BSD-3 | 活跃（Meta），**2025 年底发布 v1.0** | 是（12+ 后端：Apple/Qualcomm/Arm/MediaTek/Vulkan…） | `.pte` | fp16 / int8 | **可**：按段 `export`；运行时基座约 50 KB | 备选：与 PyTorch 生态近，量化流程较 GGUF 繁 |
| llama.cpp / GGUF | MIT | 活跃（ggml.ai 社区，~96k stars） | 是（NEON，小模型 CPU 可跑） | GGUF | k-quant / Q8_0 / MXFP4 | **不可**：只跑内置图结构，无法注入我们的分层片段 | CPU-first、可移植性最好，但与本项目「按层分片」模型不匹配 |
| MLC-LLM / MLC Chat | Apache-2.0 | 活跃（MLC AI 团队） | 是，但走 GPU（Android OpenCL/Vulkan） | TVM 编译产物 | 多种 | 需整模型 AOT 编译，偏离「按层分片」 | Android 上 OpenCL 支持因机型而异，Pixel 上已知不可靠 |
| LiteRT-LM（TFLite 后继） | Apache-2.0 | 活跃（Google），Android 端首选 | 是（NNAPI + 厂商 delegate） | LiteRT（`.tflite` 后继） | int8 / fp16 | 转换链面向 Google 发布的整模型（Gemma 级） | 整模型移动端最优；NPU 需厂商 dispatch（如 MediaTek），构建要求 NDK r28b+ |
| Chaquopy（Android 上的 CPython） | MIT（商用需授权） | 活跃 | 是 | 任意 Python | — | 可直接跑 numpy 版 `communication/` | 仅供「最小验证」捷径；**商用许可与体积需负责人批准**，不作为交付默认 |

事实来源与置信度：

- **LiteRT-LM 构建要求 NDK r28b+**、支持 CPU/GPU/NPU（QAIRT 2.42 / MediaTek dispatch）——官方文档 `ai.google.dev/edge/litert`（高置信，2026-05-19 抓取）。
- **Android NDK r28（2025-02）起默认 16 KB 页对齐**、Android 15（API 35）+ 在 64 位设备须支持 16 KB 页、Play 自 **2027-02-01** 起强制——官方文档 `developer.android.com/guide/practices/page-sizes`（高置信，2026-09-17 抓取）。
- **ExecuTorch 许可 BSD-3、`.pte` 格式、v1.0（2025 年底）、12+ 硬件后端、约 50 KB 基座**——社区汇总（`edgeaistack.ai` 2026-07、`karam-nus.github.io` 2026-03；中置信，选型前需以官方 release 复核）。
- **llama.cpp MIT / GGUF / 96k stars；MLC-LLM Apache-2.0 / 编译产线 / Android 走 OpenCL 且机型不一致；ONNX Runtime 许可 MIT、需模型转换**——社区汇总（中置信；licence 与官方仓库一致，版本号未逐一核到官方 release 页面）。
- 未核到官方 release 页的具体版本号（本环境对 `github.com` 的 HTTPS 出网超时），因此 §1 **只给性质与许可，不写版本号**；选定后必须固定到具体 release 再进 LC-5。

## 2. 模型格式与分层兼容性

- 现有引擎按 `[layer_start, layer_end)` 分段，首段含 embedding、末段含 final LN + `lm_head`，中间段为连续 decoder block（见 `node-agent/PROTOCOL.md`）。
- 移动端后端要能**只加载本段权重**：ONNX 需按段导出独立 `GraphProto`（含该段算子与常量），或在单图内用可裁剪子图；ExecuTorch 需按段 `export`。
- **首/末段权重显著大于中间段**（embedding 与 lm_head 各占一份大矩阵）：以 Qwen2.5-0.5B 量级（24 层、`d_model=896`、`vocab=151936`、fp16）估算，embedding ≈ 260 MB、单个 decoder 层 ≈ 27 MB → 段 `[0,12)` ≈ 588 MB、段 `[12,24)` ≈ 328 MB。**按层数平均分配预算会低估首段**，准入必须以真实分片字节数为准。
- 与真机对齐的验证模型：先用引擎自带的 tiny checkpoint（4 层、`d_model=32`、词表 64，可逐字节对照单机）验证链路，再换 `Qwen2.5-0.5B` 量级。
- **转换误差**：fp16/int8 量化会改变数值，需以单机 fp32 结果为基线给出误差上界；未评估前不得声称等价。

## 3. arm64、工具链与真机条件

- 仅 `arm64-v8a`；启用 NEON，fp16 需运行时支持（`onnxruntime` 的 fp16 算子覆盖有限，需逐算子核对）。
- **NDK**：`r28b` 或更高（LiteRT-LM 构建要求；r28 起默认 16 KB 对齐）。NDK r28 发布于 2025-02。
- **16 KB 页**：Android 15+ 设备可能出现 `PAGE_SIZE=16384`；所有自带 `.so` 必须 16 KB 对齐（NDK r28+ 默认），否则在这些设备上无法加载。`scripts/probe_device.sh` 会打印 `page_size` 并按此给出提示。
- **minSdk 建议 29（Android 10）**：`PowerManager.getCurrentThermalStatus()` 自 API 29 起可用，是发热准入的权威来源；低于 29 只能用 sysfs 启发式。最终取值需 LC-5 确认（本项属契约/权限边界）。
- **NNAPI/XNNPACK 加速因机型而异**，**不作为正确性前提**；CPU EP 必须可回退。
- 线程数受限于前台单进程：建议 `intra_op = min(4, big cores)`，避免热降频。
- **权限**：仅 `INTERNET`。不申请存储/位置/后台等敏感权限；无后台常驻、无 `FOREGROUND_SERVICE`。
- **发热与电池的权威来源是 Android 框架 API**：`PowerManager.getCurrentThermalStatus()`（API 29+）与 `BatteryManager`。当前实现只能读到内核接口（`/proc/meminfo`、`/sys/class/power_supply/*`、`/sys/class/thermal/*`，免权限、机型差异大），**温度→状态映射 45/50/60 ℃ 只是本地启发式**（可用 `LASTCORE_ANDROID_THERMAL_*_C` 调），必须由框架 API 校准后才能作为验收依据。JNI/平台通道由 LC-14 或后续 Android 应用壳提供，**当前不存在**。

## 4. 资源预算模型（已实现在 `budget.py`）

一次片段驻留需要：

```
peak ≈ weights(shard) + activation + KV + runtime_overhead
activation = batch × tokens × d_model × dtype_bytes
KV         = 2 × shard_layers × batch × tokens × d_model × dtype_bytes
```

准入判定（`evaluate`）：`peak × (1 + headroom) ≤ usable_memory × max_memory_ratio`，再叠加电量与温度策略。默认 `max_memory_ratio=0.6`、`headroom=0.15`、`min_battery_percent=20`。

- **热**：`serious`/`critical` 直接拒绝接单（前台长时间满载会触发降频，继续执行只会拖垮任务）。
- **电**：低于阈值且未充电时拒绝接单。
- **未落地项**：真实 KV 增长是**逐 token**的（本次按最大上下文一次计入，偏保守）；`runtime_overhead=128MB` 是待实测的占位值；未做真机热/电量曲线实测。

## 5. 最小 BackendAdapter 方案（本目录已实现）

```
AndroidExecutor (实现 node-agent Executor 契约)
   │  per-assignment 新实例，run() 阻塞至片段完成
   ├─ detect() → DeviceProfile
   ├─ build_budget() + evaluate() → ACCEPT / REJECT_*
   ├─ select_backend(profile, adapters) → BackendAdapter
   │        load(segment, weights) → run(tokens) → release()
   └─ 每步之间检查 context.cancel / deadline，异常路径 release()
```

`BackendAdapter` 只做三件事（`load` / `run` / `release`），把「加载哪一段、跑什么」交给上层，保证与引擎/桌面后端解耦，也不复制 `inference-engine`。

**取消粒度契约（真机适配器必须遵守）**：执行器只在两次 `BackendAdapter.run` 之间检查 `context.cancel` / deadline，因此可保证的轮询间隔是 `max(单次 run 耗时, step_seconds)`。真机适配器必须把一次 `run` 切成 ≤1 s 返回的分块，否则一段长时间阻塞的 `session.run()` 无法被取消，会违反 node-agent「至少每秒检查一次取消」的契约。当前参考后端是即时返回的模拟实现，不构成真机证据。

## 6. 具体阻塞（需 Lead / 负责人决策或提供）

1. **无 Android 真机 / SDK / NDK**：本环境 `adb`、`sdkmanager`、`gradle`、`cmake`、`ninja`、`java` 运行时全部缺失（证据见 §7），无已授权设备，无法产出真机证据；按 LC-13 要求以设计与阻塞代替，**不声称手机分布式推理已完成**，也不用模拟器结果冒充真机。
2. **LC-5 契约未冻结**：传输凭据、dtype/布局、ACK/背压（见 `node-agent/PROTOCOL.md` §2.5）未定；minSdk 与权限边界同属此类。
3. **LC-12 桌面节点固定成果未交付**：LC-13 的 demo 阶段依赖其固定 SHA，尚不具备。
4. **引擎缺移动端导出路径**：`inference-engine/` 当前只提供 numpy 参考实现，无 ONNX/ExecuTorch 导出；这是后端选型的前置，属跨模块接口，须先评审。
5. **`communication/` 移动端可行性**：其帧编解码基于 numpy + 原始 TCP，移动端要么引入 Chaquopy（商用许可需批准），要么在 communication 内做主端口（不得由 Android 目录私自复制）。
6. **框架 API 通道缺失**：电池/发热的权威值需要 JNI/平台通道（LC-14 或应用壳），当前只能读 sysfs，属未验证来源。
7. **后端/模型转换属重大选择**：LC-13 要求「由 Lead 上报负责人批准后再做前台小模型或分片执行 demo」，故本阶段不落地真机执行。

## 7. 本环境核实证据（2026-10-11，命令与结果）

```
$ uname -m && sw_vers
arm64
ProductName:  macOS  ProductVersion: 15.8.1

$ for t in adb sdkmanager gradle cmake ninja ndk-build java; do command -v $t; done
adb MISSING / sdkmanager MISSING / gradle MISSING / cmake MISSING / ninja MISSING / ndk-build MISSING
java -> /usr/bin/java 但 java -version 报 "Unable to locate a Java Runtime"

$ ANDROID_HOME= (empty)  ANDROID_SDK_ROOT= (empty)  ~/Library/Android/sdk 不存在

$ scripts/probe_device.sh --layers 0:2 ...
BLOCKED: adb is not installed; no real Android device can be probed.   (exit 2)
```

即：**开发机为 arm64 macOS（可跑 arm64 准入逻辑），但没有 Android 工具链、没有真机**。可执行的验证步骤与预期结果见 `VERIFICATION.md`。

## 8. 未验证项

- Android 真机/模拟器上的加载、执行、取消、网络切换、退出清理 —— **未验证**。
- ONNX/ExecuTorch 导出与量化误差 —— **未验证**（工具缺失）。
- 真机热/电量曲线、KV 实测内存、`runtime_overhead` 常量 —— **未验证**。
- Windows/macOS 桌面节点 —— 归 LC-12（Engineer-A），本目录不涉及。
