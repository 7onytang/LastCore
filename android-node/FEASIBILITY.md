# Android 节点后端可行性与最小方案（LC-13 phase 1）

业务时间：2026-10-11（Asia/Shanghai）。
范围：仅 Android 节点的**后端选型、模型格式、许可/维护、arm64 与资源预算**，以及可验证的最小 `BackendAdapter` 方案与阻塞清单。不含真机 demo、不含桌面后端与云端契约改动。

## 0. 结论摘要

- **推荐后端**：`ONNX Runtime Mobile`（MIT）为首选，`ExecuTorch`（BSD-3）为备选；两者都需把引擎的分层模型导出成移动端可加载的格式，**该导出能力当前引擎不存在**，属需 LC-5 冻结 + 负责人批准的事项。
- **推荐模型格式**：导出为 **ONNX（每层片段一个可独立加载的子图）**，量化走 `float16`（保真）或 `int8`（省内存，需误差评估）。
- **arm64**：目标 ABI 仅 `arm64-v8a`（NEON + fp16）；不打包 32 位。
- **内存准入**：以「可用内存 × 0.6 × (1 − headroom)」为上限，权重 + 激活 + KV + 运行时开销一并计入；低电量/发热降级拒绝接单。
- **阻塞**：本环境无 Android 真机/SDK/NDK；LC-5 契约未冻结；LC-12 桌面节点固定成果未交付；引擎缺 ONNX/ExecuTorch 导出路径；`communication/` 依赖 numpy + 原始 socket，移动端需专门移植决策。详见 §6。

## 1. 候选后端矩阵

| 后端 | 许可 | 维护 | arm64 | 模型格式 | 量化 | 适配我们分层模型 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ONNX Runtime Mobile | MIT | 活跃（微软） | 是（NEON/fp16/INT8，NNAPI/XNNPACK EP） | ONNX | fp16 / int8 | 需引擎提供逐段导出；运行时按段加载天然契合 `[start,end)` | 首选：许可宽松、依赖成熟、APK 增量可控 |
| ExecuTorch (PyTorch) | BSD-3 | 活跃（Meta） | 是（XNNPACK/Vulkan，实验性） | `.pte` | fp16 / int8 | 需用 PyTorch 前端重述模型并逐段 export | 备选：与 PyTorch 生态近，但工具链较新 |
| llama.cpp / GGUF | MIT | 活跃 | 是（NEON，小型模型 CPU 可跑） | GGUF | k-quant | 仅支持其内置架构，**自定义分层引擎难注入**；分片需自持 | 适合端上整模型，不适合我们的分片调度 |
| MLC-LLM | Apache-2.0 | 活跃 | 是（Vulkan/OpenCL） | 编译产物 | 多种 | 面向 GPU、整模型 TVM 编译 | 偏离「按层分片」，成本高 |
| LiteRT（原 TFLite） | Apache-2.0 | 活跃（Google） | 是 | `.tflite` | int8/fp16 | 转换链偏 Keras，自定义 decoder 导出成本高 | 备选 |
| Chaquopy（Android 上的 CPython） | MIT（商用需授权） | 活跃 | 是 | 任意 Python | — | 可直接跑 numpy 版 `communication/` | 仅供「最小验证」捷径；**商用许可与体积需负责人批准**，不作为交付默认 |

> 说明：版本号与维护状态需在选定后固定到具体 release，并在 LC-5 冻结；本表用于初筛，不代替负责人对**后端/模型转换重大选择**的批准。

## 2. 模型格式与分层兼容性

- 现有引擎按 `[layer_start, layer_end)` 分段，首段含 embedding、末段含 final LN + `lm_head`，中间段为连续 decoder block（见 `node-agent/PROTOCOL.md`）。
- 移动端后端要能**只加载本段权重**：ONNX 需按段导出独立 `GraphProto`（含该段算子与常量），或在单图内用可裁剪子图；ExecuTorch 需按段 `export`。
- 与真机对齐的验证模型：先用引擎自带的 tiny checkpoint（4 层、`d_model=32`、词表 64，可逐字节对照单机）验证链路，再换 `Qwen2.5-0.5B` 量级。
- **转换误差**：fp16/int8 量化会改变数值，需以单机 fp32 结果为基线给出误差上界；未评估前不得声称等价。

