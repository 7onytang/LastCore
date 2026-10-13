# AI 使用说明

## AI 如何参与开发

本项目使用 Codex 辅助开发与交付。多条独立聊天分别负责 Controller/节点和客户端、UI、复核部署，以及材料整理；通过各自分支、小提交和 GitHub PR 协同，不把未审分支直接当作已完成功能。队伍名由用户提供：**Tibo重置了吗**。

| 环节 | AI 参与内容 | 可追溯位置 |
| --- | --- | --- |
| 实现与修复 | 辅助编写/修复调度、持久化、节点协议、客户端、恢复和结果处理；按已有开源实现与协议确定边界 | 各模块源码、PR #59/#68、相应测试与模块 README |
| UI | 辅助管理工作台、节点端和展示页面的布局与状态交互；UI UX Pro Max 仅在项目 `.agents/skills/` 安装 | PR #67、`controller-server/app/ui/`、`demo/`、项目级技能来源/许可 |
| 复核与发布 | 独立读取固定提交、测试和结果证据；区分源代码检查、CI、真实任务、设备和签名状态 | 各 PR、`submission/RELEASE-v0.1.1.md`、`submission/evidence/` |
| 提交材料 | 整理功能状态、核心文件、六维评分依据、公开验收摘要、截图来源、对话摘录和版本包摘要 | 根 README、`submission/`、`screenshots/`、`ai-logs/` |

这张表说明协作范围，不把所有文件逐项归属为 AI 原创。原始开发记录不完整的部分，不根据代码风格、提交数量或后期回忆补造提示词和人工操作。

## 模型、工具与人工分工

- Codex 是辅助开发工具，不能与产品中运行的模型混淆。原生导出的历史消息没有逐消息模型 ID，不声称所有历史轮次使用同一模型。
- 可确认的账号检索专用子 agent 按用户规则使用 `gpt-6.1-sol`、`medium`。公开查询使用 Codex 内置网络搜索/网页读取；仓库协作和可自动化操作优先使用现有 API、Git/gh CLI、Python 和脚本。Codex 不使用豆包搜索。
- 用户提供项目方向、队伍名和取舍，并在真实 Mac 客户端亲自确认任务及数值显示一致。材料是否提交及邮件发送由用户决定；本聊天没有发送邮件。
- 账号、凭据、法律/授权和需要人工控制的步骤由用户处理；对话与公开材料不保留密码、令牌、Keychain、私有配置或原始操作员记录。
- 本次新一轮 Multica 检索在需要登录时被用户选择“暂不核对”而暂停；该轮未读取账号内任务或写入状态，不抹除此前 Alpha 阶段的只读核对历史。

## 产品内计算与验证边界

| 路径 | 实际含义 | 证据范围 |
| --- | --- | --- |
| 原生 Mac 线性任务 | `lastcore/linear-demo-v1` 是确定性小型数值计算，不是语言模型生成 | 真实目标 MacBook Pro 单节点结果与参考一致，用户文字确认 GUI；[公开摘要](submission/evidence/mac-client-0.1.1-linear-acceptance.md)不是新截图 |
| tiny 分层原型 | 自研 tiny transformer、固定种子随机权重与 TCP activation 传输 | 既有多进程与单机对照，不代表预训练模型跨物理设备分层 |
| 真实预训练模型 | 固定 SmolLM2-135M-Instruct GGUF，通过 llama.cpp CPU 后端生成文本 | [本轮公开摘要](submission/evidence/public-gguf-f913-generation-summary.json)：公网 Controller 到 Mac mini 整模型 worker；生成 Paris 摘要及停止清理通过，不是 MacBook Pro native App 的 LLM 功能 |
| 模拟执行器 | Node Agent 的 `demo` 模式只用于协议/生命周期演示 | 模拟完成状态不能当作实际计算或模型结果 |

SmolLM2 权重保留 Apache-2.0 条款，llama.cpp 保留 MIT 条款；固定版本、摘要和完整许可材料在 `real-model-runtime/`。LastCore 自有源码的 MIT 许可不覆盖或替换第三方条款。

CI 通过、源码一致、HTTP 200、人工 GUI 确认和真实模型执行分别证明不同事项。Android 主机 JNI 不替代真机，移动网页截图不替代 Android App 截图，历史回放不替代当前推理。Android 真机仍延期，其他边缘适配按实际源码/CI/设备证据标注。用户要求停止新增真实运行后，材料维护者只继续文档和静态一致性整理，不重派任务或新增模型/设备测试。

## 对话记录与截图

- `ai-logs/2026-10-13-submission.md`：材料聊天相关原文节选。
- `ai-logs/2026-10-13-demo-development.md`：开发主线的公开 Demo 相关原文节选。
- `ai-logs/optimization/optimization-conversation.json`：6 个轮次、41 条原生可见消息；材料聊天逐条核对消息 ID 与原文一致，文件摘要与排除范围见同目录说明。

以上均明确为节选，不冒称完整开发历史。工具输出、内部推理、自动上下文、无关私人资料和凭据被排除。没有逐消息时间/模型字段就不补造；新验收的公开摘要也不冒充原始私有对话或运行日志。

已有截图直接来自实际浏览器页面，原 URL、源码、北京时间、尺寸和 SHA256 在 `screenshots/capture-manifest.json`。设备 SVG 是架构示意；未使用生图替代运行截图。正式工作台的新截图以实际采集记录为准，旧 Demo 图不冒充新首页。三分钟讲稿不冒称已录制视频。

## 可复用方案与提交状态

材料组织参考了已检索的 [开源提交模板](https://github.com/drijesh-ppatel/bob-ai-hackathon-submission-template)，未复制它的源码或把它当作本赛事官方模板。运行和发布沿用项目已有原生工具，不另引入发布框架。

原 `a6` ZIP 保持不变；后续公开证据和正式材料单独提交分支/PR，并按最终固定提交生成新版本包。本文不自评分、不自拟官方等级，也不声称邮件已提交或全部平台已验收。
