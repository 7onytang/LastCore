# 证据来源与使用范围

本目录四个文件从本机 `/private/tmp/lastcore-final-delivery-85ddad9/` 复制。两个开发聊天确认该目录是公开 `v0.1.0-alpha.1` 交付记录，并提供来源；本聊天读取了实际内容，未重新运行其中验收。

公开入口：[LastCore v0.1.0-alpha.1](https://github.com/7onytang/LastCore/releases/tag/v0.1.0-alpha.1)。各文件在该 Release 中有同名附件。

- `ACCEPTANCE.txt`：历史部署、CI、客户端与模型验收范围。
- `QUICKSTART.txt`：对应 Alpha 的安装/运行说明与限制。
- `source-manifest-85ddad9.json`：该 Alpha 固定源码归档的来源与摘要。
- `client-source-equivalence-85ddad9.json`：保留客户端构建产物与固定候选源码的对应关系。

这些是已有 Alpha 的证据，不能证明尚未冻结的后续项目版本已经通过同样验收。最终比赛包必须核对最终提交与此版本之间的变化，并补充新增功能的实际验证依据。尚未确认最终参赛版本。

## Mac worker 修复后的本地验证

- `mac-worker-30b9efc-review.md`：独立复核者的原记录，覆盖修复源码、23 项相关单元测试、三个真实本地 HTTP Controller 的恢复场景，以及对唯一真实模型验收的只读核验。没有由复核者重复执行模型。
- `mac-worker-30b9efc-local.json`：主线生成的一次真实本地 Controller + 修复 worker + 固定 GGUF 任务记录，模型输出、停止确认和清理结果保留。发布时只去掉临时私有测试目录字段 `workdir`，并保留原文件 SHA256 与省略说明；材料聊天另核三个源码摘要与固定 Git 对象、当前合入源码均相符。

修复源为 `30b9efc22c5db9f351d304604e65743ca02ccda3`，经 [PR #59](https://github.com/7onytang/LastCore/pull/59) 合并为 `7a9cd4910ee41d3b27fdc8c1d02c08f8487bc331`。上述两份文件属于本地修复验证，不是新的公网任务、云部署或 Android 真机证据；原 Alpha 四份记录保持原字节和范围。
