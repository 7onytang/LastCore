# Bounded real whole-model jobs

This optional API orchestrates one explicitly selected worker running the entire fixed SmolLM2 GGUF. It is separate from `/v1/tasks` and the tiny layer protocol. `CONTROLLER_REAL_JOBS_ENABLED` defaults to `false`; while disabled no inference routes or inference tables are initialized. Existing tables/data are retained. There is no automatic inference retry or multi-device pretrained weight distribution.

Model ID: `SmolLM2-135M-Instruct-Q4_K_M`, model SHA256 `bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169`, backend source commit `8345f333951c661d166b00e6f9362e553768f292`. Worker capability records its native executable hash; local integrity checks enforce its installation before each execution. This is cooperative device verification, not hardware attestation.

| Method/path under `/v1/inference` | Bearer credential | Input/result |
| --- | --- | --- |
| `GET /workers` | Admin | Compatible node IDs, capability, online/ready state |
| `POST /workers/{node_id}/capability` | This node | `model_id`, fixed `model_sha256`, fixed `backend_commit`, 64-hex `backend_sha256` |
| `POST /workers/{node_id}/claim` | This node | `{job: object or null}`; after unknown cleanup also `cleanup_required` |
| `POST /jobs` | Admin | `worker_node_id`, optional fixed `model_id`, `prompt`, `idempotency_key`, optional budgets |
| `GET /jobs/{id}` | Admin | Request, state, lease epoch/expiry, server time, result, stop acknowledgment |
| `POST /jobs/{id}/cancel` | Admin | Cancel before claim, or transition active execution to `cancelling` |
| `POST /jobs/{id}/renew` | This node | `node_id`, `lease_epoch`; result includes cancel flag and server time |
| `POST /jobs/{id}/report` | This node | `node_id`, `lease_epoch`, `status` (`running`, `succeeded`, `failed`, `cancelled`) |
| `POST /jobs/{id}/stop-ack` | This node | `node_id`, original `lease_epoch`; acknowledges physical cleanup after cancellation/expired lease |

Whole-model workers register through `/v1/nodes/register` using the registration Bearer credential and explicitly send `accepting_tasks:false`; the new optional field defaults to `true` for existing clients. This makes the node ineligible for legacy assignments atomically at registration. Its mandatory `endpoint` may be an explicitly inert non-listening loopback placeholder because whole-model workers open no RPC listener. Available-memory figures are conservative self-reported budgets, not automatic hardware measurements.

Before the first `running` report, generate a 64-hex random `execution_key` and persist it with the claimed job/epoch in private storage. Include it in every subsequent renew/report/stop-ack body. The server stores only its digest, never returns it, and rejects a second execution owner even with the same node credential. Persisted retries reuse the same key. Before running, a lease-only renewal/stop acknowledgment may omit the key because no backend was started. A worker without its prior execution ledger must not execute a running job or acknowledge unknown native cleanup.

Prompts must contain text and fit 4096 UTF-8 bytes. `max_tokens` defaults to 64 and is limited to 1–64. The total job `timeout_seconds` defaults to 90 and is limited to 10–90; `execution_timeout_seconds` defaults to 60 and is limited to 0.01–90, allowing a separately bounded native backend deadline. The worker clamps execution to the remaining total budget. Integrity hashing precedes the native timeout and remains under the independent lease guard. Result shape is `{text, elapsed_seconds, model_sha256}`; text fits 65536 UTF-8 bytes. Raw backend diagnostics/stderr are never accepted as results. Failure `error_code` is one of `backend_error`, `deadline`, `worker_restart`, `lease_lost`.

Idempotency keys are 16–128 ASCII letters/digits/underscore/hyphen. Equal validated payload and key return the same durable job; a different payload with the same key returns 409. Claim and reservation occur under the same SQLite write transaction. Repeated claim returns the same job and epoch; epochs increase monotonically per worker. Node identity, job ID and epoch must all match on renew/report/ack. Stale/foreign leases cannot replace results.

Lease length is 10 seconds, capped by total deadline. Workers renew while executing, using returned server-time deltas conservatively on a local monotonic clock. Cancelling jobs receive no lease extension. A queued cancellation never launches. Active cancellation keeps the worker reserved until explicit cleanup acknowledgment or lease expiry; after expiry the job is terminal but the worker remains blocked until a stop acknowledgment. `stop_acknowledged=false` explicitly represents an unknown stop outcome. Terminal results do not automatically retry. A late result is rejected; an old acknowledgment retry cannot free a newer worker reservation.

Registering a whole-model capability rejects an active legacy assignment and makes that node ineligible for tiny/layer tasks, including later heartbeats that request `accepting_tasks=true`. Controller node heartbeats and job leases are separate. An application restart preserves jobs, results, idempotency keys and cleanup barriers. Online SQLite backup includes both new tables and foreign keys; recovery tests verify original node credentials and the same completed result/idempotency behavior from an independent snapshot.

Use trusted HTTPS for remote access. The macOS worker configuration, node credential and execution ledger files are private; generated tokens/prompt/native stderr are absent from captured logs. Store protocol timestamps as UTC epoch seconds; business display/verification use `Asia/Shanghai`. Roll back by stopping the dedicated worker, disabling the optional API and retaining all database files/volumes. Do not downgrade a running worker's protocol/schema or overwrite prior data without a reviewed backup/recovery path.

Implementation reuses SQLite transactions, Python process groups/pipe EOF and the fixed [llama.cpp](https://github.com/ggml-org/llama.cpp) local completion adapter. [Celery](https://github.com/celery/celery) was reviewed; its broker-backed task processing would add a separate infrastructure dependency unnecessary for this explicitly bounded single-worker path.
