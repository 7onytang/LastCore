# 贡献与协作

沿用项目已有源码、协议和模块 README。本文记录当前协作方式，补充项目
[AGENTS.md](AGENTS.md)，不替代上级个人偏好或当前任务的用户授权。

## 当前技术栈与入口

| 范围 | 已有实现与说明 |
| --- | --- |
| 控制面 | Python 3.12+、FastAPI、SQLite；[根 README](README.md)与 [API 契约](controller-server/API_CONTRACT.md) |
| 节点与 tiny 分层 | Python、NumPy、TCP；[Node Agent](node-agent/README.md)、[推理引擎](inference-engine/README.md)、[通信层](communication/README.md) |
| 原生 Mac 客户端 | Swift；[安装与功能范围](macos-client/README.md) |
| Python 桌面与 portable | 本机 HTML/JS 面板及 Tkinter；[桌面说明](node-agent/desktop/README.md)、[Windows/Linux 预览](edge-client/README.md) |
| Android | Java、JNI/C++、Gradle/CMake；[Android 说明](android-app/README.md) |
| 整模型 worker | Python CLI 与固定 llama.cpp/GGUF；[运行时说明](real-model-runtime/README.md) |

新增或调整方案前先核对成熟的官方/开源实现、许可、兼容性与集成成本，优先复用
已有能力。保持线性运算、tiny 分层和单设备整模型路径的能力边界。

## 分支、commit 与 PR

1. 先确认当前目录、分支和 `git status`，从适合的最新基线创建独立主题分支；
   并行工作可用独立 worktree。只暂存本次文件/改动，不覆盖他人的未提交内容。
2. 一个 commit 表达一个连贯改动，标题说明模块和实际行为；保留原作者及复用来源。
   同一接口的调用方、必需打包源码绑定、相关验证和文档应随兼容改动一起交付。
3. 通过 Draft PR 提供可审查候选。描述具体问题、最终行为和范围，关联相关 issue，
   给出完整 head SHA、适用验证命令/结果及未完成事项。不要用多条无关改动拼成提交。
4. 交由非作者针对固定 head 独立复核，再由维护者按当前授权合并。新增提交后更新
   审查和适用 CI 依据；不把旧 head 的通过结果转用于新代码。

## 验证与证据

- 安装、运行和验证入口见[根 README](README.md)及上表模块说明；使用隔离环境和
  已声明的依赖/锁，按改动选择有意义的检查。纯文档改动通常检查 diff、链接与事实，
  无需为了流程补重复测试。`git diff --check` 可检查空白问题。
- PR 如实区分纯 Fake/ASGI、编译/静态来源绑定、实际进程、部署和用户设备验收；
  失败、跳过及环境限制分别记录。CI 通过或 HTTP 200 不能替代真实业务/设备证据，
  Git 提交也不等于已部署。发布附件保留其真实构建 SHA、哈希、许可和验证范围。
- 遵守任务已有边界：当前新增真实模型/设备验收暂停，最终 demo、图片及新材料
  压缩包待项目完成后再做。常规源码/文档维护不自动授权部署、重新派活或发布包。

## AI、隐私与许可

AI 分工、工具和历史记录范围见 [AI_USAGE.md](AI_USAGE.md)与
[对话索引](ai-logs/README.md)。记录实际完成的操作和原始证据；不补造提示词、
模型轮次、截图或验收，不把历史证据归到新源码/设备。凭据、私有配置、状态库和
签名私钥不进入 commit、PR、日志或发布归档。

自有源码采用 [MIT](LICENSE)；第三方代码、模型和依赖保留各自的许可与署名，
不能用项目许可替代。模型/后端完整条款和来源见
[许可清单](real-model-runtime/licenses/manifest.json)，各打包路径继续核验其许可证。

组织方式参考 [GitHub 贡献指南](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors)
与 [Google 小型连贯变更建议](https://google.github.io/eng-practices/review/developer/small-cls.html)；
未引入新的贡献框架或复制上游指南正文。
