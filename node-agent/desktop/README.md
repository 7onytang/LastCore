# 桌面节点启动 / 配置 / 安装（LC-12）

本目录承载 node-agent 在 macOS 与 Windows 桌面端的常驻运行配置：跨平台差异核对、启动/安装方案、可执行环境检查（`preflight.py`）与自启动配置模板。基线：`73fabf4`（node-agent 初版，权限锁跨平台修复为 `c33e531`），目标 main。

参考边界：复用 LC-4 已核实 exo 对照报告 v2，固定 exo `21a54c5ea0230a3bec1e1a786d200126c7e34ec6`（Apache-2.0）。报告记载 exo 为去中心化 Router/Worker 架构且推理绑定 MLX，与 LastCore 的 Controller + Node Agent 不兼容；本任务未复制 exo 代码或引入其依赖，仅使用 macOS `launchd` 与 Windows Task Scheduler 原生托管，因此没有新增 exo 代码版权/NOTICE 片段。

## 一、macOS / Windows 差异核对（基于 73fabf4 逐项验证）

| 维度 | macOS | Windows | 结论 / 已采取措施 |
| --- | --- | --- | --- |
| 状态目录默认值 | `~/.lastcore/node-agent`（`Path.home()` 解析为 `/Users/<user>`） | `C:\Users\<user>\.lastcore\node-agent` | 两端均可用；如需 Windows 惯例目录，在配置文件中填写展开后的绝对路径（例如 `C:\Users\<user>\AppData\Local\LastCore\node-agent`）；不在配置值中展开 shell 变量 |
| 凭据文件权限 | `chmod 0700/0600` 生效 | `os.chmod` 仅影响只读位，0600/0700 无效 | Windows 端依赖用户 profile 的 NTFS ACL 继承；preflight 会检查状态目录是否位于用户 profile 之下并告警越界放置 |
| 状态锁 | `fcntl.flock` | **73fabf4 直接 `import fcntl`，Windows 上 `ModuleNotFoundError`，agent 无法启动** | 已修复（c33e531）：Windows 走 `msvcrt.locking(LK_NBLCK)`，语义一致（独占、非阻塞、进程死亡自动释放），单测覆盖 |
| 退出信号 | `SIGINT`/`SIGTERM` 均可触发优雅停止 | `SIGTERM` 永远不会被系统投递；Ctrl+C 是 `SIGINT`；`taskkill`（不带 /F）对控制台程序无效，`taskkill /F` 为硬杀 | Windows 优雅停止：控制台 Ctrl+C，或先 drain（`NODE_AGENT_NO_ACCEPT`）再停止；Task Scheduler「停止任务」是硬杀，属已知限制（见下） |
| 凭据持久化 | 原子写（临时文件 + fsync + rename） | `os.replace` 原子性成立；但目标被占用（杀毒/同步盘扫描）时可能抛错 | 已知限制：重试由下一次操作覆盖；建议状态目录不放在同步盘内 |
| 网络代理 | `trust_env=False`，系统代理不影响 Controller 访问 | 同左 | 一致，无需处理 |
| 休眠/唤醒 | 进程冻结，唤醒后心跳线程按间隔恢复发送；TTL 内未续约会触发执行中任务 `heartbeat-stall` 取消；轮询循环持续重试 | 行为相同（纯 Python 逻辑，无平台分支） | 语义一致，见「休眠与网络恢复」一节；真实整机休眠恢复未在本轮验证 |
| 端口 | node-agent 本身不监听端口；接入 B 引擎（StageServer）后才绑定 `--endpoint` 端口 | 同左 | preflight 提供 `--endpoint` 端口占用检查 |
| 日志 | 控制台输出；LaunchAgent 重定向到 `~/Library/Logs/` | 控制台输出；任务计划可在包装脚本中重定向 | 日志含 node_id、不含 token（token 仅经 Authorization 头，不进日志/命令行） |

## 二、启动 / 安装配置方案

### 通用准备（两平台一致）

