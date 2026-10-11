# LC-8 输入与生成结果闭环（准备阶段产物）

Engineer-B 在 LC-8 “补齐输入与生成结果闭环” 的准备范围内交付的对照与验证材料。**不改动任何模块源码**：样例通过模块公开接口驱动现有实现。

| 文件 | 内容 |
| --- | --- |
| `gap-analysis.md` | Executor/ExecutionContext ↔ StageServer 对照；输入/执行/结果三段差距清单；embedding/norm/head/sampling/tokenizer 归属；固定 file:line 证据 |
| `expectations.md` | 多步生成、结果幂等、取消与迟到结果 三类场景的固定输入、可判定预期与复现命令 |
| `input_result_loop_harness.py` | 输入→执行→结果调用样例 + 自动判定（真实 Executor 契约 + 两段 TCP 链路） |
| `controller_terminal_checks.py` | 控制平面终态语义复核（幂等上报、取消后迟到报告被拒） |
| `evidence/*.json` | 上述两个脚本在固定基线 SHA 上的真实输出 |

## 运行

两个脚本通过环境变量定位隔离 checkout：

```bash
EXT=<含 venv 与 wt-engine / wt-node / wt-ctrl2 的目录>

LC_REPO_ROOT=$(pwd) \
LC_ENGINE_DIR=$EXT/wt-engine/inference-engine \
LC_COMM_DIR=$EXT/wt-engine/communication \
LC_NODE_AGENT_DIR=$EXT/wt-node/node-agent \
  python input_result_loop_harness.py --out evidence/results-loop.json

LC_REPO_ROOT=$(pwd) LC_CONTROLLER_DIR=$EXT/wt-ctrl2/controller-server \
  python controller_terminal_checks.py --out evidence/results-controller.json
```

依赖：numpy、pytest、httpx、fastapi、uvicorn（版本同各模块 `requirements*.txt`）。

## 状态

准备阶段产物；**不代表接口已冻结**，也**不等于整个 issue 交付**（多步 decode/KV、tokenizer、sampling、Controller 端结果出口均未实现，见 `gap-analysis.md` 第 4、6 节）。
