# LastCoreClient: targeted linear calculation

This native SwiftUI client executes `lastcore/linear-demo-v1` locally. It does
not execute the tiny TCP pipeline or a pretrained LLM. The three original
client files were imported from contributor Yutaotang's commit
`9fef3e632b23a018f61e5fe2c94c610df7d87959`, preserving the original Git author.
The original PR32 remains separate; its other workloads and showcase are not
included here.

The server accepts a bounded input vector and seed, requires an explicit
`target_node_id` and `max_nodes: 1`, and never falls back to another machine when
the target is offline, busy, draining, or lacks memory. The client computes
`tanh(Wx+b)` with the existing UInt32 weight generator. Version 0.1.1 shows the
actual numeric result and task ID only after the server acknowledges the same
saved result. Node credentials can only poll/report their own assignments;
fleet listing and task creation continue to require the server admin credential.

## Build and verify

Requires an Apple Silicon Mac, macOS 13+, and Swift 5.9+. No third-party Swift
dependencies are installed. Run `swift build -c release --package-path macos-client`.
For a real loopback HTTP check using the production Swift `ClientModel`, run:

```sh
python3 -m venv /tmp/lastcore-controller-verify
/tmp/lastcore-controller-verify/bin/pip install -r controller-server/requirements-dev.txt
/tmp/lastcore-controller-verify/bin/python macos-client/scripts/smoke_client.py --out /tmp/lastcore-swift-result.json
```

The smoke creates one fresh local node, runs one actual Swift calculation,
compares it with a Python reference, verifies persisted completion and published
UI model state, and cleans its processes and credentials. It does not touch the
user's Keychain/settings, dispatch a public task, or prove a screenshot or an
existing user's MacBook Pro has executed anything.

For the actual connected client, deploy the Controller patch first, keep the
Mac software connected, then submit one admin-authorized request:

```json
{
  "model_id": "lastcore/linear-demo-v1",
  "num_layers": 2,
  "layer_memory_mb": 1,
  "max_nodes": 1,
  "timeout_seconds": 120,
  "input_vector": [1, 2, 3, 4],
  "weight_seed": 7,
  "target_node_id": "replace-with-confirmed-node-uuid"
}
```

The old connected Swift client can execute and report this request after the
server patch, but only logs the output count. Install 0.1.1 to display the numeric
values. Confirm the same task ID and result in the client and the server, not
just HTTP 200 or a connected indicator. Activity times use Asia/Shanghai.

## Recovery-fix preview download