1. Python 3.12+；仓库 `node-agent/` 下建 venv 并安装依赖：
   - macOS：`python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
   - Windows：`py -3.12 -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt`
2. 创建私有配置目录并复制 `desktop/env.example` 到配置路径（macOS `~/.lastcore/env`；Windows `%LOCALAPPDATA%\LastCore\env`）。macOS 依次运行 `mkdir -p -m 700 ~/.lastcore`、复制文件、`chmod 600 ~/.lastcore/env`；Windows 配置文件放在当前用户的 `%LOCALAPPDATA%` 下，沿用该 profile 的 ACL。填写 Controller 地址、endpoint 和资源值。配置文件只接受 `NODE_AGENT_*` 的 `KEY=VALUE` 行，不执行 shell、不展开变量；POSIX 上文件必须为 0600。路径值写成绝对路径。**注册 token 只在首次入网前填写，注册成功（状态目录出现 `node-credentials.json`）后从 env 文件删除**。
3. 运行带 `--env-file` 的 `desktop/preflight.py` 确认环境可用（用法见下节）。
4. 首次入网前台运行 `python desktop/run_agent.py --env-file <私有配置文件>`。确认成功注册且凭据持久化后按 Ctrl+C 停止，再从配置文件移除注册 token；随后启用自启动。

### macOS：launchd LaunchAgent（用户登录自启）

1. 复制 `desktop/com.lastcore.node-agent.plist` 到 `~/Library/LaunchAgents/`，替换占位符：
   - `VENV_PYTHON_PLACEHOLDER` → `<repo>/node-agent/.venv/bin/python`
   - `RUN_AGENT_PLACEHOLDER` → `<repo>/node-agent/desktop/run_agent.py`
   - `ENV_FILE_PLACEHOLDER` → 私有配置文件绝对路径（例如 `/Users/<user>/.lastcore/env`）
   - `STDOUT_PATH_PLACEHOLDER` / `STDERR_PATH_PLACEHOLDER` → `/Users/<user>/Library/Logs/lastcore-node-agent.out.log` / `.err.log`
   `run_agent.py` 安全读取配置文件，不用 shell `source`，注册 token 不进入 plist 或进程参数。
2. 注册与控制：
   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lastcore.node-agent.plist   # 注册并立即启动
   launchctl bootout gui/$(id -u)/com.lastcore.node-agent                                   # 停止并注销
   launchctl kickstart -k gui/$(id -u)/com.lastcore.node-agent                              # 重启
   ```
3. 语义：`RunAtLoad` 登录自启；`KeepAlive.SuccessfulExit=false` 崩溃/非零退出自动拉起（节流 10s）；正常退出码 0 不会自动重启。注意退出码 2（凭据被拒，需人工处理）也会被拉起重试——这是有意的：凭据修复后即可恢复，无需手动操作。

### Windows：Task Scheduler 登录任务

1. 以目标用户运行（无需管理员），私有配置文件必须已经填写：
   ```powershell
   powershell -ExecutionPolicy Bypass -File desktop\windows\install-task.ps1 -EnvFile "$env:LOCALAPPDATA\LastCore\env"
   ```
2. `uninstall-task.ps1 -TaskName LastCoreNodeAgent` 注销；`Start-ScheduledTask LastCoreNodeAgent` 手动启动。
3. 任务在登录时启动（电池供电也运行，不设执行时限），失败自动重启 3 次（间隔 1 分钟）；每次启动都经 `windows/lastcore-node-agent.ps1` 包装，再由 `desktop/run_agent.py` 安全加载 env 文件。包装脚本从 `desktop/windows/` 向上两级定位 `node-agent/`，token 不出现在命令行。
4. 已知限制（Windows 无真机，以下为文档结论，未实测）：Task Scheduler「停止任务」为 `TerminateProcess` 硬杀，不触发优雅停止；重启后凭据/journal 机制保证已结束任务不重复执行，进行中任务依赖 Controller 的 deadline/cancel 收敛。

### 安装打包路线

