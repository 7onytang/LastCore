# LC-7 可靠取消与节点执行租约：状态转换表与故障注入方案

状态：**提案 v0.1 草案，未获批**。本文档不是已冻结契约；任何字段、状态或接口在 LC-5 契约批准前不得实现到 `/v1` API 或 Node 生命周期路径（LC-7 停止条件）。

- 业务日期：2026-10-11（Asia/Shanghai）；任务：LC-7（依赖 LC-6；本轮为首批可独立验证产物）。
- 现状基线：`672b4f7`（agent/engineer-a-a/b33d0fac979a，含 PR #1 控制平面 ee57405、PR #2 dcd189e 文档/CI、node-agent 990d390..672b4f7）。Engineer-B 待审分支 449b5c4/fd6a9cf（communication/inference-engine cancel/deadline 修复）**不在**本基线内，其行为差异未纳入本文断言。
- 行号引用均对应基线 672b4f7。

## 1. 现状盘点（取消 / journal / 终态投递的真实行为）

### 1.1 Controller（controller-server/app/store.py）

| 机制 | 现状 | 位置 |
|---|---|---|
| 持久 journal | SQLite WAL；事务入口先执行 `_expire` 再处理请求，过期结果即使请求被拒也会持久化 | store.py:48-65, 26 |
| 节点存活的隐式租约 | 无显式租约对象；`last_seen` 距今超过 heartbeat TTL 即视为失联：活跃任务整体置 failed（"assigned node heartbeat expired"）。重连只是刷新 `last_seen`，**没有 epoch/ fencing token**，身份与在途 assignment 原样保留 | store.py:72-81, 116-121 |
| 任务终态 | queued → running → completed / failed / cancelled；取消幂等并级联把 pending/running assignment 置 cancelled | store.py:16, 67-70, 207-215 |
| 终态投递给 Node | Node 轮询 `GET assignments`：按 `created_at DESC LIMIT 100` 返回最近 100 个任务的行，含 `task_status`。注释声称"直到节点观察到"，但**没有观察/ACK 字段**，滑出窗口的终态对节点永久不可见 | store.py:169-182（store.py:172 注释） |
| 回报幂等 | 同状态重复回报 → 200 幂等返回；终态后不同状态回报 → 409 "assignment is terminal"；跨节点回报 → 404 | store.py:184-205 |
| 过期处理触发 | 仅在"任一事务"发生时被动触发；无 API 请求时不主动扫描（依赖下一次请求或 Node 心跳/轮询） | store.py:55 |
| 管理端列表 | `GET /v1/tasks` 同样 `LIMIT 100`，审计可见性共享同一窗口缺口 | store.py:164-167 |

### 1.2 Node（node-agent/lastcore_node_agent/）

| 机制 | 现状 | 位置 |
|---|---|---|
| 身份 journal | `node-credentials.json` 原子写、重启复用同一 node_id/token（重连免注册） | credentials.py:27-78; agent.py:62-76 |
| 执行去重 journal | `journal.json` 保存最近 512 条 `(assignment_id → status)`；重启后已 settle 的 assignment 不再执行。**settle 发生在回报之前**：若终态回报重试全部失败，本地已记 settled 而 Controller 无终态记录，重启也不会补报 | credentials.py:81-100; agent.py:281-286, 288-305 |
| 终态回报 | `report_retries` 次退避重试；404/409 视为"不再接受"放弃；网络错误耗尽重试后仅记录日志放弃 | agent.py:288-305 |
| 停止路径 | 轮询内看门狗：deadline 到期 / 心跳停滞超 TTL / `task_status` 为 cancelled/failed → `request_cancel`；Executor 按 slice 检查 cancel 事件 | agent.py:170-179; executor.py:52-58 |
| 停止确认 | 取消后本地记 `stopped`，**不回报任何取消确认**；Controller 侧确认 = 任务早已置 cancelled，投递确认只靠轮询可见性 | agent.py:267-271 |
| 并发约束 | 单活跃执行（`self._active`）；新任务只在旧执行停止并被 reap 后启动 | agent.py:34-40, 146-158 |

### 1.3 数据面（communication / inference-engine）

- CANCEL 帧、每 0.25s cancel 轮询、绝对 deadline：channel.py:25-47, 79-146；StageServer 空闲短轮询响应 `should_stop`/cancel：pipeline.py:139-156。
- **tensor 帧不携带 task/assignment/epoch 标识**（protocol.py 帧头仅 name/dtype/shape 等）：任务隔离只靠连接生命周期；上一轮尝试的迟到 tensor 在协议层无从拒绝。

## 2. 与 LC-7 验收对照的缺口

