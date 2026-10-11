# AI 使用记录（2026-10-11）

本记录仅摘录与本仓库说明有关的真实工作记录；摘要以注明的 Multica issue/comment/attachment 为准。未导出完整对话。隐去邮箱、IP、令牌、密钥、凭据指纹、私密附件内容及本机个人路径。

## 使用本次引用的材料

- **用户需求来源**：LC-1 评论 `01a12b18-1e1f-7e58-9d85-3ef543f7cbbd`，要求准备 README、AI 使用说明和 AI 对话记录等项目交付材料。该评论的部分内容在界面中有截断，本日志不扩写截断部分。
- **exo 技术对照来源**：LC-4 issue `01a12b08-49da-7109-9a34-a5073aab117f`，Engineer-B 交付评论 `01a12b66-d575-7155-9c86-d621958ad44a` 和附件 `01a12b66-d41b-72f1-b3db-d74425e5c41b`。附件记录固定 exo commit `21a54c5ea0230a3bec1e1a786d200126c7e34ec6`、Apache-2.0 许可证及平台/兼容性限制；它是静态资料对照，不是 LastCore 的运行证据。
- **生成 README、AI_USAGE 与本日志的模型**：`gpt-6-luna`（Multica 运行时提供的 agent 身份说明）。未调用其他模型或图像生成工具。
- **记录的审核状态**：截至本日志生成时，这些文档尚无独立人工审核结论。本日志列出的 LC-4 审查者是 AI agent，不是人类审批人；其模型未记录。

## 对话及其用途

### 1. 兼容性材料交付

- **来源**：LC-4 comment `01a12b32-b60f-7dbe-af11-512ea500d6c1`
- **发言者 / 模型**：Engineer-B｜开发员 B / `agent-plan/deepseek-v4-1-flash-260910`
- **真实记录摘要**：Engineer-B 说明已基于固定 exo commit 静态读取官方 README、PLATFORMS、LICENSE、项目依赖和 API 文档，交付三平台及双节点验收矩阵；并明确未做运行时验证、三平台真机测试或 LastCore 双节点联调。
- **如何使用**：只复用矩阵中明确标为事实的许可证、平台支持与限制，并沿用“参考资料不能证明 LastCore 功能已实现”的边界。矩阵中的推断没有作为 LastCore 已实现功能。

### 2. 独立审查发现

- **来源**：LC-4 comment `01a12b57-3a2d-7058-a51e-3e16dfe7c482`
- **发言者 / 模型**：Reviewer-A｜常规审查 / 模型未记录
- **真实记录摘要**：Reviewer-A 指出初版矩阵把 exo API 与 LastCore 取消接口混淆，也把尚未冻结的 KV、重派、恢复等行为写得过于确定；要求纠正映射并保留未验证状态。
- **如何使用**：据此把本 README 的功能描述限定到实际 LastCore API/源码行为，明确任务完成不等于生成结果，并把模型输出、KV 交接和自动迁移列为未验证或尚未实现。

### 3. 修订版矩阵

- **来源**：LC-4 comment `01a12b66-d575-7155-9c86-d621958ad44a`，附件 `01a12b66-d41b-72f1-b3db-d74425e5c41b`
- **发言者 / 模型**：开发助手 / 模型未记录
- **真实记录摘要**：修订记录指出 LastCore 当前取消路由为 `POST /v1/tasks/{id}/cancel`，运行状态映射不能照搬 exo；把未冻结契约标为后续目标，并保留三平台真机与跨节点验证缺口。
- **如何使用**：将该固定附件作为本仓库材料的二级参考；保留其源 ID 与 SHA-256（`931cd7dd4c35cf61bb4b1d7aebe7cfd7af67531e15a493da86dc6dec1349fb1c`），不把附件自身描述的验收预期当作 LastCore 实测结果。

## 验证与待审核

- **文档基线**：LastCore `8c665cfe03f1c852e82c9b265cae1ff6c0a3270d`。本次修改仅限顶层 `README.md`、`AI_USAGE.md` 与 `ai-logs/`。
- **模板**：未提供独立文档模板；按仓库当前 Markdown 格式编写。
- **自动化测试**：在 `controller-server/` 执行 `uv run --with-requirements requirements-dev.txt -- python -m pytest -q`，退出码 `0`，`16 passed`。Python `3.14.7` 下直接执行 `python3 -m pytest -q` 退出码 `1`（环境没有 pytest 模块）；之后用仓库固定的开发依赖运行通过。结果有一条 Starlette `TestClient` 与 `httpx` deprecation warning。
- **材料检查**：`git diff --check` 退出码 `0`；本地 Markdown 链接目标检查通过，`AI_USAGE.md` 中唯一相对链接可解析；`rg` 秘密/IP/邮箱模式检查未发现材料中的凭据或个人联系信息。扫描命中的 README 私网地址是仓库已有的示例 endpoint，不是本次对话或实际部署地址。
- **PR 头核对**：`git ls-remote origin refs/heads/main 'refs/pull/*/head'` 退出码 `0`；读到 main `8c665cfe03f1c852e82c9b265cae1ff6c0a3270d`，已有 PR head #1 `ee57405f925a6aba44a9b3a9402599cf55b04d61`、#2 `b533929fdbc2f5cfe84e80d9743ec35fbe21cb94`、#3 `73fabf4e1bdf295afb273b0772f15763d1b6fbe8`。这些为已存在 PR 的 head，不是本次文档变更的 PR。
- **人工审核**：尚未记录维护者对本次文档的批准。日志只说明文档起草所依据的 AI 工作和审查边界。
