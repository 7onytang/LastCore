# Mac 客户端 0.1.1：真实目标设备线性任务验收

这是一份最小公开摘要，依据主线的服务端结果核验、独立持久化检查，以及用户在原聊天中的文字确认整理。材料维护者没有重新派发或执行任务，没有访问生产数据库、凭据或私有回执。原 `a6` 交付 ZIP 保持不变。

## 版本与下载

- 修复源：[`f91397620575444e253c528a71105b09c2ef776d`](https://github.com/7onytang/LastCore/commit/f91397620575444e253c528a71105b09c2ef776d)。
- 合入记录：[PR #68](https://github.com/7onytang/LastCore/pull/68)，合并提交 `6d76eea20e670786a99feb1d159618ae5984ebf8`。
- 公开预发布：[v0.1.1-alpha.1](https://github.com/7onytang/LastCore/releases/tag/v0.1.1-alpha.1)。
- [LastCoreClient 0.1.1 arm64 DMG](https://github.com/7onytang/LastCore/releases/download/v0.1.1-alpha.1/LastCoreClient-0.1.1-arm64-f9139762.dmg)、[SHA256 文件](https://github.com/7onytang/LastCore/releases/download/v0.1.1-alpha.1/LastCoreClient-0.1.1-arm64-f9139762.dmg.sha256)、[产物来源记录](https://github.com/7onytang/LastCore/releases/download/v0.1.1-alpha.1/LastCoreClient-0.1.1-arm64-f9139762.json)。

发布与修复源已通过公开 GitHub API 核对。本文不追加生产签名、公证或商店发布声明。

## 本次实际计算

| 项目 | 已确认内容 |
| --- | --- |
| 客户端 | 用户原有 MacBook Pro 上的 LastCoreClient，已更新至 0.1.1，原节点身份保持 |
| 任务类型 | `lastcore/linear-demo-v1` |
| 输入向量 | `[1, 2, 3, 4]` |
| 权重种子 | `7` |
| 输出行数 | `2` |
| 执行规模 | 一台物理 MacBook Pro、一个目标执行节点、一个分配 |
| 服务端任务及分配状态 | 均为 `completed` |
| 实际结果 | `[0.9999684130511179, -0.9047475044778314]` |
| 参考结果误差 | `0`，由主线核验；独立持久化检查也通过 |
| 服务端完成时间 | `2026-10-13 17:26:32`，北京时间 `Asia/Shanghai` |
| GUI 显示 | `0.99996841`、`-0.90474750`，用户确认任务与数值一致 |

用户确认原文：**“已显示，任务和数值一致”**。这是用户亲自确认 GUI 的证据，不是材料维护者重新操作客户端或取得截图的声明。没有新截图附于本记录。

## 操作与权限边界

1. 使用上述公开版本更新客户端，连接已配置的 Controller，保留原有节点身份。
2. 由服务器管理员确认目标客户端兼容后，创建针对该节点的线性任务。节点端只查看本机设备状态和自身任务，不取得全局节点管理或派发权限。
3. 客户端领取、计算并回报结果；管理员核对任务终态和结果，用户在客户端确认当前任务及显示值。

该类型的精确字段与约束见[修复源的任务模型](https://github.com/7onytang/LastCore/blob/f91397620575444e253c528a71105b09c2ef776d/controller-server/app/models.py)：`input_vector`、`weight_seed`、`target_node_id`，以及 `max_nodes=1`。目标标识由管理员私下使用，本记录不发布实际节点 ID，也不构造缺少其它必需字段的可直接提交请求。

## 验收范围

本次是用户真实目标 Mac 客户端的**单节点线性运算**，不是临时 Stage 节点或本地模拟。它不证明分布式大模型执行、两个物理设备协同，或当前公开 Demo 网页直接发起并显示了这次任务。已有 tiny 多进程验证和历史模型回放各自保留原证明范围。

Android 真机验收仍延期，其他类型边缘设备适配仍为开发中。本文不公开用户节点 UUID、设备名称、Keychain 内容、令牌、操作员路径或私有回执原文，也不替换原 `a6` ZIP 中的历史记录。
