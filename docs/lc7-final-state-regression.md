# LC-7 现有契约回归与缺陷定位（基线 672b4f7）

状态：现有契约回归完成；**未实现任何 lease/epoch/鉴权新契约**（LC-5 人类批准缺失，停在准确缺口）。基线 HEAD `672b4f7474e424380645deaef2c1e8e68a7c2771`，父链见 git log。

## 一、回归范围与结果（独立退出码）

| 套件 | 覆盖 | 结果 | 退出码 |
| --- | --- | --- | --- |
| `controller-server/tests/test_final_state_delivery.py` | 终态窗口（>100 任务） | 1 passed, 1 xfailed | 0 |
| `node-agent/tests/test_fault_injection.py` | 断网/重启、重复/迟到回报、租约到期、取消 | 4 passed（连跑 3 次稳定） | 0 |
| `node-agent` 全套件 | 既有 36 项 + 新 4 项 | 40 passed | 0 |
| `controller-server` 全套件 | 既有 16 项基线 + 2 项新 | 17 passed, 1 xfailed | 0 |

场景→契约映射：**终态窗口**（`test_poll_window_caps_at_100_newest_tasks` 刻画现行为 / `test_cancelled_terminal_delivered_beyond_100_tasks` 编码目标行为）；**重复/迟到**（同状态幂等、乱序 409、终态冲突 409）；**断网/重启**（崩溃后同状态目录重启，身份复用不重复注册，at-least-once 重执行一次，journal 记 settled）；**租约到期**（心跳 TTL 过期任务 failed "assigned node heartbeat expired"，重连 accepted、无复活、迟到 completed 409、同状态 failed ack 幂等、资源释放可接新任务）；**取消**（运行中取消 → 执行器观察到取消、未跑满即停、journal 记 "stopped"、新旧任务不并发）。

## 二、本轮发现并修复的缺陷（测试 harness 层，非产品层）

原现场的两个测试文件最后一次写入后即因额度中断，从未完整运行。复跑发现 4/4 故障注入用例全部失败，**单一根因**：`Stack` 用 `httpx.ASGITransport` 驱动同步 `ControllerClient`，而 httpx 0.28 移除了同步 ASGI 支持（`'ASGITransport' object has no attribute 'handle_request'`）。这是测试 harness 的环境错配，不是产品缺陷。

修复：`Stack` 改为用 uvicorn 在回环临时端口上真实 serving 同一个 app，节点客户端走真实 TCP（与生产行为一致）；**全部断言保持原样，未删除、未放宽**。与原现场文件的差异仅限 imports 与 `Stack.__init__`（可 diff 核对）。节点 `requirements-dev.txt` 增加 `uvicorn==0.42.0`（与 controller-server 同版本，仅测试依赖）。

## 三、缺陷定位：终态投递超出 100 条轮询窗口（未实现，契约缺口）

**现象**：节点 210 个任务的历史中，第 101 新之前的未观察终态（如任务 101 的 cancelled）永远不出现在 `GET /v1/nodes/{id}/assignments`；节点 journal 永远不会记录它。

**精确定位**：
1. `controller-server/app/repository.py` — `task_ids_for_node(db, node_id, limit=100)`：`SELECT DISTINCT t.id ... ORDER BY t.created_at DESC LIMIT ?`，窗口外任务直接不可达；`limit` 参数存在但调用方（`store.assignments` → `TaskService.assignments`）从未传非默认值。
2. `controller-server/app/tasks.py` — `TaskService.assignments`：单次窗口查询，无"未观察终态"的补偿通道；响应行数 = 窗口行数（回归第一项已刻画：恰好 100 行）。
3. `node-agent` 侧 `agent.py` 的 settled journal 只能去重"已见过的" assignment；窗口外的 assignment id 从未进入轮询结果，journal 无从记录——即"节点无从观察"而非"节点丢弃"。

**为什么停在这里**：修复方向是"可靠终态投递/终态确认"，需改变 assignments 投递语义（如未观察终态补偿通道或游标），属 LC-7 目标中"按获批契约"的跨模块状态模型变更；LC-5 契约（D1–D6）尚未获人类批准。按本任务边界，不实现、不发明 lease/epoch/鉴权字段。

**xfail 真实去向**：`test_cancelled_terminal_delivered_beyond_100_tasks` 保持 `xfail(strict=False)`，本轮 1 passed / 1 xfailed 与原现场记录一致。该用例断言未删改；当获批契约实现后它应自然转 passed，同时 `test_poll_window_caps_at_100_newest_tasks`（现行为刻画）应随 `LIMIT` 窗口移除一并退役。两用例的协同关系写在测试文件 docstring 中。

## 四、复现

```bash
cd controller-server && pytest -q tests/test_final_state_delivery.py -rs        # 1 passed, 1 xfailed
cd ../node-agent && pytest -q tests/test_fault_injection.py                     # 4 passed
cd ../node-agent && pytest -q                                                   # 40 passed
cd ../controller-server && pytest -q                                            # 17 passed, 1 xfailed
```

环境：Python 3.14.7 / macOS arm64；httpx 0.28.1、fastapi 0.135.2、uvicorn 0.42.0、pytest 9.0.2、numpy 2.5.4。
