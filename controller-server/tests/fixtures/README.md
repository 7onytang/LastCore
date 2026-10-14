# Frozen compatibility fixtures

`store-60c58831.py.txt` is the exact Controller Store source from public commit
`60c58831edaa4de06fd144b4e79f5df216900911`, path
`controller-server/app/store.py`, SHA256 `143a4af1fd9a42dfe33f61522b96c33815c753cd9e7d705e12e5756514f75f71`.
It retains the repository MIT license and original authorship in that commit.
The test executes only its reader against synthetic SQLite metadata to verify
new typed storage cannot alter the old linear `result` field. It never starts
a Controller, computes a workload or depends on Git history availability.

The shared synthetic old-format linear request lives in
`edge-client/tests/fixtures/linear-request-63d409a8.json`. The Controller suite
checks exact serialization against it, while the isolated node suite verifies
the published 63d409a8 edge implementation reuses a frozen cache without calling
the arithmetic function. This avoids importing Controller/Pydantic into the
node workflow or changing shallow checkout requirements.