- 阶段一（本轮交付）：源码 + venv + 本目录启动配置/脚本，无新增依赖，安装步骤完全可脚本化。
- 阶段二（打包）：PyInstaller 6.22.3（2026-09-12 发布，活跃维护；GPLv2-or-later + 特殊例外，允许用其产物以任意许可分发；支持 Windows x86-32/64/ARM64 与 macOS 10.15+ universal2；不支持交叉编译，需在目标平台分别构建）。打包为单目录 console 可执行后，仍用上述 launchd / Task Scheduler 托管。选型待实施前再次确认版本。
  - macOS 已实施（LC-12 阶段二，见下节「macOS 打包产物」）；实施前版本复核通过：PyPI 当前最新即 6.22.3，METADATA 记录 `License: GPLv2-or-later with a special exception...`、`Requires-Python: <3.16,>=3.8`。
  - Windows 打包未实施、未验证：PyInstaller 不支持交叉编译，必须在真实 Windows 主机上用 `lastcore-node-agent.spec` 重新构建（spec 平台无关，但构建流程 `build_bundle.sh` 目前仅允许 Darwin 主机，其余主机明确退出码 3 拒绝，避免把未验证构建当作通过）。

### macOS 打包产物（阶段二，已实施）

构建（可复现，全部在本机前台完成，不 sudo、不改系统设置、不做开发者身份签名/公证）：

```bash
cd node-agent
./desktop/build_bundle.sh          # 产物：dist/lastcore-node-agent/{lastcore-node-agent,lastcore-preflight}
```

- `build_bundle.sh` 建立独立 venv（默认 `node-agent/.venv`，可用 `BUNDLE_VENV`/`BUNDLE_PYTHON` 覆盖），从完整构建依赖锁定 `desktop/requirements-build-lock.txt` 以 `pip install --require-hashes` 一次装齐（运行闭包 httpx==0.28.1 及全部传递依赖 + 构建工具链 PyInstaller 6.22.3 / hooks-contrib 及其传递依赖，全部带 wheel sha256；锁定范围与再生成步骤见锁文件头注释——macOS arm64 / CPython 3.12 专用，不声称跨 Python/macOS 版本二进制 hash 必同），随后执行 `desktop/lastcore-node-agent.spec`（`--clean --noconfirm`），结束时打印产物大小与两个可执行文件的 sha256。测试专用依赖（pytest 等）不进入构建 venv：跑回归请用独立验证 venv `pip install -r requirements-dev.txt`。
- 发行包附带第三方许可材料：`desktop/collect_licenses.py` 在构建前从构建 venv 元数据推导冻结的运行依赖闭包并生成 `third-party-notices.md`（含各包准确版本、许可证、许可证全文所在路径；certifi 为 MPL-2.0，附 MPL-2.0 §3.2 源码获得说明），spec 经 PyInstaller `copy_metadata()` 把每个发行版的 dist-info（含许可证全文）与该 NOTICE 一并放进 `_internal/`；构建后脚本再以 Analysis TOC 核对实际冻结的第三方发行版集合与 manifest 一致、逐个许可证文件在产物内存在。验收可复跑：`python desktop/collect_licenses.py verify --manifest build/licenses/manifest.json --bundle-dir dist/lastcore-node-agent --workpath build/pyinstaller`。
- 若 pip 报 `SSL: UNEXPECTED_EOF_WHILE_READING` 且本机直连正常（macOS 系统代理损坏的典型表现），脚本经 `desktop/pip_no_sysconf_proxy.py` 驱动 pip：仅跳过 macOS SystemConfiguration 系统代理回退，标准环境变量代理（`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY`，大小写语义同 stdlib）行为完全保留，不修改任何系统配置；非 macOS 平台不做任何替换。
- 产物为单目录 bundle：两个 console 可执行共享 `_internal` 依赖目录。`lastcore-node-agent` 入口仍是 `desktop/run_agent.py`（`--env-file` 流程与源码运行完全一致，配置仍外部载入，token 不进二进制/命令行/日志）；`lastcore-preflight` 入口是 `desktop/preflight.py`。
- launchd 托管：使用 `com.lastcore.node-agent.bundle.plist` 模板（占位符 `BUNDLE_AGENT_PLACEHOLDER` → bundle 内 `lastcore-node-agent` 绝对路径、`ENV_FILE_PLACEHOLDER`/`STDOUT_PATH_PLACEHOLDER`/`STDERR_PATH_PLACEHOLDER` 同阶段一语义）。本仓库只提供模板，不代装、不注册开机项。
- 已知边界：冻结包内 `NODE_AGENT_EXECUTOR` 仅支持内置 `demo`（`module:attr` 自定义 executor 需把对应模块加入 spec 才可用）；产物按构建主机原生架构（Apple Silicon 为 arm64）生成，无 universal2；Apple Silicon 链接器强制 ad-hoc 签名，非开发者身份签名。
- 产物验证：`python desktop/smoke_bundle.py [--keep]` 以本机 127.0.0.1 受控 mock Controller fixture（非真实 controller-server 部署）实跑：两产物 `--help`、含空格路径的 env/state 下 preflight 通过、注册→心跳→轮询→demo 任务 completed 上报、运行中状态锁被占、SIGINT 优雅退出 rc 0、退出后锁可重新获取、凭据 0600/状态目录 0700、token 不出现在任何捕获输出。
- 文件锁备选：portalocker 4.4.0（BSD-3-Clause，活跃维护，Python >=3.10）。当前标准库 `fcntl`/`msvcrt` 方案零依赖已满足需求，portalocker 仅在出现 NFS/网络盘锁需求时再引入。

