# Windows/Linux linear edge client (in development)

This entry point reuses the existing node lifecycle and the contributor's
cross-platform state lock from PR70 (`4f698b9`, preserved by cherry-pick). It
executes the same bounded `tanh(Wx+b)` calculation as LastCoreClient, persists
the actual values before reporting completion, and displays only the local
node's connection, task and acknowledged result. There is no admin/fleet UI.

This is **source-level work in progress, not a verified Windows/Linux release**.
No native Windows/Linux executable or installer has been produced. Windows
GUI, Linux GUI, native-platform packaging, dependency/license closure and user
device acceptance remain pending. `requirements-build.txt` identifies the
existing PyInstaller tool versions for later native builds; it is not a complete
platform-specific hash lock. Do not use the macOS dependency lock for Windows.

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
user's request. Future native packaging must run PyInstaller on each target OS
(it is not a cross-compiler), carry Python/Tcl/Tk/dependency licenses and exact
source/artifact hashes, and state the Linux glibc baseline.

Researched reusable components: existing MIT LastCore client math/lifecycle,
[CPython `msvcrt` locking](https://github.com/python/cpython/blob/main/Doc/library/msvcrt.rst),
and [PyInstaller](https://github.com/pyinstaller/pyinstaller) (GPL with its
distribution exception). No new task broker or server protocol is introduced.