| 验收要求 | 现状结论 |
|---|---|
| 超过 100 个任务仍能收到应处理终态 | **不满足**。终态命令受 `LIMIT 100` 窗口限制，滑出即丢失（tests/test_final_state_delivery.py 已用 210 个任务的用例固定该缺口，xfail） |
| 断线重连与 Controller/Node 重启不丢失状态 | 部分。Controller 状态经 WAL 持久 ✓；Node 身份/settled journal 持久 ✓；但 Node 端 "settle 先于 ACK" 使终态回报可能永久丢失；窗口内不可见的终态重启后也无法补看 |
| 重复与乱序幂等 | 满足（同状态 200 幂等，异状态 409，已用真实 Controller 断言） |
| 过期 epoch 的 tensor/report 被拒 | **不满足**。无 epoch 概念：节点租约过期后重连，旧执行结果仍按普通 report 处理（过期仅当任务已被 `_expire` 置终态时间接 409）；数据面 tensor 无任何隔离标识 |
| 取消后新任务不能与旧执行并发 | 控制面满足（取消即释放资源，Node 单活跃 + journal 顺序）；无显式"停止确认"记录 |
| 无 API 请求时超时仍按获批时限处理 | 不满足被动扫描；依赖下一次事务触发 `_expire`（store.py:55） |

## 3. 开源方案核对（公开资料，2026-10-11 检索）

| 参考 | 与本提案的关系 |
|---|---|
| Kubernetes Lease（coordination.k8s.io/v1：holderIdentity / leaseDurationSeconds / renewTime；节点心跳与选主用） | 证明"心跳续租 + 时长声明"是成熟模式；Apache-2.0 文档，概念引用，不引入依赖 |
| Fencing token（M. Kleppmann "How to do distributed locking"，DDIA §8；源自 Chubby/lease 文献 Gray & Cheriton SOSP'89） | 每次(重)获租约发严格递增 token，存储方拒绝 token 倒退的写；同时明确：**fencing 不能物理停止被暂停/不可信的节点**，只能让后续迟到写被拒——与 LC-7 停止条件一致 |
| ZooKeeper zxid/znode version、Kafka producer epoch | 同类"代数号拒绝僵尸写入"先例；本提案用 SQLite 单写者事务内自增，无需引入新组件 |

结论：在现有 SQLite + HTTP 轮询架构上以"租约 + 单调 epoch"最小实现，不引入消息中间件/新数据库，符合 architecture-plan v0.1 的模块边界（contracts 载体、controller 管租约、node 去重执行）。

## 4. 提案状态机（**未获批，不实现**）

### 4.1 节点租约（proposed `nodes.lease_epoch`, `nodes.lease_expires_at`）

| 当前态 | 事件 | 动作 | 新态 | 可观察 |
|---|---|---|---|---|
| 无租约 | register | 分配 epoch=1、expires=now+TTL | HOLDING(e=1) | 201 返回租约字段 |
| HOLDING(e) | heartbeat 在 TTL 内 | 刷新 expires，epoch 不变 | HOLDING(e) | 200 |
| HOLDING(e) | TTL 内无心跳（被动扫描或下一事务触发） | 标记 LAPSED；活跃任务按现规则失败 | LAPSED(e) | 节点视图 offline |
| LAPSED(e) | 心跳恢复（同一身份） | epoch+1 重新授予；**该节点全部在途 assignment 的 stale epoch 记为 e** | HOLDING(e+1) | 200 响应携带新 epoch |
| HOLDING(e) | 终态命令全部确认且无活跃任务 | 可继续续租或 drain | HOLDING(e) | accepting_tasks=false → draining |

### 4.2 执行 epoch（proposed `assignments.epoch`，即 attempt/generation）

| 当前态 | 事件 | 动作 | 新态 | 迟到输入处理 |
|---|---|---|---|---|
| PENDING | 首次派发 | epoch=e0 随 assignment 下发 | RUNNING(e0) | — |
| RUNNING(e) | report(同 epoch, 同状态) | 幂等 200 | RUNNING(e) | — |
| RUNNING(e) | report(同 epoch, 终态) | 落终态 + 记 pending-delivery | TERMINAL(e) | — |
| RUNNING(e) | report(旧 epoch) | **拒绝 409**（或专用 410），不改状态 | RUNNING(e) | 迟到上报被隔离 |
| RUNNING(e) | tensor 帧(旧 epoch/旧 task) | 数据面**拒绝接收**（需契约冻结帧字段后实现） | RUNNING(e) | 迟到 tensor 被拒 |
| RUNNING(e) | 节点租约 LAPSED 后重获 e+1 | assignment 保持/controller 重派时 epoch=e+1 | RUNNING(e+1) | 旧尝试输出全部失效 |
| TERMINAL(e) | 任意迟到 report | 现规则：同状态 200 幂等，异状态 409 | TERMINAL(e) | 与现状一致 |

### 4.3 终态可靠投递与取消确认（proposed 投递/确认）

