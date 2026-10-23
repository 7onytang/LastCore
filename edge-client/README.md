# Windows/Linux linear edge client — development preview

This entry point reuses the existing node lifecycle and the contributor's
cross-platform state lock from PR70 (`4f698b9`, preserved by cherry-pick). It
executes the same bounded `tanh(Wx+b)` calculation as LastCoreClient, persists
the actual values before reporting completion, and displays only the local
node's connection, task and acknowledged result. There is no admin/fleet UI.

This is an **unsigned development preview, not verified runtime support**.
Native Windows/Linux portable folders have been compiled and statically checked
at source `63d409a8ed313befe6f55e83feaca94afde4b8aa`, in
[build-only run 38517716454](https://github.com/7onytang/LastCore/actions/runs/38517716454).
This source includes the GUI immutable acknowledged task/result snapshot and
explicit result-owner labels. Its 19 pure Mock checks are not native GUI or device
acceptance. The current published artifacts received independent static approval.
Windows GUI, Linux GUI, user-device connection/computation and installer
acceptance remain pending. Separate native wheel hash locks are under
`packaging/locks/`; do not use the macOS dependency lock for Windows.

| Native artifact | Build/static result | Scope |
| --- | --- | --- |
| Windows x64, Python 3.13.16 | Passed, 17,630,729-byte ZIP, SHA256 `8f26b53fd751c4c9b6e9f25a9070ba63b3e5198f51c5153b3794c7dbf7e9b5eb` | PE32+ AMD64, Tk resource/scripts and full terms verified; no program launched |
| Linux x64, Python 3.12.14 | Passed, 28,909,760-byte ZIP, SHA256 `55517667466da9cbfe46f41e5cc72b278af14b245194c683a1da1bbd34ef3468` | ELF64 x86_64, required GLIBC at least 2.35; no program launched |

The archive manifest binds 21 explicit runtime/build inputs to fixed Git blobs,
and includes bundled-file hashes, full licenses, exact CA bundle, OS/ABI and
signing scope. This documentation update follows the build source; it does not
relabel those compiled artifacts with a later documentation commit. Download
links below refer to the published development preview attachments on the existing v0.1.1 release.

## Published preview downloads

| Platform | Portable ZIP | Integrity and source |
| --- | --- | --- |
| Windows x86_64 | [ZIP](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-windows-x86_64-63d409a8.zip) | [SHA256](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-windows-x86_64-63d409a8.zip.sha256), [manifest](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-windows-x86_64-63d409a8.json) |
| Linux x86_64 (GLIBC >= 2.35) | [ZIP](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-linux-x86_64-63d409a8.zip) | [SHA256](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-linux-x86_64-63d409a8.zip.sha256), [manifest](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-linux-x86_64-63d409a8.json) |

Unpack the portable folder without dropping its `_internal` or license files. These are unsigned development previews for bounded linear math only: no GGUF/LLM or tiny Stage execution. Native builds and static checks passed; native GUI launch, networking, user-device execution and Mac-to-Windows relay remain unverified. New runtime/device checks are paused at the user’s request. Publishing these attachments does not change the formal core tag, historical screenshots or fixed r2 submission archive.

## Historical preview source85a1e232

The previous unsigned previews remain unchanged as historical attachments:
[source85a1e23295dd64380a81f263a58da2593f7e94d5/build38391011563](https://github.com/7onytang/LastCore/actions/runs/38391011563).
Their build/static evidence did not verify native GUI, networking, computation or
relay, and cannot be transferred to the current63d409a8 packages. The GUI ownership
fix belongs to63d409a8; use the current download table above for that fix.

| Historical artifact | Preserved ZIP / SHA256 / manifest | Size and SHA256 |
| --- | --- | --- |
| Windows x86_64 | [ZIP](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-windows-x86_64-85a1e232.zip), [SHA256](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-windows-x86_64-85a1e232.zip.sha256), [manifest](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-windows-x86_64-85a1e232.json) | 17,632,491 bytes; `34ef2fbeffe95663c6ec707cde1368478bafbc3079649297170cdc3b56a7635a` |
| Linux x86_64 | [ZIP](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-linux-x86_64-85a1e232.zip), [SHA256](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-linux-x86_64-85a1e232.zip.sha256), [manifest](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-linux-x86_64-85a1e232.json) | 28,908,043 bytes; `fa57579d209bd9277d2847445a32fb65575fe0cc2d0ae9e7931c0f3b6900c1ea` |

## Native build-only portable previews

`Edge portable build only` runs on Windows x64 with Python 3.13.16 and Ubuntu
22.04 x64 with Python 3.12.14 (both are available in GitHub's official toolchain
manifest). It installs the separate pinned wheel SHA256 locks from
`packaging/locks/`, reuses maintained PyInstaller Tk/certifi hooks, and builds
two entry points in one portable folder. Its steps collect full installed
Python/Tcl/Tk/dependency terms plus the actual OpenSSL version's upstream terms
and notices. No private config or credentials are embedded. No Authenticode
signature, Linux package installation or production-signing claim is made.

CI inspects PE/ELF headers, transported license and CA bundle hashes, fixed Git
source hashes, ZIP CRC and native dependency metadata. Linux's required GLIBC
symbol version and external shared libraries are measured with `readelf`, not
assumed from the runner name. Artifacts include the portable ZIP, external
manifest and checksum. Native executable compilation and static integrity
**do not prove that the GUI launches, the node connects or computation works**.
Neither entry point, smoke script nor any task/model/device acceptance is run
by this workflow. Preview attachments have been published separately as linked above.

Build scripts: `packaging/prepare_licenses.py`, `packaging/portable.spec` and
`packaging/static_manifest.py`. These tools write only build/release folders.
Runtime acceptance and installers remain pending; the old macOS development
checks below cannot substitute for native-platform execution evidence.

## Run from source

Requires Python 3.12+ and the OS's Tk support for the optional GUI:

```sh
python -m pip install -r edge-client/requirements-runtime.txt
python edge-client/gui.py
```

Exit the old client first. Select its existing private `NODE_AGENT_*` environment
file and confirm the **original issuing HTTPS origin**. The existing state path
and node credential files are retained. This client refuses a missing existing
identity, a conflicting origin or an already locked state directory; it never
registers a replacement node to bypass a failed connection. The configuration
and credential files stay on the device and must not be committed or sent to an
admin chat. Windows state-file confidentiality relies on the user's protected
profile ACL; POSIX mode bits are not an NTFS permission guarantee.

For a console session:

```sh
python edge-client/linear_edge.py --env-file /path/to/existing/private.env --bind-existing-origin https://8.134.161.123
```

The origin confirmation is needed only for an existing identity without a local
linear-origin binding. The input file is parsed without shell execution; the
explicit linear entry point chooses the real linear executor, even if that old
file describes demo or stage mode. Other workloads are rejected, not simulated.
Only a server-admin request for `lastcore/linear-demo-v1` with this node's explicit
`target_node_id` is eligible for this test path. This does not execute GGUF/LLM
inference or a tiny TCP pipeline. Activity display uses Beijing time.

## Evidence and remaining work

Before the instruction to stop further tests, 34 targeted executor/state-lock
tests passed on macOS. A real isolated HTTP Controller and this source client
also completed one linear task, persisted/published its values, matched the
reference with error 0, denied node fleet access, and released its process/state
lock. These are macOS development checks, **not native Windows/Linux or physical
Windows device proof**. The user's Windows node was visible earlier but later
had stale heartbeats; no task was dispatched to that offline node.

The intended relay is Mac output → Windows input → final reference comparison.
It has not been executed. The separate existing MacBook Pro linear tests cannot
substitute for the Windows stage. New real tasks/device checks are paused at the
user's request. The published native previews were built separately on each target OS
(PyInstaller is not a cross-compiler), carry Python/Tcl/Tk/dependency licenses
and source/artifact hashes, and state the Linux GLIBC baseline.

Researched reusable components: existing MIT LastCore client math/lifecycle,
[CPython `msvcrt` locking](https://github.com/python/cpython/blob/main/Doc/library/msvcrt.rst),
and [PyInstaller](https://github.com/pyinstaller/pyinstaller) (GPL with its
distribution exception). No new task broker or server protocol is introduced.


## Result display ownership

The GUI separates the active/recent task from the last cloud-acknowledged result.
After ACK validation, LinearController publishes one immutable task-ID/value
tuple; the GUI reads that snapshot once rather than reading the old two fields.
Every displayed value is prefixed with its completed task ID, so running task B
cannot imply that the saved values of A belong to B. Starting a new Session and
refreshing a session with no acknowledged result resets the result label instead
of retaining the previous session's text. Empty results or missing ownership are
not displayed as acknowledged values; zero-valued results remain valid. Preview
formatting keeps the existing16-value bound and labels truncation explicitly.

The view reuses CPython/Tk's existing StringVar update mechanism and this module's
pure view-state helpers, with no new runtime dependency or controller authority.
Verification is pure Mock/view-state and static HTML only; it does not launch Tk,
start a worker, create a task, access credentials or validate a native device.
Published preview archives and their source identities are unchanged by this
source-only fix.

## 本机暂停/恢复与状态确认

既有配置/GUI 启动方式保持不变。portable 仍只执行有界线性任务、复用现有节点身份；不切换到 Python desktop 的 tiny/demo backend。界面显示实际已加载会话的安全 Controller origin、配置上报内存/权重及逻辑核心数；它们不是当前内存占用测量。

暂停/恢复直接调用同进程 NodeAgent 的接单接口，不重启或停止当前任务。接单请求先显示“待确认”，只有最新 request ID 对应的心跳 ACK 才显示已暂停/接收任务；断网、清理和恢复屏障保持可见。近期错误仅显示安全原因码。未建立会话时点击停止直接显示已停止，不留下永久的停止中状态。任务与上一份已确认结果仍按各自任务 ID 分开显示。

生产 Session observer/回调和标签的离线回归（不创建 Tk 窗口、网络连接、真实 worker 或模型）：

```sh
(cd edge-client && python -m pytest -q tests/test_gui_view.py tests/test_admission_view.py tests/test_runtime_binding_inputs.py)
```

新增运行依赖 `local_status.py`、`local_control.py` 已列入 portable 的必需源码绑定清单；没有重生成锁、许可证或历史预览包。Fake 控件只承接真实回调输出，不等于完整 Tk GUI 或真实设备验收。

### Cross-platform text regression

Source-text readers in the callback/binding tests explicitly use UTF-8. The
callback regression also simulates a legacy Windows cp1252 default on every OS,
so a missing encoding fails locally on macOS/Linux instead of first failing in
Windows CI. Do not hide the issue by globally forcing PYTHONUTF8 or skipping the
Windows job. Before pushing callback changes, run the same portable checks from
the same directory as the Desktop controls workflow:

```sh
cd edge-client
python -m pytest -q tests/test_gui_view.py tests/test_admission_view.py tests/test_runtime_binding_inputs.py
```

These are pure callback/Fake-core checks: no Tk window, actual model, device,
Controller service or external network is started. Platform CI remains the
final check; a local pass does not establish that every platform passed.
