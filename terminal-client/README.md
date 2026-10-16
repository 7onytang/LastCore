# LastCore Terminal Client（LC-14）

三平台（macOS / Windows / Android）终端操作界面与本地节点适配的首批交付：交互原型、本地 adapter 边界与凭据流向方案。**原型阶段**：界面与推理后端分离，未接真实推理，未引入未批准的框架或跨进程权限模型。

## 本轮核对结论（基线 main@8c665cf，2026-10-12）

| 核对项 | 结论 |
| --- | --- |
| 现有前端 | main 分支不存在任何前端/终端目录；按 LC-14 要求提交最小框架与本地 adapter 方案 |
| 本地 Node 接口 | node-agent（headless CLI，最新 2318c59，位于 `agent/engineer-a-a/b33d0fac979a`，未并入 main）**没有**供界面调用的本地 API；LC-12 桌面增量只有启动/preflight，同样 headless |
| Controller /v1 | PR #1（ee57405）既有接口：register / heartbeat / assignments / report / tasks / cancel，均为云端控制平面接口，节点 token 只能管理本节点 |
| 界面权限 | UI 不持有管理员 token、不持有 node_token、不做调度——与 LC-3 架构边界一致 |

因此「UI ↔ 本地 Node」之间的通道是当前真正的缺口，本目录用 adapter 契约把它显式化，而不是悄悄假定一个接口。

## 目录结构

```
terminal-client/
├── README.md
├── prototype/
│   ├── index.html            # 交互原型（无构建步骤、零依赖）
│   ├── styles.css
│   ├── app.js                # 界面状态与六类交互流（连接/资源/状态/结果/停止/错误）
│   └── adapter/
│       ├── adapter.js        # adapter 契约：错误码、输入校验、快照结构、秘密字段防泄漏
│       ├── mock-adapter.js   # MockNodeAdapter：脚本场景 + 故障注入（评审/测试用）
│       └── local-http-adapter.js  # LocalHttpNodeAdapter：本地 API 桩（端点未批准，显式失败）
└── tests/
    ├── adapter.test.mjs      # adapter 契约单元测试（node --test，11 例）
    └── e2e_browser.mjs       # 真实 Chrome 无头浏览器全流程验证（11 项交互断言）
```

## 运行与验证

原型（需本机任意浏览器，ES module 不能用 file:// 直接打开）：

```bash
cd terminal-client/prototype
python3 -m http.server 8788
# 浏览器访问 http://127.0.0.1:8788
```

adapter 单元测试（零依赖，Node 20+）：

```bash
node --test terminal-client/tests/adapter.test.mjs
```

真实浏览器流程验证（自动查找本机 Chrome/Chromium，无则打印 SKIP）：

```bash
node terminal-client/tests/e2e_browser.mjs
```

## adapter 边界（本交付的核心）

UI 只与 `NodeAdapter` 契约对话，永远不直接访问 Controller /v1、node 状态目录或推理引擎：

```
┌───────────────────────────── UI（prototype，可移植到任意壳） ─────────────────────────────┐
│  连接入网 / 资源设置 / 节点状态 / 任务与结果 / 停止执行 / 错误显示（异步订阅快照，长任务不阻塞）  │
└──────────────────────────────────────┬───────────────────────────────────────────────────┘
                                       │ NodeAdapter 契约（connect/getStatus/updateResources/
                                       │ requestStopExecution/getRecentAssignments/subscribe）
                    ┌──────────────────┴──────────────────┐
          MockNodeAdapter（场景+故障注入）        LocalHttpNodeAdapter（桩）
                                              │  仅 localhost，端点方案待批准
                                     ┌────────┴─────────┐
                                     │ node-agent 本地 API（未实现） │
                                     │ 凭据/日志都在 node-agent 侧    │
                                     └────────┬─────────┘
                                        Controller /v1（云端）
```

契约要点（`adapter.js`）：

