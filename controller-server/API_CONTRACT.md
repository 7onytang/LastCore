# Agent / Controller contract and security boundaries

Source baseline: `2f66bcbffc30eb80821652fd5c5799df5262cd50`.
This is a source inventory for [SPEC-01 #40](https://github.com/7onytang/LastCore/issues/40)
and [SPEC-02 #41](https://github.com/7onytang/LastCore/issues/41), not a claim that
their original acceptance criteria or platform/device verification are complete.
Changes to this contract must be checked against the implementation and reviewed.
The baseline is the code reviewed here, not a claim about the running server.
The additive own-assignment lookup below extends that baseline; it requires an
upgraded Controller and node agent.

## Schema source and verification

Request schemas come from [models.py](app/models.py), routes/authentication from
[main.py](app/main.py), and response dictionaries/state changes from
[store.py](app/store.py). The optional whole-model namespace uses
[inference_jobs.py](app/inference_jobs.py) and [REAL_JOBS.md](REAL_JOBS.md).
FastAPI's `app.openapi()` exposes request schemas, routes and Bearer security.
Most routes return untyped dictionaries: generated OpenAPI alone does **not**
describe their response fields or every runtime error. The tables below supply
that missing source inventory; there is no checked-in generated client or
automatically enforced response-schema compatibility gate.

Reuse decision: use the existing FastAPI/Pydantic schema machinery
and the [OpenAPI HTTP Bearer/response definitions](https://spec.openapis.org/oas/v3.1.0.html),
following [FastAPI's OpenAPI generation documentation](https://fastapi.tiangolo.com/how-to/extending-openapi/).
This avoids a second handwritten executable schema or a new framework/dependency.
The Controller already pins FastAPI `0.135.2`; upstream
[FastAPI](https://github.com/fastapi/fastapi/blob/master/LICENSE) and
[Pydantic](https://github.com/pydantic/pydantic/blob/main/LICENSE) use MIT licenses.
This reuses the installed application's schema API rather than introducing a
potentially incompatible generated-code runtime or a new version upgrade.
The generated document describes this application's implementation, not a new
protocol version or an assurance that all responses are typed.

For a local, offline schema check, install the existing Controller requirements
as described in the root README. From `controller-server/`, use a temporary
database and explicit test credentials; do not load a production `.env`:

```python
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from app.main import Settings, create_app

with TemporaryDirectory() as directory:
    for enabled in (False, True):
        settings = Settings("a" * 32, "r" * 32,
                            str(Path(directory) / f"schema-{enabled}.sqlite3"),
                            real_jobs_enabled=enabled)
        schema = create_app(settings).openapi()
        print(json.dumps({"real_jobs_enabled": enabled, "paths": sorted(schema["paths"])}))
```

This initializes only a temporary SQLite database; it starts no listener, node,
executor or model. Full field constraints remain inspectable in
`schema["components"]["schemas"]`.

## Authentication, endpoints and response shapes

Every protected request uses `Authorization: Bearer <credential>`. Registration
and administrator credentials must be distinct and at least 32 characters.
The server stores a SHA256 digest of each randomly generated node token; only
registration returns the plaintext token. Tokens are not interchangeable roles.
There is no refresh/revoke endpoint in this baseline.

| Method/path | Credential | Success | Response |
| --- | --- | --- | --- |
| `GET /healthz` | none | 200 | `{status:"ok"}`; process health, not job readiness |
| `POST /v1/nodes/register` | registration | 201 | `{node_id,node_token,heartbeat_ttl_seconds}` |
| `GET /v1/nodes` | admin | 200 | `{nodes:[Node]}` |
| `POST /v1/nodes/{node_id}/heartbeat` | this node | 200 | `Node` |
| `GET /v1/nodes/{node_id}/assignments` | this node | 200 | `{assignments:[Assignment + task_status,request,deadline]}` |
| `GET /v1/nodes/{node_id}/assignments/{assignment_id}` | this node | 200 | one `Assignment + task_status,request,deadline`, independent of history window |
| `POST /v1/nodes/{node_id}/assignments/{assignment_id}/report` | this node | 200 | full `Task`, including its assignments |
| `POST /v1/tasks` | admin | 201 | full `Task`; no creation idempotency key |
| `GET /v1/tasks` | admin | 200 | `{tasks:[Task]}`, latest 100 |
| `GET /v1/tasks/{task_id}` | admin | 200 | full `Task` |
| `POST /v1/tasks/{task_id}/cancel` | admin | 200 | full `Task` |

`Node` fields: `id,name,platform,endpoint,available_memory_mb,compute_weight,
accepting_tasks,last_seen,supported_workloads,status`. `status` is
`offline/busy/available/draining`; it is derived from heartbeat age and legacy
task reservation, not proof of native execution or a whole-model lease state.
Token hashes are omitted. `Task` fields: `id,request,status,created_at,deadline,
error,assignments,result,workload_result`. `Assignment` fields:
`id,task_id,node_id,position,layer_start,layer_end,status,error,endpoint,
previous_node,next_node,result,workload_result`. Peers are
`{node_id,endpoint,id}` or null. Layer ranges are zero-based `[start,end)`.

Polling selects the latest 100 related **tasks**, including terminal tasks, then
returns only this node's assignments. There is no durable delivery cursor or
acknowledged command queue. Task times and deadlines are UTC epoch seconds;
display/business interpretation uses `Asia/Shanghai`.

The single-assignment lookup authenticates the node token before checking the
assignment ID and restricts the lookup to that node's ownership. An unknown ID
and an ID owned by another node both return `404 {detail:"assignment not found"}`;
a wrong/missing token returns 401. It returns only the owned assignment using the
same fields and peer metadata as a list row, not the full task's other results.
It performs one addressed task lookup in a transaction, introduces no delivery
queue or event retention, and leaves the existing latest-100 list unchanged.

When its known active assignment is absent from a successful history poll, the
upgraded node agent queries that ID. A matching `cancelled`/`failed` task requests
local cancellation. The worker must still finish before the agent releases its
local active slot or starts another task. Receiving a terminal state is not a
physical-stop acknowledgement. This protects a **known active assignment**;
it does not guarantee delivery or journaling of all historical terminal tasks.

Upgrade the Controller and agent to obtain this protection. With an old
Controller, FastAPI's route-missing `404 {detail:"Not Found"}` (or 405/501) causes
a warning and legacy history-only fallback cached until agent restart. Restart
the agent after upgrading the Controller. During fallback, cancellation beyond
the latest-100 window can still wait for the local deadline/heartbeat watchdog.
An endpoint's unknown-assignment 404, network failure, malformed response or
other server failure does not acknowledge cancellation, release the worker or
disable future lookup retries; 401 retains the existing credential rejection
path. Old agents remain history-window limited even against a new Controller.

## Request validation

All request models inherit `StrictModel`: unknown fields and non-finite numbers
are rejected. The name does not mean every scalar is a strict JSON type:
Pydantic coercion remains for fields without explicit strict validation.

| Model | Fields and bounds (defaults in parentheses) |
| --- | --- |
| `NodeRegistration` | `name`:1–128 chars; `platform`:1–64; `endpoint`:HTTP(S) URL ≤2083 chars; `available_memory_mb`:1–10,000,000; `compute_weight`:>0–1,000,000; `accepting_tasks`:(true); `supported_workloads`:up to 2 entries from `pi/log_stats` (empty) |
| `Heartbeat` | `available_memory_mb`:0–10,000,000; `accepting_tasks`:(true); same `supported_workloads` (empty) |
| `TaskRequest` common | `model_id`:1–256 chars; `workload`: `linear/pi/log_stats` (`linear`); `num_layers`:1–100,000; `layer_memory_mb`:1–10,000,000; `max_nodes`:1–128 (8); `timeout_seconds`:10–86,400 (600) |
| Linear fields | optional `input_vector`:1–256 finite values each within ±1,000,000; `weight_seed`:0–2,147,483,647; `target_node_id`:UUID |
| π fields | `total_samples`:strict integer 1,000–100,000,000; seed required; partitions cannot exceed samples |
| Log fields | `log_lines`:1–10,000 strings, each ≤4096 characters, ≤1,000,000 characters total; `num_layers` must equal line count |
| `AssignmentReport` | `status`: `running/completed/failed`; `error`:optional ≤2048 chars; `result`:optional 1–4096 finite values in [-1,1]; `workload_result`:optional dictionary ≤4 keys, strict integer values 0–100,000,000 |

π/log requests additionally reject bool/float/string values for the listed integer
budget/seed fields, and reject irrelevant linear/log/π fields. Targeted
`lastcore/linear-demo-v1` requires vector, seed and target, `max_nodes=1`, at most
4096 rows. These limits are admission bounds, not measured RAM guarantees.
The scheduler checks self-reported free memory against `num_layers ×
layer_memory_mb`, not a manifest-derived activation/KV/native-memory estimate.

Completed linear reports must contain every assigned output row. Completed π
reports require exactly `inside,samples` with the assigned sample count; log
reports accept only ERROR/WARN/INFO/OTHER keys with counts summing to the assigned
line count. Other statuses/workloads must not supply these result fields.
Tiny logits travel back through the data channel and are not stored in Task
results. Linear values and π/log aggregates are stored separately.

## Lifecycle, retries and compatibility

Task states: `queued → running → completed`, or `failed/cancelled`. Assignments
start `pending`, then `running/completed/failed/cancelled`. A failed shard,
expired task, or expired assigned-node heartbeat fails the active task and its
unfinished assignments. A completed shard remains reserved until the whole task
terminates. Cancel immediately releases the legacy scheduling reservation;
Controller cannot prove or force physical cleanup on that path.

Heartbeat TTL defaults to 30 seconds (server setting 5–3600 seconds, returned at
registration). Expiry runs at startup, during Store transactions, and in a
background sweep with a default 1-second wait (configurable 0.1–60 seconds).
Database lock/work duration can delay that sweep; it is not a real-time stop
guarantee. Startup reconciliation failure rejects startup; background SQLite
errors are logged and retried on the next interval. Offline nodes returning
later do not resume failed tasks automatically.

Same assignment status with identical saved result fields is idempotent and
returns Task; a conflicting result or different state after terminal is 409.
The free-form `error` string is not part of that same-status conflict check.
Repeated cancel of an already cancelled task returns Task; cancel of a different
terminal state is 409. Retrying task creation or registration creates a new
identity/task if the earlier request actually succeeded: unknown outcomes require
operator reconciliation, not assumptions about exactly-once creation.
The legacy Agent does not persist an enrollment pending marker before register;
its join retry can therefore create duplicate nodes after a lost registration
response. The separate whole-model workers have their own enrollment recovery
barriers. This legacy gap remains under #43/#44.

The Python Node Agent implementation is in
[agent.py](../node-agent/lastcore_node_agent/agent.py),
[controller.py](../node-agent/lastcore_node_agent/controller.py) and
[credentials.py](../node-agent/lastcore_node_agent/credentials.py):

- Startup reuses validated private credentials and the state-directory lock.
  Corrupt credentials/journals stop work without silently replacing evidence.
  Persisted-credential 401 is fatal; it does not automatically re-register.
- Join retries transport/Controller errors after 1 second; heartbeat defaults to
  10 seconds, poll to 1 second, HTTP timeout to 10 seconds. Poll failures use the
  configured fixed interval, not exponential backoff. Terminal reports have
  three attempts and a default 0.5-second wait between failed attempts, then
  retain pending records for replay.
- A live process suppresses duplicate delivery by active/settled/pending IDs.
  Pending terminal reports are durable before settlement; restart replays them
  instead of recomputing. The legacy execution journal is bounded to 512 IDs;
  the launch-to-durable-terminal crash window is not an exactly-once guarantee.
- Expired/terminal assignments do not launch. An independent watchdog requests
  cancellation on deadline/heartbeat TTL even if polling is blocked. A compliant
  Executor checks its cancel event and releases resources. Shutdown waits the
  configured `stop_grace` (10 seconds); an uncooperative daemon thread is marked
  `stop-unconfirmed` and abandoned. This is not native-process fencing.
- π/log result replay verifies task, assignment, node, model, layer range,
  request hash and saved result in the returned ACK. Unknown/mismatched ACK,
  including 404/409, retains the typed result and blocks new work. Legacy
  status-only pending reports clear on 404/409. These are distinct guarantees.

Compatibility is explicit, not inferred from a platform name. Old heartbeats
omitting `supported_workloads` clear prior π/log capability claims. New π/log
Agents need the compatible Controller first: an older strict request model
rejects the new heartbeat field. Legacy `linear` request serialization omits the
new workload/π/log fields to preserve previously published request hashes.
There is no negotiated protocol version beyond these route/field conventions;
the FastAPI application version `0.1.0` is not an artifact/release version.

## Model manifest and rejection layers

There are three separate execution families. The approved whole-model manifest
and reproducibility/known hardware record are in [MODEL_SELECTION.md](MODEL_SELECTION.md).
No remote assignment has a shell command, executable path, Python module factory,
model-download URL or arbitrary-code field. `module:factory`, local model config
and local weights are trusted operator CLI options, never assignment inputs.
They are not a sandbox for a hostile operator or hostile local installation.

| Family | Actual acceptance/rejection layer | Limits and unresolved boundary |
| --- | --- | --- |
| Legacy `linear`, including tiny/layer identifiers | Controller accepts any nonempty bounded `model_id` unless it conflicts with a known typed workload. `StageExecutor` checks assignment against its locally configured model/layer plan. Legacy demo simulates slices and does not reject every unknown model. | **No global Controller model allowlist or downloadable manifest**. Identifiers do not trigger downloads. Unknown legacy IDs are not uniformly rejected; tracked by #41. |
| `lastcore/linear-demo-v1` | Controller validates targeted vector/seed/one-node bounds; compatible native/edge linear executors perform bounded arithmetic. | This is not GGUF or tensor relay; a heartbeat alone does not prove compatibility. |
| π/log | Controller model/workload pairing plus declared node capability; built-in demo executor independently validates bounded payload. | No unknown typed model or task type; malformed requests are 422. New device execution remains separately unverified. |
| Whole-model `/v1/inference` | Literal fixed model/source hashes in `Capability/JobRequest/Result`; worker checks pinned model/backend before each native run. | Whole model on one device; local-file integrity is not hardware attestation or protection against concurrent trusted-owner tampering. |

Remote model sources and executable fields are absent rather than fetched then
screened. Extra fields such as `command`, `executor` or `model_url` are 422.
The task API cannot select the trusted local custom executor; its CLI loader can
import operator-selected code. Tiny NPZ weights are selected through trusted
local configuration, rather than a remote task-supplied file reference.
Peer endpoints are node-supplied; Controller does not check reachability or make
them authenticated. The tiny TCP protocol has size/dtype/hash/handshake checks,
but **no peer identity authentication or TLS**. Use only a controlled trusted
network; no arbitrary discovery mechanism is implemented. Transport/security
and two-physical-device acceptance remain [#49](https://github.com/7onytang/LastCore/issues/49).

## Error inventory

HTTP failures use FastAPI's `{detail: ...}` response, a string for explicit
HTTPException or structured validation details for schema errors. There is no
uniform machine-readable application error-code enum on `/v1/tasks`. Clients
must branch on status and context, not treat mutable English strings as codes.

| Code/layer | Conditions | Client action |
| --- | --- | --- |
| 401 HTTP | missing/wrong role credential, unknown node or wrong node token; Bearer challenge returned | Correct authorization; persisted node identity must not blindly re-register |
| 404 HTTP | absent task; assignment absent **or owned by another node**; absent whole-model job; inference routes when disabled | Do not assume ownership/cleanup from absence |
| 409 legacy | insufficient eligible capacity; conflicting saved result; different report/cancel transition after terminal | Reconcile returned/persisted state; do not relaunch merely to retry a report |
| 422 HTTP | extra/unknown fields, task-type/model pairing, excessive declared budget, invalid payload/result shape | Correct input; not a transient retry |
| 409 whole-model | stale/foreign lease, conflicting execution key/idempotency key, offline/incompatible worker, active legacy assignment or invalid state transition | Preserve identity/ledger and reconcile original lease |
| 422 whole-model | wrong fixed manifest, invalid budget/prompt/result, missing execution key at start, success without result | Refuse launch/report until corrected |
| transport/5xx | network, server/database or unexpected backend failure | Outcome may be unknown; preserve durable records. No blanket safe retry for registration/create |
| local `StateError`, CLI 4 | invalid private credential/journal/config or lock conflict | Stop and preserve files for recovery |
| local `CredentialRejected`, CLI 2 | saved node credential rejected | Stop; operator reconciliation |
| local executor exception | invalid stage/model/endpoint, unsupported trusted local executor or runtime failure | Agent reports failed; these are not HTTP schema error codes |

For whole-model jobs, worker report `error_code` accepts `backend_error`,
`deadline`, `worker_restart`, `lease_lost`; server expiry additionally emits
`cancel_lease_expired`. Do not confuse these stored outcomes with HTTP status.

## Optional whole-model routes and cleanup

With `CONTROLLER_REAL_JOBS_ENABLED=false` (default), inference routes and their
table initialization are absent. When enabled, all routes below live under
`/v1/inference`; JSON bodies are the schemas in `inference_jobs.py`:

| Method/suffix | Credential | Body / response |
| --- | --- | --- |
| `GET /workers` | admin | `{workers:[worker view]}` |
| `POST /workers/{node_id}/capability` | this node | `Capability` / `{node_id,capability,single_worker_only:true}` |
| `POST /workers/{node_id}/claim` | this node | no body / `{job:Job or null}`; may include `cleanup_required` |
| `POST /jobs` | admin | `JobRequest` / Job (201, including equal idempotent retry) |
| `GET /jobs/{job_id}` | admin | Job |
| `POST /jobs/{job_id}/cancel` | admin | no body / Job |
| `POST /jobs/{job_id}/renew` | this node | `LeaseRequest` / Job |
| `POST /jobs/{job_id}/report` | this node | `Report` / Job |
| `POST /jobs/{job_id}/stop-ack` | this node | `LeaseRequest` / Job |

Job view includes `id,worker_node_id,request,status,created_at,deadline,
lease_epoch,lease_expires,result,error_code,stop_acknowledged,server_time,
cancel_requested`; private request/execution digests and idempotency key are
omitted. `JobRequest` has target worker, fixed model, nonblank prompt ≤4096 UTF-8
bytes, `max_tokens` 1–64 (64), total timeout 10–90 seconds (90), native timeout
0.01–90 seconds (60), and a 16–128 ASCII alphanumeric/underscore/hyphen
idempotency key. Lease bodies identify node and epoch ≥1, with optional 64-hex
execution key; the key is mandatory and durable before `running`. Result is
text ≤65536 UTF-8 bytes, elapsed seconds 0–90, and the fixed model hash.

Full state rules are in [REAL_JOBS.md](REAL_JOBS.md): equal validated create
payload/key returns the original durable job, conflicting payload is 409;
claim is transactional; a 10-second lease is capped by total deadline.
Whole-model registration explicitly drains legacy work, then capability
registration prevents later legacy eligibility. A worker with unknown physical
cleanup remains blocked after lease expiry until authenticated stop acknowledgment.
This differs from immediate legacy reservation release. The macOS guardian and
Android screen-bound worker implement different host lifecycles; neither proves
Android foreground-service/device acceptance.

## Open acceptance and server changes

- [#40](https://github.com/7onytang/LastCore/issues/40): review this inventory;
  response typing/automated compatibility enforcement is absent, not promised.
- [#41](https://github.com/7onytang/LastCore/issues/41): freeze the policy and
  decide backward-compatible legacy model allowlisting, manifest-derived memory
  budgets and stable rejection codes. This documentation does not implement them.
- [#42](https://github.com/7onytang/LastCore/issues/42): named public hardware
  configuration and measured minimum RAM remain unknown; see model selection.
- [#43](https://github.com/7onytang/LastCore/issues/43),
  [#44](https://github.com/7onytang/LastCore/issues/44),
  [#47](https://github.com/7onytang/LastCore/issues/47): desktop registration and
  lifecycle/packaging acceptance retain their original platform requirements.
  Bounded duplicate-delivery suppression is not a cross-crash exactly-once claim.
- [#48](https://github.com/7onytang/LastCore/issues/48): pinned native model runs
  exist on the separate runtime/worker path; old `Executor` integration and its
  original acceptance are not established by those records.
- [#49](https://github.com/7onytang/LastCore/issues/49): authenticated transport,
  repeated two-device split-model run and disconnect/security acceptance remain.
- [#50](https://github.com/7onytang/LastCore/issues/50),
  [#51](https://github.com/7onytang/LastCore/issues/51),
  [#52](https://github.com/7onytang/LastCore/issues/52),
  [#53](https://github.com/7onytang/LastCore/issues/53),
  [#54](https://github.com/7onytang/LastCore/issues/54): current desktop GUI,
  signing/install/upgrade/uninstall, Android device matrix/foreground service,
  and full release acceptance retain their original requirements.
  New client/device and real-model tasks are paused; Android physical-device
  execution is explicitly deferred. Signing preparation is not signed delivery.

Existing tests are evidence of their own scope: Node Agent
`tests/test_agent.py` and `tests/test_controller_client.py` use FakeController /
HTTPX MockTransport for protocol success/failure/duplicate/cancel/deadline/outage.
They do not execute a pretrained model or validate either desktop GUI/device.

Documentation verification on this baseline: those two files passed **28 tests**.
Offline OpenAPI inspection with temporary databases matched 9 paths / 10
operations when inference is disabled, and 18 paths / 19 operations when enabled;
request fields/bounds, HTTP Bearer, success statuses and forbidden extra fields
were checked against the tables and implementation. Local document links resolve.
These checks started no listener, executor or model and contacted no live node.
