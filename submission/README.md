# LastCore 参赛提交核对

队伍名：**Tibo重置了吗**。截止时间：**2026 年 10 月 13 日 18:00，Asia/Shanghai**，来自用户提供的赛事要求。

本文件是提交材料索引，不是赛事官方 README 模板。赛题全称、官方开赛时间、官方模板和评分等级描述尚待提供；不得据此自行宣称满足全部赛事要求。

用户已明确要求：**最终 Demo、图片和压缩包等项目完成后再制作**。因此本目录当前保留文档草稿和已有 Alpha 证据；最终截图、Demo、ZIP 与发送步骤暂停，等待核心项目完成并固定源码版本后继续。

当前开发状态来自协作主线：Mac worker 注册拒绝后的重试、凭据与 Controller origin 绑定、停止账本恢复上报顺序三处问题正在修复，最终源码尚未冻结。已有 Alpha 验收记录不作废，但不能据此宣称 Mac 异常恢复已全部验证；最终材料须补入修复提交与对应验证结果。

## 必需材料

| 材料 | 当前状态 | 位置 / 待办 |
| --- | --- | --- |
| 源代码 | 已有；最终打包应固定提交 | 仓库各模块；公开 Alpha 源码为 `85ddad92758d072097de87857f7ed1a229c8f76e` |
| README 项目说明 | 已有运行说明；本次补充提交核对 | 根 `README.md`；功能和核心文件见下表 |
| AI 使用说明 | 已新增，历史使用待各开发聊天核实 | `AI_USAGE.md` |
| AI 对话记录 | 本聊天及 Demo 主线真实节选已记录；完整开发覆盖仍待补充 | `ai-logs/` |
| 3–8 张实际运行截图 | 待 Demo 主线交付并核对 | 根 `screenshots/`；不能用设计图或示意图充数 |
| Demo 链接或不超过 3 分钟视频 | 草稿保留；最终制作等项目完成 | 最终须写入根 README；API 文档不代替完整演示 |
| 同一 ZIP 压缩包 | 等截图、Demo、最终提交齐备后生成 | 应包含上述材料；排除凭据、缓存和私有数据库 |
| 开发时间证明 | 本地历史已检查；赛事时间与平台创建时间未核验 | `submission/git-history.txt` |
| 邮件提交 | 未发送 | `seproject@yeah.net`，主题为队伍名 + 赛题 |

## 功能状态与核心文件

状态为代码与已有证据所支持的范围；历史验收不能当作本次重新运行。