| 侧 | 现状缺口 | 提案（需批准） |
|---|---|---|
| Controller | `assignments()` 固定 100 窗口，无 ACK 字段 | 终态命令带 `pending_delivery` 标记；`assignments()` 先返回未确认终态（不受窗口限制）；同状态 report 或专用 ack 将其置 delivered |
| Controller | 无 API 请求时超时不处理 | 引入到期被动扫描（获批的时限参数），或在 Node 心跳/轮询必经路径保证触发频率 |
| Node | settle 先于 ACK，回报可能永久丢失 | journal 增加 pending-report 态：回报被 Controller 接受后才记 settled；重启后先补报 pending 项再领取新任务 |
| Node | 取消无显式确认 | 现轮询确认继续有效（task_status=cancelled → 本地停止）；可选新增 cancel-ack 回报字段，Controller 据此记录 stop_confirmed；未确认前该节点不参与新任务派发 |
| 双方 | fencing 语义边界 | epoch 只保证迟到**输入**被拒；不承诺物理停止不可信节点（见 §3） |

## 5. 可执行故障注入方案（本轮已交付，全部可跑）

| # | 场景 | 测试 | 基线结果 | LC-7 实现后 |
|---|---|---|---|---|
| 1 | 210 个任务下，未观察的取消终态被窗口吞掉 | `controller-server/tests/test_final_state_delivery.py::test_cancelled_terminal_delivered_beyond_100_tasks`（xfail）+ `::test_poll_window_caps_at_100_newest_tasks`（特征化） | xfail（缺口已固定） | 目标断言转正，特征化测试随 LIMIT 一起退役 |
| 2 | Node 执行中崩溃→重启（真实 NodeAgent + 真实 Controller app，ASGI 进程内） | `node-agent/tests/test_fault_injection.py::test_node_restart_mid_execution_reexecutes_once` | 通过：at-least-once 重执行一次、终态最终一致 | 增加 epoch 断言：重获租约需先再获取，旧尝试输出被拒 |
| 3 | 重复/乱序/冲突终态回报 | `::test_duplicate_and_out_of_order_reports_are_idempotent` | 通过：同状态幂等 200，异状态 409 | 保持；report 增带 epoch 字段 |
| 4 | 心跳 TTL 过期→迟到 completed 上报→重连→新任务 | `::test_late_report_after_lease_expiry_rejected` | 通过：过期任务 failed、迟到 completed 409、重连不复活、新任务可建 | epoch 递增使旧尝试在重连后即失效（不依赖任务已终态） |
| 5 | 取消→真实停止→资源释放→新任务不与旧执行并发 | `::test_cancel_stops_execution_and_new_task_runs_after` | 通过：cancel 幂等、Executor 实际提前停止、started 顺序 [A,B] 无重叠 | 增加 stop_confirmed 显式记录 |

故障注入方法：真实 `NodeAgent`（非 mock）通过进程内 ASGI 传输打到真实 `Store + create_app`，双方共享 `[1000.0]` 可控时钟（Store `clock` 与 NodeAgent `clock` 同源），因此 TTL/到期行为可精确推进而无需真实等待；Executor 为可注入的 `HoldExecutor`（挂起以制造崩溃点）/`SliceRecorder`（记录实际执行片数）。

## 6. 验证命令

```bash
# 基线回归（四模块全绿）
(cd controller-server && python -m pytest -q)   # 16+2 用例
(cd node-agent && python -m pytest -q)          # 36+4 用例
(cd inference-engine && python -m pytest -q)
(cd communication && python -m pytest -q)

# 本轮新增
(cd controller-server && python -m pytest tests/test_final_state_delivery.py -v)
(cd node-agent && python -m pytest tests/test_fault_injection.py -v)
```

node-agent/requirements-dev.txt 新增测试专用依赖 `fastapi==0.135.2`（与 controller-server 同版本，仅测试导入真实 Controller app，不影响运行时依赖）。

## 7. 未实现 / 未验证 / 风险

- **全部提案字段与状态机未实现**：等待 LC-5 契约批准与 LC-6 重构固定基线；本轮仅交付对照表、用例与注入方案（LC-7 首批产物）。
- 数据面 epoch 拒收（tensor 帧携带 task/assignment/epoch）无法在契约冻结前实现；现状 tensor 帧无隔离标识（§1.3）。
- `GET /v1/tasks` 管理列表同样 `LIMIT 100`（store.py:167），审计可见性缺口未单独立用例。
- Node journal 上限 512 条（credentials.py:13）：超长运行 + 大量任务时旧 settled 记录被挤出，与 100 窗口叠加可能重复执行极老任务；契约冻结时一并定容量。
- 崩溃注入为进程内放弃对象（非真实 kill -9）；真实进程级重启在 LC-11 双节点系统验证覆盖，缺物理设备项如实列为未验证。
- Engineer-B 待审分支的 cancel/deadline 修复不在本基线；其合入后需复跑 §6 命令并复核断言。
- 本文档引用的 k8s Lease / fencing 文献为 2026-10-11 公开网页核验；许可证均为概念引用，不复制代码，不改变项目许可决策。
