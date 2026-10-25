# Client installation, upgrade, rollback and removal

This guide covers the current native Mac app, Python desktop entry points and
Windows/Linux linear portable preview. It documents operator steps for
[#51](https://github.com/7onytang/LastCore/issues/51) and rollback boundaries for
[#54](https://github.com/7onytang/LastCore/issues/54); it is not evidence that a
clean-machine installation or credential revocation has been exercised.

## Choose the actual client

| Client | Install/run entry | Current limits |
| --- | --- | --- |
| Native Swift `LastCoreClient.app` | [Mac README](../macos-client/README.md), DMG copied to Applications | Apple Silicon, macOS 13+; bounded targeted linear math. Published recovery preview is ad-hoc signed, not Developer ID signed or notarized. |
| Python browser desktop / tiny bundle | [Desktop README](../node-agent/desktop/README.md), `desktop/ui.py` or the complete `lastcore-node-agent` bundle | Explicit tiny `stage` or simulated `demo`; not the Swift app or a pretrained LLM. The existing macOS arm64 bundle requires macOS 14+ and lacks Developer ID signing/notarization. Source Windows/Linux launchers are not proof of platform installation acceptance. |
| `LastCoreEdge` GUI / `LastCoreLinearEdge` console | [Edge README](../edge-client/README.md), complete portable folder | Windows x86_64 / Linux x86_64 (GLIBC >= 2.35), unsigned development previews, bounded linear math only. Existing identity required; no first registration. Native build/static checks do not establish GUI, networking or device execution. |

The [llama.cpp worker](../real-model-runtime/README.md) and
[Android app](../android-app/README.md) have separate lifecycles. Switching a
Python env file to a different executor does not turn these desktop clients
into the native Mac app, Android app or whole-model worker. There is no shipped
MSI, automatic updater or general-purpose uninstall/reset action here. Owner
production-signing preparation does not change the status of published previews.

## Record and verify before installing

1. Use the module README's current download table. Keep the artifact, SHA256
   sidecar and provenance/manifest together. Record the full source commit,
   architecture, OS minimum, version and actual signing status. A version label
   alone is insufficient: different source-bound previews can share a version.
2. Compare the downloaded file's SHA256 with the published sidecar, for example:

   ```sh
   shasum -a 256 /absolute/path/LastCoreClient-preview.dmg
   ```

   On Windows PowerShell:

   ```powershell
   Get-FileHash -Algorithm SHA256 -LiteralPath 'C:\Downloads\LastCoreEdge-preview.zip'
   ```

   Replace example paths with the selected artifact. A checksum establishes
   correspondence to the published hash, not publisher identity, notarization
   or runtime compatibility. Keep normal OS trust checks; do not disable them.
3. Keep private env/config files and state outside a replaceable application
   directory. On POSIX use private permissions; on Windows use a protected local
   user profile with appropriate NTFS ACLs, not a shared or synchronized folder.
   The env-file parser does not expand `$HOME` or `%LOCALAPPDATA%` inside values;
   use the actual absolute state path. Do not copy secrets into a report or PR.

## Identity and data that must survive replacement

| Client | Identity/configuration | Recovery data to preserve |
| --- | --- | --- |
| Native Mac | Generic-password Keychain item: service `ai.lastcore.node`, account `credentials:<Controller URL with leading/trailing / removed>`. Saved settings use the app's UserDefaults domain `ai.lastcore.client`: `controllerURL`, `nodeName`, `endpoint`. The registration input is not a saved UserDefaults setting. | `~/Library/Application Support/LastCoreClient/pending-reports.json`, bound to the original Controller origin and node. Keep the original Keychain item alongside the journal; do not rebind it to another server. |
| Python desktop / node CLI | `NODE_AGENT_STATE_DIR` from the private env file or CLI override; default `~/.lastcore/node-agent` (`C:\Users\<user>\.lastcore\node-agent` on Windows). `node-credentials.json` contains the identity/token. The private env file is a separate operator-chosen path. | Preserve the **whole** state directory: `journal.json`, `pending-reports.json`, `registration.pending` if present, `agent.lock`, generated weights and any other executor state. Also retain externally configured checkpoint/config files. |
| Linear portable | Same configured Python state and existing credentials; `linear-origin.json` pins the confirmed original issuing origin. | Preserve the whole state directory, including `linear-results/`, `last-linear-result.json`, origin binding and core journals. These are saved outcomes, not disposable download caches. |

For native builds run outside the packaged app, UserDefaults can use the
executable's domain; confirm the actual installation instead of deleting a
guessed preferences file. Keychain access can prompt after app/signing changes;
the user handles that prompt locally. Do not export or display token values.

The Python state lock is an open-file OS lock (`fcntl` on POSIX, `msvcrt` on
Windows), not the mere presence of `agent.lock`. Never delete/rename a lock file
or copy credentials to a new state directory to bypass a running owner. Keep
only one client using an identity; the Swift app does not share this Python lock.
Back up stopped state privately on the same trusted device with its permissions;
do not clone a live identity to a second machine. Keep the Keychain item in place
for an ordinary Mac upgrade rather than exporting it into a file.

## Stop before upgrade or removal

1. Pause new admissions using the client's supported control, and wait for its
   matching heartbeat confirmation. A pending toggle, offline label or old ACK
   does not confirm a pause. Do not start new work during the maintenance window.
2. Let active work finish and its saved result receive a matching acknowledgement,
   or follow the existing authorized cancellation/recovery procedure. Preserve
   pending outcomes on an outage; a missing ACK is not permission to discard them.
3. Request Stop and wait for real cleanup, then quit the application/launcher.
   Native Mac must finish its original session, not remain in “stopping”. In the
   browser desktop, closing the browser alone does not stop the child process;
   use Stop and wait, then exit the launcher. A six-second UI wait expiry is not
   cleanup confirmation. In the current portable GUI, both the agent loop and
   executor thread must finish; “停止未确认 · 工作线程仍在运行” blocks replacement.
   For a console entry use its supported Ctrl+C shutdown and wait for exit.
4. Verify the original worker/process has ended and no other owner holds the
   Python state lock before replacing binaries or taking the stopped-state
   backup. A returned agent loop, a Controller terminal task or heartbeat TTL
   alone does not prove execution stopped. If cleanup is unconfirmed, keep the
   state and old installation intact for investigation; do not force a new client
   past the barrier. Historical preview binaries may predate current stop guards.

If you previously installed a launchd/Task Scheduler template yourself, disable
that exact user job's automatic restart before maintenance, then confirm its
worker has stopped. The repository does not install those jobs automatically.
The Windows `uninstall-task.ps1` only unregisters the selected scheduled task;
it neither proves the process stopped nor removes files or revokes credentials.
Do not treat Task Scheduler's forced termination as a graceful shutdown.

## Install or replace

**Native Mac:** mount the verified DMG, copy `LastCoreClient.app` to Applications
after the old app has stopped, and eject the image. Preserve the verified old
DMG/receipt for an eligible rollback. Open the copied app through the normal OS
flow, keeping the same Controller origin and Keychain identity. First enrollment
requires an administrator-provided registration credential entered locally;
an unexpected request to register an existing installation needs investigation,
not a new identity to bypass inaccessible Keychain/recovery data.

**Python source desktop:** use a separate private venv and fixed source checkout
with the requirements declared in the [desktop guide](../node-agent/desktop/README.md).
Retain the private env file and its original state path. Run the documented
`desktop/ui.py --env-file /absolute/private/node.env` entry from `node-agent/`.
For an existing frozen tiny bundle, unpack the entire replacement folder in a
new location, preserving `_internal` and licenses; point the launcher/template
to that folder only after cleanup. Do not mix files from different builds.
An unexpected first-enrollment/recovery barrier requires the
[enrollment recovery procedure](../node-agent/ENROLLMENT_RECOVERY.md), not deletion
of `registration.pending`, credentials or journals.

**Windows/Linux linear portable:** unpack the complete verified ZIP into a new
versioned folder. Launch `LastCoreEdge.exe` on Windows (`LastCoreEdge` on Linux),
select the existing private env file, and confirm the independently verified
original issuing origin when the local binding is absent. This entry refuses
missing credentials; a clean machine without an enrolled identity is not yet a
complete install/onboarding path. Do not use tiny/demo registration as a shortcut
to manufacture a device acceptance result. Preserve both executables, `_internal`
and license files; replace shortcuts only after the old session has stopped.

Before resuming admissions, verify the selected binary/source identity and
same node/origin, no recovery barrier, and a fresh authenticated heartbeat for
the desired admission setting. Do not dispatch a task merely to validate an
upgrade without separate authorization. Replaced files and a connected indicator
do not establish clean-machine install/upgrade acceptance.

## Rollback gate

Keep the old artifact and its receipt, but do not assume an older client can
read newer recovery records. Stop again before rollback. Review both versions'
protocol, journal format, origin binding and workload support against the current
Controller. Use the current source's documented
[typed-result downgrade gate](../node-agent/README.md#explicit-cpu-demo-workload-capability)
where applicable: pending typed outcomes or an unverifiable journal block an
older worker. A server database check alone cannot inspect remote journals.
Do not delete unknown fields or pending records to make a gate pass.

Do not roll the native Mac app back past the pending-report recovery fix while
it has unresolved saved outcomes. Linear portable state also needs compatible
result replay; an older client must not recompute or overwrite a saved outcome.
If compatibility is unproven, keep the installation stopped and preserve the
state for a reviewed recovery plan. Do not restore an old state backup over
results acknowledged after that backup. Client rollback does not roll back the
Controller database or authorize an old server image, schema downgrade or live
database snapshot restore. Avoid historical artifacts with known dependency
security defects merely because their GUI version looks the same.

## Uninstall: choose retention or explicit cleanup

After the stop/cleanup steps, remove the Mac app via Finder, or remove only the
selected Python venv/bundle/portable folder and its own shortcuts. Remove an
operator-installed startup job separately. The default retention choice keeps
the private env file, Keychain identity and all state/recovery data for later
compatible reinstall; removing application files does not remove them.

For permanent local cleanup, first have the owner/admin reconcile pending tasks,
unknown enrollment outcomes and retained results. Explicitly choose which private
backup, config, state directory and logs to retain or remove; do not delete all
of `~/.lastcore`, shared model directories or user-supplied checkpoints. For native
Mac, use Keychain Access locally to identify only the matching service/account
above before deleting that item; handle `LastCoreClient`'s Application Support
data and its specific settings domain separately. Do not reveal/export the item
or erase unrelated Keychain entries. Removal can make pending outcomes impossible
to report, so unresolved journals remain a recovery task, not automatic cleanup.

## Credential retirement: current missing capability

The Controller currently has **no node-token refresh/revoke or node-removal API**.
The registration secret only authorizes creating a node; existing node requests
authenticate against the stored token hash. Heartbeat TTL changes availability,
not token validity. Consequently none of these proves server-side revocation:

- deleting the app, Keychain item, env file or `node-credentials.json`;
- stopping heartbeats, seeing “offline”, pausing admissions or cancelling a task;
- rotating the registration secret or enrolling a replacement identity.

For retirement or suspected compromise, keep the client stopped, protect the
remaining state and privately tell the administrator the node ID and original
Controller origin, without sending its token. The administrator must reconcile
active assignments/outcomes and arrange an explicitly reviewed server-side
revocation or containment procedure. This repository supplies no approved
per-node command or database-edit recipe; do not improvise SQL deletion or claim
that a re-registration invalidates the old token. If no server-side control is
available, record revocation as **pending**, and keep any administrator-approved
containment distinct from actual credential invalidation.

After such a capability/procedure exists, the administrator must verify at the
intended Controller that the retired credential is rejected on its protected
node operations, cannot restore availability or fetch/report assignments, and
that unrelated identities/recovery records remain valid. Verification must use
the approved secure procedure without logging tokens or generating real work.
No such operation or verification was performed by this documentation change.
Until then, local uninstall is possible, but #51's credential retirement and
full signed clean-machine lifecycle acceptance remain open.

## Basis and verification limits

Paths and behavior above follow [Swift identity/settings](../macos-client/Sources/LastCoreClient/ClientModel.swift),
[bundle identity](../macos-client/scripts/build_dmg.py),
[Python configuration](../node-agent/lastcore_node_agent/config.py),
[state/lock implementation](../node-agent/lastcore_node_agent/credentials.py),
[linear identity/results](../edge-client/linear_edge.py) and the
[Controller routes](../controller-server/app/main.py)/[token authentication](../controller-server/app/store.py).

The approach reuses [Apple's app removal flow](https://support.apple.com/en-gb/102610),
[PyInstaller's complete-folder distribution](https://pyinstaller.org/en/stable/operating-mode.html)
and [OWASP's server-side session invalidation guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).
PyInstaller is the project's existing maintained packager (GPL with its bundling
exception); OWASP's maintained guidance is CC BY-SA 4.0. These are references, not
copied implementation/text. A new installer/updater dependency would not solve
the existing credential-revocation and recovery compatibility gaps, so none is
introduced. This guide has source/link checks only: no real app, signing service,
Keychain, credential deletion, device, model, production upgrade or uninstall was
accessed. New demo/ZIP materials remain deferred.
