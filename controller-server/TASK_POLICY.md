# Versioned task admission policy v1

This is the normative, reviewable policy for new Controller requests. The
implementation is [task_policy.py](app/task_policy.py), the checked-in manifest
is [task-policy-v1.json](app/task-policy-v1.json), and the unchanged wire schemas
remain [models.py](app/models.py) and [inference_jobs.py](app/inference_jobs.py).
It addresses SPEC-02 #41 without claiming hardware qualification or that every
original acceptance item is complete. No model or executable is downloaded by
admission; no new dependency or remote manifest loader is added.

## Profiles and actual constraints

| Execution family / version | Strict admission | Input, execution and output bounds |
| --- | --- | --- |
| `lastcore/linear-demo-v1`, algorithm v1 | Enabled, bundled bounded arithmetic; no GGUF artifact required | Existing targeted-node/one-node rule; 1–4096 rows; 1–256 finite input values within ±1,000,000; seed 0–2,147,483,647; one finite [-1,1] output per row |
| `lastcore/pi-monte-carlo-v1`, algorithm v1 | Enabled only with `workload=pi` and explicit node capability | 1,000–100,000,000 samples; required seed; partitions ≤ samples and ≤100,000; exact assigned `inside,samples` counts; executor checks deadline/cancel periodically |
| `lastcore/log-statistics-v1`, algorithm v1 | Enabled only with `workload=log_stats` and explicit node capability | 1–10,000 lines including blank lines; ≤4096 Unicode code points per line, ≤1,000,000 total; row count equals partition count; bounded ERROR/WARN/INFO/OTHER counts |
| `SmolLM2-135M-Instruct-Q4_K_M`, revision `04f7f8a05a8e4bc208e88d11e8c9122be36c2f9b` | Enabled only on the separate optional whole-model API, fixed SHA256 `bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169`, backend source commit `8345f333951c661d166b00e6f9362e553768f292` | Prompt ≤4096 UTF-8 bytes; 1–64 new tokens; total timeout 10–90 seconds; native timeout 0.01–90 seconds additionally capped by overall/lease deadline; result ≤65536 UTF-8 bytes. Native context differs: Mac CPU adapter2048, Android JNI256; native tokenization/context failures remain worker rejections, not a universal prompt-fit guarantee |
| `lastcore/tiny-cpu` / operator-selected local config/NPZ | **Unavailable in strict admission** | Controller cannot bind the operator-selected local weight/config identity and resource profile. Do not fabricate a version/hash or infer compatibility from its name |
| Other legacy model identifiers | **Rejected in strict admission** | They do not select another implementation, remote source or shell command |

Common legacy-schema bounds remain: positive layer-memory declaration ≤10,000,000
MB; ≤128 nodes; total timeout10–86,400 seconds. The manifest repeats the relevant
per-profile caps and admission enforces them in addition to existing schemas.
For v1 the whole-model `output_bytes` must equal the actual fixed65536-byte
`Result` limit; any different manifest value rejects startup rather than pretending
to configure report validation. Existing result/report/ACK behavior is unchanged.
Schema coercion behavior is unchanged; π/log retain their stricter integer rules.
All models retain `extra=forbid`, including compatibility mode. Model/workload
mismatch is rejected; no global GGUF allowlist is imposed on arithmetic.

There are three different resource statements:

- **Configured admission:** legacy scheduling uses declared MB and current
  node capacity. Whole-model workers self-report availability. These inputs are
  not OS attestation, measured RSS or hardware minimums.
- **Implemented constraints:** existing input/shape/token/output/deadline bounds
  remain enforced by the schema, executor and report validator. Whole-model lease
  loss/cancel still requires local cleanup acknowledgement before worker reuse.
  A legacy cancel releases its server reservation but does not prove physical stop.
- **Unmeasured / unsupported:** `measured_peak_memory_mb` and
  `native_hard_memory_limit_mb` are explicitly null. No hard native RSS cap is
  added. Android's 512MiB availability gate is a policy gate, not measured model
  demand. Missing required identity/budget fields in an executable profile reject
  startup; a deliberately unavailable profile rejects new work.

