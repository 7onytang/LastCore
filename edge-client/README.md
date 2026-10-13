# Windows/Linux linear edge client — development preview

This entry point reuses the existing node lifecycle and the contributor's
cross-platform state lock from PR70 (`4f698b9`, preserved by cherry-pick). It
executes the same bounded `tanh(Wx+b)` calculation as LastCoreClient, persists
the actual values before reporting completion, and displays only the local
node's connection, task and acknowledged result. There is no admin/fleet UI.

This is an **unsigned development preview, not verified runtime support**.
Native Windows/Linux portable folders have been compiled and statically checked
at source `85a1e23295dd64380a81f263a58da2593f7e94d5`, in
[build-only run 38391011563](https://github.com/7onytang/LastCore/actions/runs/38391011563).
Windows GUI, Linux GUI, user-device connection/computation and installer
acceptance remain pending. Separate native wheel hash locks are under
`packaging/locks/`; do not use the macOS dependency lock for Windows.

| Native artifact | Build/static result | Scope |
| --- | --- | --- |
| Windows x64, Python 3.13.16 | Passed, 17,632,491-byte ZIP, SHA256 `34ef2fbeffe95663c6ec707cde1368478bafbc3079649297170cdc3b56a7635a` | PE32+ AMD64, Tk resource/scripts and full terms verified; no program launched |
| Linux x64, Python 3.12.14 | Passed, 28,908,043-byte ZIP, SHA256 `fa57579d209bd9277d2847445a32fb65575fe0cc2d0ae9e7931c0f3b6900c1ea` | ELF64 x86_64, required GLIBC at least 2.35; no program launched |

The archive manifest binds 21 explicit runtime/build inputs to fixed Git blobs,
and includes bundled-file hashes, full licenses, exact CA bundle, OS/ABI and
signing scope. This documentation update follows the build source; it does not
relabel those compiled artifacts with a later documentation commit. Download
links below refer to the published development preview attachments on the existing v0.1.1 release.

## Published preview downloads

| Platform | Portable ZIP | Integrity and source |
| --- | --- | --- |
| Windows x86_64 | [ZIP](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-windows-x86_64-85a1e232.zip) | [SHA256](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-windows-x86_64-85a1e232.zip.sha256), [manifest](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-windows-x86_64-85a1e232.json) |
| Linux x86_64 (GLIBC >= 2.35) | [ZIP](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-linux-x86_64-85a1e232.zip) | [SHA256](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-linux-x86_64-85a1e232.zip.sha256), [manifest](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreEdge-0.2.0-preview-linux-x86_64-85a1e232.json) |

Unpack the portable folder without dropping its `_internal` or license files. These are unsigned development previews for bounded linear math only: no GGUF/LLM or tiny Stage execution. Native builds and static checks passed; native GUI launch, networking, user-device execution and Mac-to-Windows relay remain unverified. New runtime/device checks are paused at the user’s request. Publishing these attachments does not change the formal core tag, historical screenshots or fixed r2 submission archive.

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
