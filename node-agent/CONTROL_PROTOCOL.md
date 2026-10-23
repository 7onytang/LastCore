# Local admission control and recovery

`NodeAgent.set_accepting_tasks(value: bool, request_id: str | None = None)` changes
the desired admission state. Pausing gates new local worker starts immediately;
the active worker, cancellation reconciliation, heartbeat, and durable terminal
report replay continue. Resuming requires an actual heartbeat response with the
matching boolean from the current request generation. An old in-flight ACK cannot
confirm a newer pause/resume request, even when its boolean happens to match.
Request IDs are unique per agent lifetime: the same ID/value is idempotent; reuse
with a different value is rejected. This does not add heartbeat wire fields.

Every optional observer update carries an `admission` snapshot:

```json
{"desired":true,"confirmed":false,"request_id":"resume-2","confirmed_request_id":"pause-1","status":"pending"}
```

`confirmed` is the last value confirmed for a matching generation, or null before
the first confirmation. `status: "confirmed"` refers to the current request, with
`confirmed_request_id == request_id`. Consumers must use this status/ID pairing,
not just a historical boolean. Invalid control files emit only the safe enum
`control_error: "invalid-command"`; successful new commands emit null. Existing
connection, poll, and task observation enums remain unchanged.

The optional CLI `--ui-control-file PATH` reads one local command from a private
temporary directory. The application never opens a control socket or executes
command text. A desktop wrapper can call:

```python
from lastcore_node_agent.local_control import write_control_command
write_control_command(control_path, request_id="pause-1", accepting_tasks=False)
```

The strict UTF-8 JSON schema is exactly
`{"version":1,"request_id":"pause-1","accepting_tasks":false}`. Version is an
integer, acceptance is a boolean (integers are rejected), IDs contain 1–64 ASCII
letters/digits/underscore/hyphen, and the file is at most 1024 bytes. Duplicate or
extra keys, symlinks/reparse points, hardlinks, nonregular files, insecure parent
directories, and nonprivate files are rejected without applying a command.
The writer reuses the existing credential journal's atomic fsync/replace helper;
it never reads credentials. On POSIX both file and parent must belong to the
current user and have no group/other access. Windows checks native owner/DACL:
allow ACEs must target the current user, Administrators, SYSTEM, or Owner Rights
after verifying that the actual owner is one of those principals. A private
`TemporaryDirectory` on Python 3.12.4+ establishes the required Windows directory
ACL; chmod mode bits alone do not establish Windows privacy.

Commands are consumed before polls and heartbeats and by the independent watchdog
at most `min(watchdog_interval, poll_interval, 1 second)` apart, apart from normal
scheduler/I/O delay. A 30-second poll-error backoff does not postpone file control.
The command file is local intent, not a durable recovery journal or authenticated
Controller decision. Session wrappers should generate fresh request IDs.

Poll transport/Controller failures use capped exponential waits from twice the
configured poll interval up to `max(poll_interval, 30 seconds)`. A successful poll
resets the interval. Event-based waits remain interruptible; heartbeat and local
deadline/watchdog checks run independently. Shutdown grace expiry retains the live
worker reference and marks cleanup unconfirmed until the thread exits; this does
not forcibly terminate arbitrary Python plugins or promise exactly-once execution
across crashes. No normal-path resource overlap was demonstrated in the old code:
the lost reference occurred while exiting the polling loop.

Validation from repository root (isolated environment with the two declared test
dependency files installed):

```sh
python -m pytest -q -c tools/contracts/pytest.ini tools/contracts
python -m pytest -q -c node-agent/pytest.ini node-agent/tests/test_admission_control.py
```

The dedicated Controller Node contract workflow runs both on Ubuntu, Windows, and
macOS 15. They use actual ASGI/SQLite routes with Fake executors, plus pure Fake
time/failure tests. There are no model/device/production-task checks. Windows
asyncio's internal startup socketpair is still the documented framework boundary;
application requests use in-process transport and do not bind sockets.

References: [Python JSON duplicate-key hooks](https://docs.python.org/3/library/json.html#repeated-names-within-an-object),
[Python Windows private temporary directories](https://docs.python.org/3.12/whatsnew/3.12.html#tempfile),
[Win32 security descriptors](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getnamedsecurityinfow),
and [bounded retry guidance](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_mitigate_interaction_failure_limit_retries.html).
