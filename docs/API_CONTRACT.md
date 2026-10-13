# LastCore Controller API Contract

## Status and scope

This document records the legacy layer-task and node-lifecycle routes under `/v1`, including the deterministic `lastcore/linear-demo-v1`, `lastcore/pi-monte-carlo-v1`, and `lastcore/log-statistics-v1` demonstration workloads. None is a pretrained LLM. Current optional whole-model job behavior is documented separately in [`controller-server/REAL_JOBS.md`](../controller-server/REAL_JOBS.md); use [`node-agent/PROTOCOL.md`](../node-agent/PROTOCOL.md) for the packaged Agent protocol. Fields or behavior not described as implemented below must not be assumed by a client.

The API is exposed at the Controller base URL, for example `https://controller.example.com`. The service does not enforce TLS itself. Deployments that carry credentials or task data must provide TLS or a trusted encrypted tunnel in front of it.

Interactive OpenAPI documentation is available at `/docs` when enabled by the FastAPI application. `GET /healthz` is unauthenticated and returns `{"status":"ok"}`.

## Authentication

Protected routes use `Authorization: Bearer <token>`:

| Credential | Allowed operations |
| --- | --- |
| Registration token | Register a node |
| Node token | Heartbeat, poll that node's assignments, report that node's assignments |
| Admin token | List nodes, create/list/read/cancel tasks |

Registration returns a new `node_id` and `node_token`. The token is returned only at registration time; the server stores a SHA-256 digest. An Agent must persist both values securely and reuse them after restart. Registration has no idempotency key, so retrying a registration request after an ambiguous network failure can create another node.

Missing or invalid credentials return HTTP `401`. A node credential cannot operate on another node's resources.

## Endpoints

### Register a node

`POST /v1/nodes/register` (registration token, success `201`)

```json
{
  "name": "Mac Mini A",
  "platform": "macOS-arm64",
  "endpoint": "http://10.0.0.11:9000",
  "available_memory_mb": 8192,
  "compute_weight": 3.0
}
```

`endpoint` must be a valid URL. The Controller stores it for the assignment plan but does not connect to or verify it. Successful response:

```json
{
  "node_id": "<uuid>",
  "node_token": "<secret returned once>",
  "heartbeat_ttl_seconds": 30
}
```

`name` is 1-128 characters, `platform` is 1-64 characters, memory is an integer from 1 to 10,000,000 MB, and compute weight is greater than 0 and at most 1,000,000. Unknown JSON fields are rejected.

### Heartbeat