The [recovery-fix preview DMG](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreClient-0.1.1-arm64-5a83c1d6.dmg)
is built from `5a83c1d609af7181fe6a75812e1f70889f501fda`, merged through
[PR #83](https://github.com/7onytang/LastCore/pull/83) at
`83f046d916a2b03356d6792e81651ecc39d4186a`.
[SHA256](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreClient-0.1.1-arm64-5a83c1d6.dmg.sha256),
[build provenance](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreClient-0.1.1-arm64-5a83c1d6.json) and
[independent static audit](https://github.com/7onytang/LastCore/releases/download/v0.1.1/independent-static-audit-5a83c1d.json)
accompany the download. Its DMG SHA256 is
`6f7e2af998b6ff8dd790459c8205de2a7fa08d13f561ae8ac92a2aac19b0f70b`.

The application version remains `0.1.1`, Apple Silicon arm64/macOS 13+,
ad-hoc signed and not notarized. Eight pure URLProtocol Mock regressions,
optimized build and independent static artifact audit passed; the existing
smoke harness was compiled only. No GUI launch, Keychain access, real network
job, computation or new device acceptance was performed for this preview.

The [historical f9139762 DMG](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreClient-0.1.1-arm64-f9139762.dmg),
[original provenance](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreClient-0.1.1-arm64-f9139762.json) and
[checksum](https://github.com/7onytang/LastCore/releases/download/v0.1.1/LastCoreClient-0.1.1-arm64-f9139762.dmg.sha256)
remain unchanged. Its [historical MacBook Pro linear acceptance](../submission/evidence/mac-client-0.1.1-linear-acceptance.md)
does not establish runtime acceptance of the recovery preview. The formal
core tag remains `bf5b82e`, and the complete-source ZIP remains fixed at
`6977c0919c9df3323cb0ec52e2b8820646df8233`; neither includes this later fix.

## DMG

After committing the source, run
`python3 macos-client/scripts/build_dmg.py --output-dir /tmp/lastcore-client-release`.
The script uses macOS's existing `swift`, `codesign`, and `hdiutil` to produce an
arm64 read-only DMG, source binding JSON and SHA256 sidecar. This avoids new
packaging dependencies; researched alternatives were
[appdmg (MIT)](https://github.com/LinusU/node-appdmg) and
[create-dmg](https://github.com/create-dmg/create-dmg).
The package is ad-hoc signed and **not Apple notarized**. macOS trust or Keychain
prompts require the user's action; no security bypass is included.

### Owner production signing entry (not yet exercised)

The default command above remains a development build. An owner who has already
configured a Developer ID Application identity and an existing `notarytool`
Keychain profile can explicitly request production processing:

```sh
python3 macos-client/scripts/build_dmg.py --output-dir /tmp/lastcore-owner-production \
  --production \
  --signing-identity 'Developer ID Application: OWNER NAME (TEAMID1234)' \
  --team-id TEAMID1234 \
  --certificate-sha256 OWNER_PUBLIC_LEAF_CERTIFICATE_DER_SHA256 \
  --notary-profile OWNER_EXISTING_PROFILE
```

Replace the placeholders with owner-approved values. The certificate SHA256 is
of the public leaf certificate's DER bytes, not the certificate name or its
SHA1 identity selector. The script does not create/import identities, configure
profiles, accept passwords or export private keys. Running this opt-in command
**does** use the existing signing identity/Keychain profile and submit the DMG to
Apple; the owner must authorize that separate real operation. Missing or invalid
configuration fails before a build or signing command.

The pipeline uses a release Swift build, `codesign --options runtime --timestamp`
for the app, Apple Developer ID certificate requirements, actual authority/Team
ID/timestamp inspection, and extracted public certificate SHA256 comparison.
It signs and verifies the DMG, requires `notarytool submit --wait` JSON status
`Accepted` with a valid submission ID, then runs `stapler staple`, `stapler
validate`, and final DMG signature/integrity checks. It reuses Apple's tools
without additional dependencies; see [Apple's notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)
and the mature [MIT electron/notarize implementation](https://github.com/electron/notarize)
for the same tool-based approach. The Electron wrapper is unnecessary for this
Python/Swift project.

All candidates stay in a temporary directory. Only after every check succeeds
are the `-production.dmg`, JSON receipt and checksum exclusively created; existing
artifacts are never overwritten and failed publication removes its own files.
The receipt records the fixed Git commit, hashes and blob IDs of tracked client/license input
files exported from that commit into a private snapshot, actual signature checks, certificate fingerprint, Apple submission ID and
DMG ticket validation. The embedded source binding is generated **before** signing
and says `developer-id-requested`, `notarized: false`; the final external receipt
records the completed verification. The DMG ticket is stapled; the inner app is
signed and included in that notarized submission, not separately stapled.
Build inputs and manifest hashes come from the same fixed Git tree/blob objects,
not the live checkout or index; concurrent working-tree changes do not alter the
package. Regular tracked files are exported without Git filters or archive
attributes; symlinks/submodules fail closed. Captured tool failure output is
withheld from terminal errors.

Validation for this entry is synthetic only:

```sh
python3 -m unittest discover -s macos-client/scripts -p 'test_build_dmg.py' -v
```

The tests replace subprocesses and use synthetic files. No certificate, Keychain,
Apple service, real app build/launch or device calculation has been accessed by
these tests. This entry does not make existing preview DMGs production releases;
real owner signing, notarization and platform acceptance remain unverified.

## Compatibility and rollback

The server adds the independent `assignment_results` table without changing the
old eight-column `assignments` schema, so old clients and positional inserts
continue to work. Existing tasks still complete without numeric results. Only
linear tasks may report a finite, correctly sized result. Matching completion
retries are idempotent; conflicting retries are rejected and cannot replace the
saved values. Cancelled or expired tasks cannot be revived.

The preceding compatibility paragraph describes the historical introduction of
linear numeric results. Before rollout, preserve the previous image and back up
the database. For the current additive protocol, roll back code on the same live
data volume only when both versions are compatible with its schema; do not
restore a pre-deployment snapshot over subsequent writes. An incompatible
historical protocol rollback requires a separate migration or stopped-write
data recovery plan. No TLS,
admin/registration credentials, existing node IDs, or whole-model job protocol
are changed by this patch.

Validation reuses [Pydantic's bounded finite types](https://github.com/pydantic/pydantic/blob/main/docs/api/standard_library_types.md)
(MIT, already a project dependency) and existing SQLite transactions (public
domain); no ORM, queue framework or generic job service is added.

## Interrupted result recovery

The source client saves terminal outcomes before reporting them in
`~/Library/Application Support/LastCoreClient/pending-reports.json` (private
permissions, atomic replacement, no credentials). Records bind the exact
Controller origin, existing node identity, task, assignment, model and row
range. Reconnecting replays saved values before new work; it never converts
an unknown network outcome into a failed calculation. Completion is displayed
only after the Controller confirms the matching assignment and values.

A 404/409, malformed acknowledgement or damaged journal preserves the record
and prevents new work for that identity. Keep the journal and original
Keychain identity while investigating; do not delete them or rebind them to a
new server to bypass recovery. Stop cancels the old loop, retains pending
outcomes, and permits restart only after that loop has ended.

Run the URLProtocol fixture harness (also works with the command-line Swift
installation when XCTest is unavailable):

```sh
swiftc -parse-as-library macos-client/Sources/LastCoreClient/ClientModel.swift macos-client/Sources/LastCoreClient/PendingReports.swift macos-client/tests/RecoveryTests/RecoveryTests.swift -o /tmp/lastcore-swift-recovery-fixtures
/tmp/lastcore-swift-recovery-fixtures
```

The harness uses production sources;
no GUI, native calculation, Controller service or real network is started.
These cover interrupted ACK replay after restart, origin/identity isolation,
wrong/terminal ACKs, failed reports, corruption, running-report outages,
exact replay payloads and cancellation while awaiting completion ACK. A
build or fixture pass does not establish real device or network acceptance.
The implementation uses the existing system Foundation APIs, including
[atomic file replacement](https://developer.apple.com/documentation/foundation/nsdata/writingoptions/atomic).
The maintained [Swift Foundation](https://github.com/swiftlang/swift-foundation)
(Apache-2.0) and [SwiftNIO](https://github.com/apple/swift-nio) (Apache-2.0)
were reviewed; adding a network framework would not replace the required
application outcome journal, so no new dependency is introduced.

## Session and acceptance feedback

The native status text, indicator and controls bind to the production
`ClientModel` session state. Starting shows connecting until a successful
node-authenticated heartbeat returns the same node ID and the requested boolean
`accepting_tasks`. Registration alone is not a connected state. A session error
clears current confirmation and shows retrying; an initial registration failure
ends unconfirmed for manual retry, preserving the existing behavior rather than
silently creating another registration after an unknown response.

The acceptance toggle is the desired setting, separate from its heartbeat
confirmation. Each change advances a revision; an older in-flight heartbeat
cannot confirm a newer pause/resume, even if the value was toggled back. While
confirmation is pending the client does not claim new work. Pausing preserves
an in-flight task/report and only blocks the next assignment. A matching
heartbeat confirms the setting. Connection errors, delayed responses and stop
cannot optimistically publish confirmed acceptance.

Stop shows stopping and disables restart until the original loop actually exits
and performs its cleanup. It does not immediately replace the current task with
idle. Pending-result save/replay and exact ACK validation stay intact. The
recent-heartbeat label explicitly uses Beijing time; it denotes the last ACK,
not a guarantee of current connectivity. The validated session origin is frozen
for the session and omits user info, paths, queries and fragments. Server error
bodies are not displayed because they may echo sensitive configuration.

The resource summary reports the actual configured declaration used by this
client: 4096 MB advertised memory and compute weight 1.0. It explicitly labels
these as configuration, **not OS-measured free memory or a benchmark**. This
change does not add hardware measurement or new supported workloads.

The existing URLProtocol runner above now runs 17 production-model fixtures:
the eight recovery fixtures plus connecting/authenticated-heartbeat, network
retry/recovery, initial failure, malformed/wrong-node/wrong-boolean ACK,
revision isolation, confirmed pause/resume, paused in-flight report and stopping
with delayed ACK cases. It includes a synthetic server-detail privacy check.
No live network, GUI/app launch, Keychain access, native calculation, device,
DMG rebuild, signature or notarization is part of this validation. SwiftUI source
type-checking verifies the property bindings, not visual or real-device acceptance.
The implementation reuses Apple's [ObservableObject](https://developer.apple.com/documentation/combine/observableobject)
and [cooperative task cancellation](https://developer.apple.com/documentation/swift/task/cancel%28%29)
without adding dependencies.
