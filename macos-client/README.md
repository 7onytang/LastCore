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

## Compatibility and rollback

The server adds the independent `assignment_results` table without changing the
old eight-column `assignments` schema, so old clients and positional inserts
continue to work. Existing tasks still complete without numeric results. Only
linear tasks may report a finite, correctly sized result. Matching completion
retries are idempotent; conflicting retries are rejected and cannot replace the
saved values. Cancelled or expired tasks cannot be revived.

Before production rollout, back up the Controller database, finish/cancel any
new linear task, and preserve the previous image and database snapshot. The old
image ignores the added table but cannot receive a new linear request or a
numeric report; restore the snapshot when reverting that protocol. No TLS,
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
wrong/terminal ACKs, failed reports, corruption and stop/restart fencing. A
build or fixture pass does not establish real device or network acceptance.
The implementation uses the existing system Foundation APIs, including
[atomic file replacement](https://developer.apple.com/documentation/foundation/nsdata/writingoptions/atomic).
The maintained [Swift Foundation](https://github.com/swiftlang/swift-foundation)
(Apache-2.0) and [SwiftNIO](https://github.com/apple/swift-nio) (Apache-2.0)
were reviewed; adding a network framework would not replace the required
application outcome journal, so no new dependency is introduced.