- **快照驱动**：所有界面数据来自 `subscribe(listener)` 推送的完整快照（connection/node/execution/recentAssignments/errorLog）；界面不自行轮询、不自行判断执行状态。
- **错误显式化**：`NodeAdapterError` 携带机器可读 `code`（UNREACHABLE / AUTH_REJECTED / MODEL_UNSUPPORTED / CONTROLLER_ERROR / INVALID_STATE / INVALID_INPUT / LOCAL_API_UNAVAILABLE）与中文用户文案；UI 不允许在没有 adapter 确认时显示成功。
- **停止取自执行状态**：`requestStopExecution()` 在没有 starting/running 执行时抛 `INVALID_STATE`；停止经 `stopping` 宽限期到 `stopped`，由快照回报，按钮禁用/启用只是状态的投影。
- **保存后回读**：`updateResources()` 返回 adapter 侧生效后的完整快照，界面渲染回读值而非表单输入值（`available_memory_mb` / `compute_weight` / `accepting_tasks` 排空语义与 node-agent 一致）。
- **秘密防泄漏**：`assertNoSecrets()` 在每次快照发布时拒绝出现 token 类字段；测试同时断言 token 值不出现在任何快照、错误信息与 DOM 中。

## 凭据流向（方案，随原型实现）

1. 用户在连接页粘贴**注册 token**（一次性）→ 仅存在于表单内存 → 提交即传给 adapter → `finally` 中从输入框清除；不写 localStorage/sessionStorage、不回显、不进错误日志。
2. adapter 把注册 token 交给本地 Node 侧（Mock 直接丢弃；LocalHttp 桩 POST `/join` 到 localhost）→ node-agent 完成注册并把 `node_id`/`node_token` 原子持久化到状态目录（0600，既有实现）。
3. **node_token 永不进入 UI 进程**：界面只见 `node_id` 展示值；本地 API 快照结构中禁止 token 字段（由 `assertNoSecrets` 契约测试守护）。
4. 凭据被拒（401）映射为 `AUTH_REJECTED`：UI 明示「不自动重新注册，需人工处理」，对应 node-agent 退出码 2 的语义；Controller 不可达则保持重试并展示重试计数，两者不混淆。
5. 管理员 token 与本界面无关：任务创建/全量任务列表是 Controller 管理面能力，节点 UI 不申请。

## 待批准选项（未擅自实现）

### A. 界面框架（本轮原型不依赖任何一项）

| 选项 | 许可证/维护（2025-2026 公开资料） | 三平台覆盖 | 集成成本 | 备注 |
| --- | --- | --- | --- | --- |
| 纯 Web + 系统浏览器（当前原型） | 无依赖 | 三平台均可 | 最低 | Android WebView 与桌面浏览器表现需实测；无法后台常驻 |
| Tauri 2 | Apache-2.0/MIT 双许可，约 11 万 stars，活跃 | 桌面 + Android/iOS（v2 移动端） | 需 Rust 胶水层；系统 WebView 差异需兼容矩阵 | 包体小（<10MB）、默认拒绝能力模型，与最小权限方向一致 |
| Electron | MIT，Microsoft 维护，最成熟 | 仅桌面（无 Android） | 中 | 包体 150MB+、内存 200MB+；Node 全栈团队上手快 |
| Wails | MIT，Go 后端 | 仅桌面 | 需 Go | v3 破坏性变更中，生态小于前两者 |

本原型是零依赖 ES module + 静态资源，四种壳都能直接装载，框架决策可推迟到 LC-5/LC-3 批准时点而不报废本轮产物。

### B. 本地 Node API 与权限方案（LocalHttpNodeAdapter 桩假设的端点，均未实现、未批准）

