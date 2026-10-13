# Integration source provenance

Recovered from LastCore commit `d14c107f9b05389c974a4917db68121330c5209c` (PR #7), using Git archive. No external project code was copied.

Original files and SHA-256:

- `FAILURE_MATRIX.md`: `0ea27264009bd61ff50efbbb4a7a487921ccdfe63fa8c1fab78a700c14326512`
- `pipeline_client.py`: `ce6d9f5bb0aaada5789e7f2ef75c3cc9f58d8f5fac4874ed30d0a393d97e2537`
- `run_two_node_local.sh`: `c69a9ef2271aed31283381b025a58645673a532ca6c66cb1d9372df217c2eb7a`

`pipeline_client.py` and `FAILURE_MATRIX.md` retain their original content. The failure matrix describes its historical baseline; it does not certify this candidate. The shell runner now applies Beijing time/private output permissions, uses an absolute client path, waits/reaps its own child processes and RSS sampler, and fails cleanup verification if any tracked process or port survives. Its EXIT/INT/TERM traps preserve failure status.

Reuse references: CPython subprocess cwd and process APIs (https://github.com/python/cpython/blob/3.12/Lib/subprocess.py), existing LastCore StageServer/Controller.