| 功能 | 状态与边界 | 核心实现 / 验证依据 |
| --- | --- | --- |
| Controller API、鉴权、节点注册/心跳 | 已实现 | `controller-server/app/main.py`、`controller-server/app/models.py`、`controller-server/app/store.py`；`controller-server/tests/test_controller.py` |
| 连续分层调度与容量约束 | 已实现；内存仍按每层估算 | `controller-server/app/scheduler.py`；`controller-server/tests/test_controller.py` |
| 节点轮询、去重、取消/超时停止 | 已实现；停止依赖节点遵守协议 | `node-agent/lastcore_node_agent/agent.py`、`node-agent/lastcore_node_agent/executor.py`、`node-agent/lastcore_node_agent/inference_adapter.py`；`node-agent/tests/` |
| tiny 分层推理与 TCP activation 传输 | 已实现，本机多进程验证路径；不是预训练大模型多设备分片 | `inference-engine/inference_engine/model.py`、`inference-engine/inference_engine/stage.py`、`inference-engine/inference_engine/pipeline.py`；`communication/communication/channel.py`、`communication/communication/protocol.py`；`integration/run_two_node_local.sh` |
| 固定 SmolLM2 单设备真实模型 | 已实现；CPU 整模型，与 tiny 路径分开 | `real-model-runtime/runtime.py`、`real-model-runtime/bootstrap.py`；`real-model-runtime/evidence/verification.json` |
| 云端整模型任务与 macOS CLI worker | 已实现；部署验收记录来自其他开发主线 | `controller-server/app/inference_jobs.py`、`controller-server/REAL_JOBS.md`；`real-model-runtime/worker.py`、`real-model-runtime/job_guardian.py`；`submission/evidence/ACCEPTANCE.txt` |
| Android 前台工作节点/JNI | 已实现并有构建和主机 JVM/JNI 证据；Android 真机未验收 | `android-app/app/src/main/java/ai/lastcore/android/MainActivity.java`、`android-app/app/src/main/java/ai/lastcore/android/CloudWorker.java`、`android-app/app/src/main/java/ai/lastcore/android/JniBackend.java`、`android-app/app/src/main/java/ai/lastcore/android/CredentialVault.java`；`android-app/app/src/main/cpp/local_runtime.cpp`；`submission/evidence/ACCEPTANCE.txt` |
| SQLite 诊断、在线备份与恢复验证 | 已实现 | `controller-server/app/diagnostics.py`、`controller-server/app/backup.py`；`controller-server/tests/test_diagnostics.py`、`controller-server/tests/test_backup.py`；`integration/verify_backup_recovery.py` |
| 部署与固定来源归档 | 已实现；公开 Alpha 有独立来源记录 | `ops/public-ip-https/`、`ops/staging/prepare_release.py`、`ops/staging/integration-sources.json`；`submission/evidence/` |
| 预训练大模型跨设备分层、KV cache、NAT 穿透、自动迁移 | 未实现 | 根 README 已列局限，不列为已完成功能 |
| Android 真机运行、生产签名/公证、Windows 发布 | 未验收 / 未交付 | 不把 APK 构建、主机 JNI 或未签名 bundle 当作这些能力的证明 |

## 评分依据索引

仅使用用户提供的六个维度和分值，未收到公开等级描述，不给出虚构等级或自评分。

| 维度 | 分值 | 供评审引用的具体文件 | 评估限制 |
| --- | --- | --- | --- |
| 完成度 | 25 | `integration/run_two_node_local.sh`、`controller-server/REAL_JOBS.md`、`submission/evidence/ACCEPTANCE.txt` | 明确区分 tiny 分层与整模型；真机延期 |
| 实用性 | 20 | `node-agent/desktop/README.md`、`android-app/README.md`、`submission/evidence/QUICKSTART.txt` | 用户接入仍需部署操作员提供私有凭据 |
| 创新性 | 15 | `controller-server/app/scheduler.py`、`inference-engine/PROTOCOL.md`、`node-agent/PROTOCOL.md` | 分层和异构资源组织是实现依据，不宣称算法首创 |
| 代码实现 | 15 | `controller-server/app/store.py`、`controller-server/tests/test_capacity_expiry_scaling.py`、`node-agent/tests/test_agent.py` | 测试覆盖不等于生产吞吐证明 |
| 项目架构 | 15 | `communication/PROTOCOL.md`、`controller-server/REAL_JOBS.md`、`ops/staging/integration-sources.json` | 两类任务协议独立，不能混淆产物范围 |
| 展示效果 | 10 | 待最终 `demo/` 与 `screenshots/` 实际文件补齐 | 尚不作完成声明 |

## 开发时间与最终打包

`submission/git-history.txt` 保存本次读取的完整提交 ID、作者时间、提交时间与标题。Git 时间是本地历史证据，可被修改；不能单独证明 GitHub 创建时间或赛事合规。不得改写时间、补造功能提交或拆分历史提交凑次数。

最终包需包含源代码、根 README、`AI_USAGE.md`、`ai-logs/`、`screenshots/`、Demo 入口及本目录的证据。大文件可在包内提供外部下载链接；先验证评委无需私有账号可访问。记录最终源码提交、包内文件清单与 SHA256，并检查压缩包可解压。

排除 `.git/`、`.venv/`、`.env`、token、密钥、签名材料、模型/构建缓存、数据库和未经脱敏的测试日志。真实对话记录保留来源和脱敏说明，禁止补写不存在的对话。

邮件主题待赛题确认后填写为 `【Tibo重置了吗 + 赛题全称】`。本文件不代表已发送邮件或已收到主办方确认。