| 选项 | 形态 | 优点 | 风险/成本 |
| --- | --- | --- | --- |
| 本地 HTTP + 回环凭据（桩当前假设） | node-agent 监听 127.0.0.1，token 写状态目录 0600，UI 每请求携带 | exo 等同类项目即「节点进程自带 localhost 仪表盘/API」模式，生态可参照；三平台 Webview 都能访问 | 需要定义端口协商、防其他本地进程读取 token（文件权限 + 回环绑定），属于 LC-5 冻结范围 |
| stdin/stdout JSON-RPC | UI 壳拉起 node-agent 子进程 | 无网络端口暴露面 | Android 前台服务与桌面壳生命周期耦合复杂；进程崩溃恢复路径多 |
| node-agent 内嵌仪表盘（exo 模式） | node-agent 直接 serve UI 静态资源 | 零跨进程、单二进制 | UI 与 agent 版本强绑定；Android WebView 集成方式需实测 |

桩中 `/join`、`/status`、`/resources`、`/execution/stop`、`/assignments` 仅用于表达边界形状，**不是协议提案的冻结**；端点、字段与鉴权在 LC-5 冻结前不得当作现有接口。

## 六类交互流与验收对照

| 交互流（LC-14 要求） | 原型实现 | 验证 |
| --- | --- | --- |
| 连接入网 | 表单校验 → connecting（重试计数）→ connected（node_id 回显）；401 显式终止不自动重试 | 单测 + 浏览器 E2E |
| 资源设置 | 内存/权重/接单开关，保存后渲染回读值；未连接/非法值显式报错 | 单测 + 浏览器 E2E |
| 节点/任务状态 | 连接、心跳（最后成功时间、连续失败数）、当前执行、最近分配表 | 单测 + 浏览器 E2E |
| 查看结果 | 分配状态列表；明示当前 /v1 不含模型输出（LC-8 闭环前不伪造结果） | 浏览器 E2E |
| 停止 | 按钮可用性由执行状态决定；确认框展示真实执行详情；stopping→stopped 由快照回报 | 单测 + 浏览器 E2E |
| 错误显示 | 顶部横幅（错误码+中文文案+处置提示）+ 持久错误日志列表 | 单测 + 浏览器 E2E |
| 断网/Controller 失败 | 心跳连续失败→offline，恢复→connected；连接期不可达→重试计数可见 | 单测 |
| 模型不支持 | 分配失败带 MODEL_UNSUPPORTED 显式原因，不假成功 | 单测 |
| 长任务不阻塞 | 全异步订阅快照，无同步等待 | 架构保证 + E2E |

## 未完成 / 未验证 / 风险

- **未接真实后端**：Mock adapter 之外的 LocalHttpNodeAdapter 是桩，node-agent 本地 API 未实现、未批准；「连接→入网→状态→结果→停止」的真实链路（真实 node-agent + Controller）未验证。
- **未做三平台实测**：本机仅 macOS（Chrome 无头）；Windows/Android 无设备验证，Android System WebView 下 ES module/`<dialog>`/Intl 时区行为未实测。
- **浏览器 E2E 局限**：验证的是 UI + adapter 契约在真实 Chromium 中的行为，不替代真实用户在真实设备上的流程验证。
- **LC-8 未闭环**：结果页只有状态没有模型输出，属预期缺口。
- **风险**：若本地 API 最终采用非 HTTP 形态，仅需替换 adapter 实现（契约不变），UI 层不动——这是本交付刻意保留的调整空间。

## 开源方案核对记录（公开资料，2026-10-12 检索）

- exo：节点进程自带 localhost 仪表盘与 OpenAI 兼容 API（`http://localhost:52415`），macOS 桌面 App 作为本地指挥中心——印证「本地 API + 本地 UI」模式可行；但其 API 无鉴权，LastCore 不能照抄，需回环凭据。
- Tauri 2 / Electron / Wails 维护状态与许可证见上表选项 A；资料来源为官方仓库/文档与 2025 年多篇对比评测，结论只用于选项呈现，不构成选型决定。
