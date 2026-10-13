# Independent Mac Worker recovery fix review

Fixed worker commit:30b9efc22c5db9f351d304604e65743ca02ccda3, clean sole-author worktree.
Worker SHA256:3278bcaad5a4d0e3e7882dea76a974de73b63fce4c24aac8dcf49bbebabbcf82.
Local Controller baseline:2e8c76b88fdff8f265dadafa1a0e0e6597133bed (runtime equivalent to deployed85dd).

Verdict: APPROVE; original three core findings resolved. No additional actionable blocker in bounded delta.

Source review:
- Confirmed401/403/422 registration rejection clears pending; missing/empty/header-invalid input fails before marker/client; transport/server/invalid201 retains unknown marker, refusing blind repeat registration.
- Canonical origin uses scheme, normalized ASCII/IDNA host, IPv6 authority and port; rejects paths, URL credentials/query/fragment. Existing mismatched/originless credentials fail before authenticated client construction. Explicit offline legacy binding requires independently verified original origin to match config; locks state, preserves token/ledger, makes no HTTP/registration call, cannot overwrite already bound different origin.
- Under lock and credential origin validation, recovery precedes installation checks/capability. Durable stopped outcome or positive guardian cleanup is reconciled; never-launched claimed state fails safely. Launched state without positive physical-stop evidence still blocks. Artifact/provenance checks remain before readiness/claim/execution.
- Unknown/auth/server stopped-report failures retain exact ledger; only confirmed409 triggers cleanup acknowledgement after known physical stop. Constructor failure closes HTTP client and lock.
- Native guardian/generation launch/lease mechanics unchanged. README distinguishes migration/operator verification and fixture/model evidence.

Independent executed tests:
1. Full relevant stdlib unittest set23 tests passed in1.979s. Includes15 pre-existing runtime/guardian checks and8 added HTTPX MockTransport/private-state regression tests. These unit/protocol tests are not new real model proof.
2. One actual isolated loopback HTTP Controller acceptance execution, fresh SQLite/test credentials, three synthetic protocol fixtures. missing-model-stopped and bad-model-stopped: old job becomes failed/worker_restart with stop_acknowledged=true, ledger removed and worker ready/current_job=None before installation failure. unknown-native-stop: old job remains running/unacknowledged and ledger remains until fixture cleanup. Every scenario leaves queued next job lease_epoch0; constructor HTTP clients closed and state lock reacquirable. All fixture jobs cleaned to terminal/stop acknowledged; temporary DB/credentials removed; Controller thread stopped. No model/native execution, public-cloud call or private production DB access. Initial sandbox port binding failed before service/job creation, then approved loopback execution completed once.

Actual local fixture source: /private/tmp/lastcore-independent-medium-review/core-gap/actual_local_recovery.py
Actual local fixture evidence: /private/tmp/lastcore-independent-medium-review/core-gap/actual-local-recovery-evidence.json
Normal real-GGUF positive-path validation is owned separately by integration; this review does not claim new model execution or final deployment.

Normal actual model path evidence addendum: APPROVE
Independently read /private/tmp/lastcore-mac-30b9efc-real-normal.py and its real-normal-evidence.json without rerunning. Harness pins clean worker30b9efc/local Controller85, verifies reused model/backend/source hashes, enrolls a fresh private fixture over loopback, creates exactly one bounded16-token10s job and observes actual guardian/native descendants. Jobd1784ec4-fb57-4bf0-8012-122b842f6e16 succeeded with Paris, elapsed0.257s, pinned model SHA and server stop_acknowledged=true. Saved credential origin matches local origin; worker exits0, ledger/completion files clear, worker ready/current_job=None and lock reacquirable. Observed native/guardian gone, Controller exits and port rebindable; logs checked for credential absence. Independently recalculated all three worker/runtime/guardian source hashes against fixed30b9efc Git objects: match. This is one real local model acceptance produced by integrator, separately from this reviewer's actual local protocol fixtures. No new public-cloud/Android-device/model rerun by reviewer. Original three fixes and normal model execution scope are now supported by complementary evidence.