Android's native screen-bound worker supports the pinned whole model only,
not pretrained layer splitting, arbitrary sources or π/log. Its separate Python
agent is not evidence of native-app support. Platform strings never grant a
capability. Physical Android, hard RAM/peak budgets, native-context-aware placement
and transport/device qualification remain outside this offline change (#42/#48/#49/#52/#53).

## Data, deadlines, cancellation and idempotency

Inputs are bounded inline vector/log/prompt data; outputs are the existing bound
result fields identified by task/assignment/node or job/lease identities. Remote
input/output references are unsupported, not fetched. Task bodies cannot select
`command`, `shell`, `code`, `script`, `executor` or `executable`; other unknown
fields are also rejected. Text *containing* shell-like words remains inert data.
Only trusted local operator configuration may install/select executor code.
Pinned sources in bootstrap/install metadata are not task-provided URLs.

The server derives task deadlines from its clock and the bounded timeout; a
caller cannot override an absolute deadline with an extra field. Existing expiry
transactions and executor checks reject/skip expired assignments. Late changed
reports are409; an identical saved terminal ACK remains accepted. Existing
whole-model cancellation/lease fencing/stop-ack semantics are unchanged.

Legacy task creation and node registration remain non-idempotent: reconcile an
unknown POST before retrying. Equal terminal result reports and repeat cancel
retain their existing rules. Whole-model equal create key/payload returns the
original job **before** checking new-admission policy; conflicting payload/key
remains409. Policy does not rewrite saved requests, add default wire fields,
change result types, alter request/cache digests or re-admit historical reads.

## Stable rejection codes

HTTP status and the existing `detail` string/list are retained. New task/job
admission and whole-model capability schema errors additionally have a top-level
`error_code`. This HTTP envelope is separate from existing stored worker outcome
codes (`backend_error`, `deadline`, `worker_restart`, `lease_lost`, etc.).

| `error_code` | HTTP | Rule |
| --- | --- | --- |
| `TASK_TYPE_UNSUPPORTED` |422| Unknown task workload |
| `MODEL_NOT_ALLOWED` |422| Unknown model or execution-family/version mismatch |
| `MODEL_METADATA_UNAVAILABLE` |422| Known local profile lacks executable metadata |
| `MODEL_SOURCE_UNTRUSTED` |422| Task-supplied model/backend source/path or invalid fixed capability pins |
| `EXECUTABLE_FIELD_FORBIDDEN` |422| Task body attempts to select shell/code/executor |
| `REFERENCE_UNSUPPORTED` |422| Task-provided input/output reference |
| `BUDGET_REJECTED` |422| Invalid/excessive schema or profile budget |
| `REQUEST_INVALID` |422| Other malformed/unknown-field/cross-field request |
| `ASSIGNMENT_EXPIRED` |409| Changed legacy report after deadline, or renewal/report of expired whole-model lease/job |

Multiple-invalid requests have deterministic classification priority: executable,
source, references, other unknown fields, workload/model, budget, then generic
validation. Ordinary validation details remain identical; non-finite invalid
inputs are stringified in error details so rejecting them does not itself cause
an invalid-JSON500. Authentication/ownership401/404, ordinary409 conflicts and
route-missing fallback keep their existing response shapes and semantics. No
policy error acknowledges physical cleanup or releases a live local worker.

## Compatibility and rollout boundary

New applications default to strict admission. Only the trusted server setting
`CONTROLLER_LEGACY_TASK_MODEL_IDS=true` (also accepts1; false/0 otherwise) enables
arbitrary **legacy linear** IDs/local tiny fixtures (including an opaque GGUF-like
ID used only as an old simulation label, not a whole-model execution request); invalid setting values fail
startup. It does not weaken extra-field rejection or π/log/whole-model checks.
A warning explicitly states that this mode does **not** meet strict #41 acceptance.
It is not a request field. Historical task reads/reports/cancel and saved whole
jobs remain usable even when their model is unavailable for new strict work.

This is a default change for future code rollout, not a production configuration
change. Review existing producers before deployment; controlled local tiny
integration explicitly opts into compatibility and is not strict-policy evidence.
Both modes use the same existing database layout; code rollback uses the same
compatible live volume, not a snapshot over later writes. Current fixed a924
production deployment is unchanged by this source-only PR.

Offline verification: `python -m pytest -q tests/test_task_policy.py` from
`controller-server/`, plus that module's existing suite and the contract suite
in `tools/contracts`. These use synthetic SQLite/ASGI/Fake payloads, not model,
network, hardware, memory measurements or device acceptance. Issue #41 should not
be closed solely on these tests: the contract still requires non-author review,
and this policy does not supply missing hard native-memory/peak qualification.