## 三、可执行检查 preflight.py

```bash
cd node-agent
python desktop/preflight.py --env-file <私有配置文件> [--min-memory-mb 2048] [--json]
```

检查项（任何 FAIL 退出码为 1）：Python >= 3.12；httpx 可导入；状态目录缺失的父级可创建、目录可写，权限位（POSIX 0700）或位于用户 profile（Windows）；状态锁原语可用 + 真实状态目录锁占用诊断；凭据文件存在、可解析且 POSIX 权限为 0600（只显示 node_id，永不显示 token）；endpoint 端口占用；Controller `/healthz` 可达；可用内存 / 磁盘剩余。配置文件键名受白名单限制且不执行；`--json` 输出供脚本/界面消费。

## 四、休眠与网络恢复语义（73fabf4 行为，本轮核对源码确认）

- **无网络 / Controller 不可达**：启动阶段每 1s 重试入网直到成功；运行中仅心跳失败计数与轮询告警，不退出、不影响本地执行。
- **休眠唤醒后**：进程冻结，唤醒后心跳线程在下一个间隔（默认 10s ≤ TTL 30s）恢复续约；轮询 1s 内恢复；执行中任务若因停机超过 TTL，会在下次轮询按 `heartbeat-stall` 取消，符合「设备休眠→任务超时取消」的既定语义；已持久化凭据复用，不重复注册。
- **重复任务**：journal 先本地落盘再上报，重启后同一 assignment 不重复执行（进程崩溃窗口内为 at-least-once）。
- **凭据被拒（401）**：退出码 2，不自动重新注册（防重复身份），需人工删除/修复状态目录凭据。

## 五、验证状态

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 单元测试（含 Windows 锁分支模拟） | 通过 | `pytest -q` 31 passed |
| preflight 实跑（macOS 本机） | 通过 | 见 LC-12 交付评论附件 |
| 真实流程：Controller + 双节点 + 任务执行/取消/停止/重启恢复（macOS） | 通过 | 见 LC-12 交付评论 |
| Controller 断连→恢复（macOS） | 通过 | 见 LC-12 交付评论 |
| 阶段二 macOS 打包构建（PyInstaller 6.22.3） | 通过 | 见 LC-68 交付评论（构建 rc/日志/产物 hash） |
| 阶段二 macOS 打包产物 smoke（--help/preflight/生命周期/SIGINT/锁/token 检查，本地 mock fixture） | 通过 | 见 LC-68 交付评论 |
| 阶段二 macOS 打包返工（LC-76：第三方许可清单落盘、代理保留 env 行为、构建依赖 hash 锁定） | 通过 | 见 LC-76 交付评论（干净锁定构建/许可核对/代理子进程验证/回归） |
| 阶段二 Windows 打包 | **未验证** | PyInstaller 不支持交叉编译，需真实 Windows 主机 |
| 真实整机休眠唤醒恢复 | **未验证** | 无权让共享设备休眠；语义经源码核对，待真机 |
| Windows 全部路径（安装/入网/任务/停止/重启） | **未验证** | 无 Windows 真机；锁分支有单测与源码核对 |
