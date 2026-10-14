# LastCore 正式提交材料索引

队伍：**Tibo重置了吗**。本文按项目和已提供的六维评分要求整理，不用赛题名称或开赛时间作为材料准备的阻塞，也不作未经核验的赛事时间合规结论。

正式 r2 材料及后续完整源码补充 ZIP 已分别公开发布。原 `a6` ZIP 字节不变；正式 v0.1.1 已公开发布，源提交 `bf5b82e37636d344959d06945c3a6a9e7dfd74ac`；本索引不表示邮件已经发送。

已公开的[完整源码 ZIP](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCore-v0.1.1-complete-source.zip)固定源码 `6977c0919c9df3323cb0ec52e2b8820646df8233`，包含 Windows/Linux 客户端源码、README、AI 使用说明、公开对话节选、8 张原始截图、许可证和作者历史。[SHA256](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCore-v0.1.1-complete-source.zip.sha256) 为 `03c58948ecff6918fabdc9d019421936cfd2c71dfc5d71fe8c28672fe5bb3721`；[manifest](https://github.com/7onytang/LastCore/releases/download/v0.1.1/COMPLETE_SOURCE_MANIFEST.json)记录固定文件摘要与范围。正式核心标签仍为 `bf5b82e`，原 r2 包仍固定 `ac5d630`；源码补充包及此次文档更新不改变既有归档，不新增运行验收。

## 实现与真实证据

| 路径 | 当前已确认范围 | 可引用文件 |
| --- | --- | --- |
| 原生 Mac 客户端 | LastCoreClient 0.1.1 在用户真实 MacBook Pro 上完成单节点线性任务；服务端结果核验与用户 GUI 数值确认通过 | [公开 Mac 验收](evidence/mac-client-0.1.1-linear-acceptance.md)；修复源 `f9139762` 的 `controller-server/app/models.py` 与 Mac 客户端源码 |
| 真实小模型 | 本轮公网 HTTPS Controller 到 Mac mini CPU 整模型 worker，生成 Paris 摘要并确认停止和原生进程清理 | [本轮公开摘要](evidence/public-gguf-f913-generation-summary.json)、[真实模型运行入口](../real-model-runtime/README.md)、[worker](../real-model-runtime/worker.py) |
| tiny 分层原型 | 既有本机多进程 tiny 链路及单机参考对照；不得归并为预训练大模型跨物理设备执行 | [双节点脚本](../integration/run_two_node_local.sh)、[引擎协议](../inference-engine/PROTOCOL.md)、[原 Alpha 验收](evidence/ACCEPTANCE.txt) |
| Android | JNI/主机与 APK 构建有证据，真机验收仍延期 | [Android 说明](../android-app/README.md)、[原验收范围](evidence/ACCEPTANCE.txt) |
| Windows / Linux 边缘客户端 | 已发布未签名有界线性开发预览；源 `85a1e232` 的原生构建和静态检查通过；GUI、联网、用户设备与 relay 未验 | [预览下载、来源及安装说明](../edge-client/README.md)；不执行 GGUF 或 tiny Stage |
| 其他边缘设备 | 适配开发中，未列具体已验收设备 | 根 [README](../README.md) 的实现范围 |

服务端 Linux 部署与 Linux 边缘节点设备验收是不同证据。原生 Mac 线性任务、tiny 矩阵前向和真实 GGUF 模型生成也分别记录，不互相替代。

## 同一正式 ZIP 的必要文件

- 全部固定源码、根 `README.md`、依赖/安装/运行与验证说明、许可证及来源绑定。
- `AI_USAGE.md` 和 `ai-logs/`：使用说明与明确标注完整度的真实记录。
- `screenshots/`：3–8 张实际运行图，逐张记录来源 URL、源码、北京时间、像素尺寸和 SHA256。新工作台首页不能用旧 Demo 截图冒充；用户文字确认不冒称截图。
- 实际工作台入口、保留的历史 Demo、离线展示与不超过三分钟的讲解路线。默认入口及 `/console` 于北京时间 2026-10-13 18:41:15 核验对齐正式源 `bf5b82e`，见[公开部署补充](evidence/public-deployment-current.md)。18:04 的 `cc7c7ba` [历史部署摘要](evidence/formal-console-deployment-summary.json)与 18:08 的[3 张正式截图](../screenshots/formal-v0.1.1/README.md)保留原来源和范围。
- 本目录的公开证据、源码/作者历史、支持范围表及 ZIP manifest/checksums。

不提交原始私有运行收据、用户节点标识或主机名、Keychain、令牌、操作员路径、模型/构建缓存和私钥。原生客户端产物可以通过固定公开版本链接提供；不误标未签名、开发签名或未公证产物。

## 六维评审引用

| 维度 | 分值 | 文件依据与核验点 |
| --- | --- | --- |
| 完成度 | 25 | 客户端真实线性验收、`public-gguf-f913-generation-summary.json`、`integration/run_two_node_local.sh`；核对不同真实链路的完成范围 |
| 实用性 | 20 | `real-model-runtime/README.md`、Mac 0.1.1 公开安装入口、最终工作台操作指南；核对更新、接任务与结果确认步骤 |
| 创新性 | 15 | `controller-server/app/scheduler.py`、`inference-engine/inference_engine/pipeline.py`、两类任务协议；资源组合与执行收敛，不虚称算法首创 |
| 代码实现 | 15 | `controller-server/app/store.py`、`real-model-runtime/tests/test_worker_recovery.py`、任务契约测试与归档测试；以实际代码和通过的检查为据 |
| 项目架构 | 15 | `controller-server/REAL_JOBS.md`、`node-agent/PROTOCOL.md`、`communication/PROTOCOL.md`；管理端/节点端权限与执行路径隔离 |
| 展示效果 | 10 | 最终工作台的实际桌面/移动网页截图、真实任务数值说明、讲稿及截图来源清单；保留隐私与历史回放标签 |

本文不自评分或自拟官方等级。正式入口为 https://8.134.161.123/，实际页面为 `/console`。已发布 ZIP 的固定源码、完整文件摘要和作者历史由各自归档 manifest 记录，原 a6 包不变。
