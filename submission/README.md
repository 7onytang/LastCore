# LastCore 参赛提交核对

队伍名：**Tibo重置了吗**。截止时间：**2026 年 10 月 13 日 18:00，Asia/Shanghai**，来自用户提供的赛事要求。

本文件是提交材料索引，不是赛事官方 README 模板。赛题全称、官方开赛时间、官方模板和评分等级描述尚待提供；不得据此自行宣称满足全部赛事要求。

用户要求的顺序已执行：最小核心完成并冻结后，再制作最终展示。Demo 源码已由 PR #62 合入；本批补入 5 张原始实际截图、采集元数据和三分钟讲稿。统一 ZIP 等全部材料与最小部署脚本进入最后云端 main 后生成一次；随后静态部署与同字节镜像通过实际检查，结果随最终交付验收收据提供。邮件未发送。

Mac worker 的三处恢复/凭据修复已由 PR #59 合入：修复源 `30b9efc22c5db9f351d304604e65743ca02ccda3`，核心合并提交 `7a9cd4910ee41d3b27fdc8c1d02c08f8487bc331`。23 项相关测试（8 项新增回归）、真实本地 Controller 的三个恢复场景和一次真实固定 GGUF 任务通过；证据见 `submission/evidence/mac-worker-30b9efc-review.md` 与 `submission/evidence/mac-worker-30b9efc-local.json`。这是本地修复验收，不是新云部署或 Android 真机证明；最终参赛提交和展示材料仍待统一固定。

## 必需材料

| 材料 | 当前状态 | 位置 / 待办 |
| --- | --- | --- |
| 源代码 | 最小核心及 Demo 已合入；打包固定最后云端提交 | 各模块和 `demo/`；原 Alpha `85ddad9` 与修复源 `30b9efc` 的验证范围分开 |
| README 项目说明 | 已有运行说明；本次补充提交核对 | 根 `README.md`；功能和核心文件见下表 |
| AI 使用说明 | 已有并对照两条主线节选更新；完整历史不作推断 | `AI_USAGE.md` |
| AI 对话记录 | 三个聊天相关节选已记录；完整开发覆盖仍待补充 | `ai-logs/`；优化主线 41 条原文已逐条核对 |
| 3–8 张实际运行截图 | 已有 5 张原始 PNG，摘要与尺寸核对通过 | `screenshots/README.md`、`screenshots/capture-manifest.json`；3 张网页、2 张实际 HTTPS 服务截图 |
| Demo 链接或不超过 3 分钟视频 | 页面与离线展示已合入；上线检查随交付验收提供 | `demo/index.html`、`demo/offline.html`、`demo/DEMO-SCRIPT-3MIN.txt`；讲稿不冒充已录制视频 |
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
| 云端整模型任务与 macOS CLI worker | 已实现；恢复修复已合，原云验收与修复后本地验收分开 | `controller-server/app/inference_jobs.py`、`controller-server/REAL_JOBS.md`；`real-model-runtime/worker.py`、`real-model-runtime/job_guardian.py`、`real-model-runtime/tests/test_worker_recovery.py`；`submission/evidence/ACCEPTANCE.txt`、`submission/evidence/mac-worker-30b9efc-local.json` |
| Android 前台工作节点/JNI | 已实现并有构建和主机 JVM/JNI 证据；Android 真机未验收 | `android-app/app/src/main/java/ai/lastcore/android/MainActivity.java`、`android-app/app/src/main/java/ai/lastcore/android/CloudWorker.java`、`android-app/app/src/main/java/ai/lastcore/android/JniBackend.java`、`android-app/app/src/main/java/ai/lastcore/android/CredentialVault.java`；`android-app/app/src/main/cpp/local_runtime.cpp`；`submission/evidence/ACCEPTANCE.txt` |
| SQLite 诊断、在线备份与恢复验证 | 已实现 | `controller-server/app/diagnostics.py`、`controller-server/app/backup.py`；`controller-server/tests/test_diagnostics.py`、`controller-server/tests/test_backup.py`；`integration/verify_backup_recovery.py` |
| 部署与固定来源归档 | 已实现；公开 Alpha 有独立来源记录 | `ops/public-ip-https/`、`ops/staging/prepare_release.py`、`ops/staging/integration-sources.json`；`submission/evidence/` |
| 预训练大模型跨设备分层、KV cache、NAT 穿透、自动迁移 | 未实现 | 根 README 已列局限，不列为已完成功能 |
| Android 真机运行、生产签名/公证、Windows 发布 | 未验收 / 未交付 | 不把 APK 构建、主机 JNI 或未签名 bundle 当作这些能力的证明 |

## 评分依据索引

以下按提供的六个维度和分值整理实际证据，不给出没有证据支持的等级或自评分。

| 维度 | 分值 | 供评审引用的具体文件 | 评估限制 |
| --- | --- | --- | --- |
| 完成度 | 25 | `integration/run_two_node_local.sh`、`controller-server/REAL_JOBS.md`、`submission/evidence/ACCEPTANCE.txt`、`submission/evidence/mac-worker-30b9efc-local.json` | 原云验收、本机 tiny、修复后本地真实模型分别记录；真机延期 |
| 实用性 | 20 | `real-model-runtime/README.md`、`node-agent/desktop/README.md`、`android-app/README.md` | 当前 CLI 与旧凭据迁移按最新指南；生产接入需私下配置凭据，本地自建不需生产 token |
| 创新性 | 15 | `controller-server/app/scheduler.py`、`inference-engine/PROTOCOL.md`、`node-agent/PROTOCOL.md` | 分层和异构资源组织是实现依据，不宣称算法首创 |
| 代码实现 | 15 | `controller-server/app/store.py`、`controller-server/tests/test_capacity_expiry_scaling.py`、`real-model-runtime/tests/test_worker_recovery.py`、`ops/staging/tests/test_prepare_release.py` | 23 项相关恢复测试含 8 项新增；9 项真实 Git 归档测试；不等于生产吞吐证明 |
| 项目架构 | 15 | `communication/PROTOCOL.md`、`controller-server/REAL_JOBS.md`、`ops/staging/integration-sources.json` | 两类任务协议独立，不能混淆产物范围 |
| 展示效果 | 10 | `demo/index.html`、`demo/offline.html`、`demo/DEMO-SCRIPT-3MIN.txt`、`screenshots/01-demo-desktop.png`、`screenshots/02-demo-history-replay.png`、`screenshots/03-demo-mobile-browser.png`、`screenshots/04-live-swagger-health.png`、`screenshots/05-live-health-response.png` | 网页实际渲染与 HTTPS 服务响应；回放不等于新推理，390px 浏览器不等于 Android 真机 |

## 开发时间与最终打包

`submission/git-history.txt` 保存本次读取的完整提交 ID、作者时间、提交时间与标题。Git 时间是本地历史证据，可被修改；不能单独证明 GitHub 创建时间或赛事合规。不得改写时间、补造功能提交或拆分历史提交凑次数。

最终包需包含源代码、根 README、`AI_USAGE.md`、`ai-logs/`、`screenshots/`、Demo 入口及本目录的证据。大文件可在包内提供外部下载链接；先验证评委无需私有账号可访问。记录最终源码提交、包内文件清单与 SHA256，并检查压缩包可解压。

排除 `.git/`、`.venv/`、`.env`、token、密钥、签名材料、模型/构建缓存、数据库和未经脱敏的测试日志。真实对话记录保留来源和脱敏说明，禁止补写不存在的对话。

邮件主题待赛题确认后填写为 `【Tibo重置了吗 + 赛题全称】`。本文件不代表已发送邮件或已收到主办方确认。