## 3. arm64 与运行时

- 仅 `arm64-v8a`；启用 NEON，fp16 需运行时支持（`onnxruntime` 的 fp16 算子覆盖有限，需逐算子核对）。
- NNAPI/XNNPACK 加速可用性因机型而异，**不作为正确性前提**；CPU EP 必须可回退。
- 线程数受限于前台单进程：建议 `intra_op = min(4, big cores)`，避免热降频。

## 4. 资源预算模型（已实现在 `budget.py`）

一次片段驻留需要：

```
peak ≈ weights(shard) + activation + KV + runtime_overhead
activation = batch × tokens × d_model × dtype_bytes
KV         = 2 × shard_layers × batch × tokens × d_model × dtype_bytes
```

准入判定（`evaluate`）：`peak × (1 + headroom) ≤ usable_memory × max_memory_ratio`，再叠加电量与温度策略。默认 `max_memory_ratio=0.6`、`headroom=0.15`、`min_battery_percent=20`。

- **热**：`serious`/`critical` 直接拒绝接单（前台长时间满载会触发降频，继续执行只会拖垮任务）。
- **电**：低于阈值且未充电时拒绝接单；不申请 `FOREGROUND_SERVICE`，不做后台常驻。
- **权限**：仅 `INTERNET`。不申请存储/位置/后台等敏感权限。

## 5. 最小 BackendAdapter 方案（本目录已实现）

```
AndroidExecutor (实现 node-agent Executor 契约)
   │  per-assignment 新实例，run() 阻塞至片段完成
   ├─ detect() → DeviceProfile
   ├─ build_budget() + evaluate() → ACCEPT / REJECT_*
   ├─ select_backend(profile, adapters) → BackendAdapter
   │        load(segment, weights) → run(tokens) → release()
   └─ 每 ≤1s 检查 context.cancel / deadline，异常路径 release()
```

`BackendAdapter` 只做三件事（`load` / `run` / `release`），把「加载哪一段、跑什么」交给上层，保证与引擎/桌面后端解耦，也不复制 `inference-engine`。

## 6. 具体阻塞（需 Lead / 负责人决策或提供）

1. **无 Android 真机 / SDK / NDK**：本环境 `adb`、`sdkmanager`、`gradle` 均缺失，无法产出真机证据；按 LC-13 要求以设计与阻塞代替，**不声称手机分布式推理已完成**。
2. **LC-5 契约未冻结**：传输凭据、dtype/布局、ACK/背压（见 `node-agent/PROTOCOL.md` §2.5）未定，Android 端 suspend。
3. **LC-12 桌面节点固定成果未交付**：LC-13 的 demo 阶段依赖其固定 SHA，尚不具备。
4. **引擎缺移动端导出路径**：`inference-engine/` 当前只提供 numpy 参考实现，无 ONNX/ExecuTorch 导出；这是后端选型的前置，属跨模块接口，须先评审。
5. **`communication/` 移动端可行性**：其帧编解码基于 numpy + 原始 TCP，移动端要么引入 Chaquopy（商用许可需批准），要么在 communication 内做主端口（不得由 Android 目录私自复制）。
6. **后端/模型转换属重大选择**：LC-13 要求「由 Lead 上报负责人批准后再做前台小模型或分片执行 demo」，故本阶段不落地真机执行。

## 7. 未验证项

- Android 真机/模拟器上的加载、执行、取消、网络切换、退出清理 —— **未验证**。
- ONNX/ExecuTorch 导出与量化误差 —— **未验证**（工具缺失）。
- Windows/macOS 桌面节点 —— 归 LC-12（Engineer-A），本目录不涉及。