`POST /v1/nodes/{node_id}/heartbeat` (that node's token, success `200`)

```json
{"available_memory_mb": 8192, "compute_weight": 12.5, "accepting_tasks": true}
```

Memory may be 0-10,000,000 MB. `compute_weight` is optional, finite, greater than 0, and at most 1,000,000; when supplied it replaces the node's previous weight. `accepting_tasks` defaults to `true`. Setting it to `false` prevents new scheduling but does not stop an active assignment. Successful response is the node record, with `id`, `name`, `platform`, `endpoint`, `available_memory_mb`, `compute_weight`, `accepting_tasks`, `last_seen` (Unix timestamp in seconds), and computed `status` (`available`, `draining`, `busy`, or `offline`).

The bundled Agent samples system available memory every five seconds. At startup it runs a short deterministic CPU workload and reports its throughput as `compute_weight`; this is a relative Python CPU score, not a hardware-independent benchmark. Set `--memory-mb` or `--compute-weight` to override these values. No GPU, network throughput, or live CPU load measurement is implemented yet.

The heartbeat TTL defaults to 30 seconds and is configurable from 5 to 3,600 seconds. Requests and a background sweep trigger expiry processing; a task is failed if its deadline has passed or any assigned node has missed its heartbeat TTL. The background sweep runs at most every half TTL (capped at 30 seconds), so stale reservations are released even when the Controller receives no requests. A node reconnecting later does not restore that task.

### Poll assignments

`GET /v1/nodes/{node_id}/assignments` (that node's token, success `200`)

Response shape:

```json
{
  "assignments": [
    {
      "id": "<assignment-uuid>",
      "task_id": "<task-uuid>",
      "node_id": "<node-uuid>",
      "position": 0,
      "layer_start": 0,
      "layer_end": 16,
      "status": "pending",
      "error": null,
      "previous_node": null,
      "next_node": {"node_id": "<node-uuid>", "endpoint": "http://10.0.0.12:9000", "id": "<assignment-uuid>"},
      "task_status": "queued",
      "request": {"model_id": "Qwen/example", "num_layers": 32, "layer_memory_mb": 512, "max_nodes": 8, "timeout_seconds": 600},
      "deadline": 1791879000.0
    }
  ]
}
```

Layer intervals are zero-based and half-open: `[layer_start, layer_end)`. `previous_node` and `next_node` are `null` at pipeline boundaries; otherwise each contains the peer node ID, endpoint, and assignment ID (`id`). `deadline` is a Unix timestamp in seconds. The `request` echoes the task request; `model_id` is an identifier only and does not imply that the Controller or Agent can download that model.

Polling has no cursor, acknowledgement, or long-poll parameter. The response repeats assignments on every request and includes the node's assignments from its 100 most recently created tasks, including terminal task states. Agents must deduplicate by assignment ID and inspect both `task_status` and `deadline` before starting work. This is a prototype polling API, not a durable command queue; older history can fall out of the 100-task window.

Assignment statuses are `pending`, `running`, `completed`, `failed`, and `cancelled`. Task statuses are `queued`, `running`, `completed`, `failed`, and `cancelled`.

### Report assignment status

`POST /v1/nodes/{node_id}/assignments/{assignment_id}/report` (that node's token, success `200`)

```json
{"status":"running"}
```

Allowed report statuses are `running`, `completed`, and `failed`. `error` is optional and limited to 2,048 characters; for `failed`, an omitted or null error becomes `node reported failure`. `result` is optional and accepted only with `completed`. Linear inference reports a finite-number array whose length equals the assigned range. The π workload reports `{ "inside": integer, "samples": integer }` for its deterministic sample interval. The log workload reports level counts such as `{ "ERROR": 2, "INFO": 4 }`; counts must sum to the assigned line range. Error text is cleared for `running` and `completed` reports. The response is the full task representation, including its request, assignment partial results, and (when complete) aggregated result.

An assignment may be reported `completed` directly from `pending`; the API does not require a prior `running` report. A repeated report with the same assignment status is idempotent. A different report after the assignment or task is terminal returns `409`. Any assignment failure fails the whole task and marks other pending/running assignments failed. A task becomes completed only after all its assignments are completed. Completion reports confirm node-reported work; for the built-in linear demo, partial model output is stored and returned when all assignments complete.

### List nodes

`GET /v1/nodes` (admin token, success `200`)

Returns `{"nodes":[...]}` with the node record fields described under Heartbeat. Node tokens and token digests are not included.

### Create a task

`POST /v1/tasks` (admin token, success `201`)

```json
{
  "model_id": "lastcore/linear-demo-v1",
  "num_layers": 32,
  "layer_memory_mb": 512,
  "max_nodes": 8,
  "timeout_seconds": 600,
  "input_vector": [0.25, -0.5, 1.0],
  "weight_seed": 42
}
```

`max_nodes` defaults to 8 (range 1-128); `timeout_seconds` defaults to 600 (range 10-86,400). `model_id` is 1-256 characters. `num_layers` and `layer_memory_mb` must be positive integers, with maxima 100,000 and 10,000,000 respectively. `input_vector` is optional, contains 1-256 finite numbers, and when present limits `num_layers` to 4,096. `weight_seed` defaults to 42 and must be a non-negative 32-bit integer. Unknown fields are rejected.

`workload` defaults to `linear`. For `pi`, set `model_id` to `lastcore/pi-monte-carlo-v1`, set `workload` to `pi`, and provide `total_samples` (1,000-100,000,000); `num_layers` is the number of deterministic scheduling chunks. For `log_stats`, set `model_id` to `lastcore/log-statistics-v1`, set `workload` to `log_stats`, and provide 1-10,000 `log_lines`; `num_layers` must equal the number of lines. Each line is limited to 4,096 characters and the whole log payload to 1,000,000 characters. Non-linear workloads cannot include `input_vector`.

On success the response is a task object containing `id`, `request`, `status`, `created_at`, `deadline`, `error`, `assignments`, and `result`. Assignment ranges form a contiguous, non-overlapping partition of `num_layers`; for `linear-demo-v1`, these are output unit indices. Each assignment includes its node, range, status, optional partial `result`, and adjacent peer details as described above. When a task is completed and every assignment supplied a result, linear task results are concatenated in range order, π task counts are summed and returned with `pi_estimate`, and log-level counts are summed and returned with `total_lines`. Otherwise task `result` is `null`. The scheduler considers only online, accepting, unreserved nodes. If the requested layers cannot fit within `max_nodes`, the request returns `409` and creates no task.

The bundled Agent executes the workload selected by `workload` and matching `model_id`. The linear demo computes a deterministic `tanh(Wx+b)` output slice. The π demo estimates π with a deterministic integer hash-based Monte Carlo sample stream and returns per-assignment inside/sample counts; the Controller calculates `4 * total_inside / total_samples`. The log demo counts `ERROR`, `WARN`, `INFO`, and other lines in each assigned range; the Controller adds the per-node counts. These are demonstration workloads and are not intended for sensitive logs or production analytics. Unsupported model IDs fail explicitly.

The controller reserves each selected node for the entire task, even after that node reports its own segment complete. It does not validate endpoint reachability or transfer model data/tensors.

### Read and cancel tasks

- `GET /v1/tasks` (admin token) returns `{"tasks":[...]}` for up to the 100 most recently created tasks.
- `GET /v1/tasks/{task_id}` (admin token) returns one task object; unknown IDs return `404`.
- `POST /v1/tasks/{task_id}/cancel` (admin token) changes an active task to `cancelled` and marks pending/running assignments cancelled. Repeating cancellation of an already-cancelled task is idempotent. Cancelling a completed or failed task returns `409`.

Cancellation is recorded in Controller state. The Controller cannot forcibly stop a remote process; an Agent must poll and stop its work when it observes a terminal task status.

## Errors and validation

| HTTP status | Meaning in the current API |
| --- | --- |
| `401` | Missing or invalid credential |
| `404` | Unknown task or assignment, or assignment does not belong to the authenticated node |
| `409` | Insufficient schedulable capacity, assignment/task is terminal, or task cannot be cancelled in its current state |
| `422` | Request body or path validation failed |

FastAPI error bodies use its standard `{"detail": ...}` format; validation errors use a list of error objects in `detail`. Clients should not rely on exact human-readable error strings as stable machine codes. No rate-limit response, retry-after header, API negotiation, or per-node assignment acknowledgement is currently implemented.

## Client behavior required by this contract

- Keep node credentials private; never include tokens in logs or support output.
- Persist `node_id` and `node_token` after successful registration and reconnect with them instead of registering again.
- Send heartbeats within the configured TTL and report status changes; use client-side network timeouts and bounded retry/backoff.
- The bundled Python Agent retries connection and response errors with exponential delays, capped at 30 seconds, and resets the delay after a successful poll.
- Deduplicate repeated polls by assignment ID. Before starting, require an active task status and an unexpired deadline.
- Stop work after observing `cancelled`, `failed`, or an expired deadline. The server cannot enforce remote process termination.
- Treat `model_id` and ranges as scheduling metadata except for the three documented deterministic demo workloads. Linear tasks send their input vector through the Controller API and store partial outputs in SQLite; log-statistics tasks send their input lines through the Controller and store partial counts. Do not use these demo payloads for sensitive or large inputs. General model transfer, tensor transport, model versions/hashes, resource reservations beyond layer memory, and arbitrary output schemas are not defined by this API.

## Versioning and open decisions

The current FastAPI application reports version `0.2.0`, and routes are prefixed `/v1`. No explicit schema negotiation or compatibility header exists. Additive fields are currently rejected in request bodies because request models forbid extras; clients should send only documented fields. Any incompatible API change requires a coordinated controller and Agent release (and should use a new API path version).

Before implementing real inference, separately decide and document task type, model version and source, input/output references, resource budget semantics, cancellation acknowledgement, reliable command delivery, and node-to-node transport/security. These are not part of the implemented `0.2.0` contract.
