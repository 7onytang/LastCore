# First enrollment: unknown outcome recovery

The legacy `/v1/nodes/register` endpoint creates a new identity for every accepted
POST and has no idempotency-key contract. A lost response does not prove that the
server failed to create a node. The Agent therefore does not automatically retry
an enrollment whose result is unknown and does not claim server exactly-once
registration.

Before the first POST the Agent writes the private, secret-free
`registration.pending` JSON intent (`version: 1`, `phase: "pending"`) in its node
state directory. It reuses the credential journal's fsync/atomic writer, then
publishes that intent exclusively with a hard link; an existing marker is never
overwritten. POSIX directory metadata is fsynced before the POST. A filesystem
that cannot safely reserve the intent is rejected before network registration.

The response must validate as NodeCredentials and be saved/read back successfully
before the marker is removed. Transport failures, unusable success responses,
credential-save failures, or HTTP error responses retain the intent and enter
`recovery-required`. Although the current Controller's auth/validation rejection
occurs before its creation transaction, a client cannot prove that an observed
HTTP status came from that path rather than an intermediary, so it does not erase
the barrier based on 401/403/422 alone. Exception/response text and credentials are
not included in the enrollment recovery message or observer error state.

On restart, a pending intent without valid saved credentials blocks registration.
Corrupt intents or identities are preserved and rejected; a known intent/identity
disappearing during the same Agent lifetime is also rejected. A valid saved
identity plus a valid leftover marker is reused: the Agent only finishes intent
cleanup, never registers another node. If cleanup still fails, it preserves both
files and reports the cleanup failure instead of proceeding with an uncertain
startup. An ordinary existing identity without a marker continues to heartbeat;
temporary connectivity failures retain the existing connection-retry behavior.

When recovery is required:

1. Stop repeated launches and preserve the original private state directory,
   including credentials, marker, pending reports, and settled journal. Do not
   delete/rename a marker or identity merely to get past the error.
2. Have the Controller administrator inspect enrollment/audit records to establish
   whether the specific attempt created a node. A status code or absent local
   response is insufficient evidence by itself.
3. If the original validated identity can be recovered through an authorized
   private mechanism, restore that same identity to the original private state
   directory. The next launch validates it and completes marker cleanup. Never
   paste node tokens or registration credentials into chat or ordinary logs.
4. If the original identity cannot be recovered, the administrator must explicitly
   reconcile any orphan identity and authorize the next enrollment. This change
   provides no automatic marker reset, identity deletion, credential recovery, or
   destructive administrative operation. Ask for a reviewed recovery procedure
   based on the administrator's evidence before changing these files.

If all durable evidence is externally deleted, the client cannot infer a previous
attempt after restart. This barrier covers preserved local state/process recovery;
it is not a server idempotency guarantee or a guarantee under arbitrary storage
loss. It adds no HTTP fields, model/device execution, or new recovery service.

Pure Fake/temp-state validation from `node-agent/` after installing its declared
`requirements-dev.txt`:

```sh
python -m pytest -q tests/test_credential_recovery.py tests/test_credentials.py tests/test_settled_recovery.py
```

These tests exercise lost responses after simulated creation, HTTP errors, save
and cleanup failures, restart, malformed or disappearing state, exclusive intent
reservation, successful identity reuse, and offline heartbeat retry. They do not
register a real node or execute a task. Design references: the project's
`real-model-runtime/worker.py` enrollment barrier and [AWS's discussion of unknown
outcomes and idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).
