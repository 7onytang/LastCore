# Real local model runtime

This Python 3.12+ adapter runs an actual open-weight 135M parameter LLM on a local CPU through llama.cpp `llama-completion`. The local adapter proves prompt-to-generated-text inference. The optional whole-model worker described below connects it to a separate Controller jobs API; neither path distributes pretrained layers. The small model is an integration baseline, not a quality benchmark or production assistant.

## Reused upstream implementation

- [llama.cpp](https://github.com/ggml-org/llama.cpp), MIT, commit `8345f333951c661d166b00e6f9362e553768f292` (official v0.6.0 release). Source archive SHA-256 `84050e783dbd0c4a4678f90f253b0881df04e90a98c20f73ae3709751c4a4850`.
- [SmolLM2-135M-Instruct GGUF](https://huggingface.co/lmstudio-community/SmolLM2-135M-Instruct-GGUF/tree/04f7f8a05a8e4bc208e88d11e8c9122be36c2f9b), Apache-2.0; revision `04f7f8a05a8e4bc208e88d11e8c9122be36c2f9b`, Q4_K_M file, 105,454,144 bytes, SHA-256 `bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169`.
- [Original model](https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct) and [upstream security guidance](https://github.com/ggml-org/llama.cpp/blob/8345f333951c661d166b00e6f9362e553768f292/SECURITY.md).

llama.cpp provides maintained native CPU support with low integration cost. The release's new `llama-cli` links server code; `llama-completion` retains a direct local completion path. No HTTP/RPC server target is built or launched. MLC would require a broader platform packaging integration, so it is deferred.

## Install and run (macOS arm64 verified)

Requires Python 3.12+, Apple Command Line Tools, internet for setup, and roughly 1 GB free disk. All dependencies and artifacts stay in this directory. Build parallelism is limited to four jobs. CPU-only build avoids Metal compiler requirements.

```sh
python3 -m venv real-model-runtime/.venv
real-model-runtime/.venv/bin/python -m pip install cmake==4.1.0
real-model-runtime/.venv/bin/python real-model-runtime/bootstrap.py
python3 real-model-runtime/runtime.py --prompt 'What is the capital of France?' --tokens 32 --timeout 60
python3 -m unittest discover -s real-model-runtime/tests -v
python3 real-model-runtime/verify_real.py
```

Setup verifies archive/model hashes, binds every backend source file to the verified archive, and records the built executable hash in `.cache/installed.json`. Both initialization and every generation verify executable/model hashes before launch, so a reused instance rejects replaced artifacts. The installation must remain trusted and unchanged during generation; this is not protection against concurrent filesystem tampering. Cached native libraries remain a trusted local installation; this is not a signed distribution or a sandbox for untrusted models. Setup uses no remote Python model code. Preserve or move aside a rejected backend tree before explicitly creating a fresh one; setup never adopts or overwrites an unbound, partial or changed tree.


### Cached backend source binding

The existing SHA-256 mechanism is reused for the pinned source archive and every cached source file. Setup hashes and parses the same open archive, accepts only bounded ordinary file/directory members under the expected `llama.cpp-<commit>` root, and rejects unsafe paths, duplicate/conflicting members, links and special files before extraction. First extraction exclusively creates `.cache/backend`; `.lastcore-source-binding.json` is written only after the complete extracted source matches. A failed extraction leaves its partial tree unbound and preserved.

On every reuse, setup requires that exact completion binding **and** compares source paths, types, sizes and file hashes against the verified archive. A matching marker alone is never sufficient. Missing, changed, extra or linked source entries fail before model download or CMake, preserving the old tree and any `installed.json`. Setup repeats the source check after CMake and before writing a new installed receipt. A known unbound/incorrect marker is rejected before any download.

The only source-inventory exception is a real, non-symlink top-level `backend/build/`, matching the existing `cmake -B` output directory. Its generated files, CMake cache and native dependencies remain **trusted local build state**; source binding is not a reproducible-build, signed-binary or complete build-output integrity guarantee. A linked build root is rejected. Source/build/cache files must remain trusted and unchanged during setup; this does not defend against concurrent filesystem tampering.

For an older or interrupted cache without this binding, preserve it by moving `.cache/backend` to an owner-selected backup outside the active backend path, then explicitly rerun setup for a fresh tree. Keep the hash-verified `backend.tar.gz` and `model.gguf` caches to avoid unnecessary downloads. Do not copy a completion marker into an old tree as a migration shortcut. Setup never automatically deletes or repairs the rejected tree.

Offline binding regression only:

```sh
python3 -m unittest discover -s real-model-runtime/tests -p test_bootstrap_source.py -v
```

These tests use synthetic tar/temp fixtures with download/subprocess mocked, including normal first install/reuse, partial and forged bindings, source changes, path/link/size rejection, generated-build compatibility and preservation of installed receipts. They do not download weights, run CMake or execute a real model. This local guard does not implement the separate historical model-manager module in PR #6/#10.

The CLI returns JSON with generated text, timing, scope, and backend diagnostics. `--tokens` limits generated tokens to 1–256; prompt input is limited to 4096 UTF-8 bytes. Context is 2048 tokens. Ctrl-C requests cancellation. Backend execution deadline, cancellation, backend error, output overflow, or caller exception terminates the dedicated process group and reaps its child. Each stdout/stderr stream is limited to 1 MiB; crossing that limit raises an explicit failure and cleans up the process group, including after a fast backend exit. Successful output is never silently truncated. Integrity hashing precedes the backend deadline. Unit lifecycle tests use synthetic subprocesses and are separate from the real model smoke evidence.

Model weights, downloaded source, compiler packages, and build products are ignored by Git. Remove `.cache` and `.venv` to uninstall locally. Future Android packaging and Controller integration require their own device/end-to-end validation.

## Verified evidence

On macOS 15.8.1 arm64, the real backend generated `The capital of France is Paris.` from the smoke prompt. `evidence/smoke.json` preserves backend diagnostics with prompt/token evaluation; `evidence/verification.json` records Beijing time, hashes, platform, and successful real-backend timeout/cancellation cleanup. All eleven synthetic lifecycle/integrity tests passed separately in `evidence/unit-tests.txt`. Re-run `verify_real.py` after setup to refresh this local evidence. CPU token timings are single-run diagnostics, not a throughput claim.


## License materials

`licenses/SmolLM2-APACHE-2.0.txt` contains the full Apache-2.0 terms from Apache's official source. Both the fixed GGUF revision and original model revision `12fd25f77366fa6b3b4b768ec3050bf629380bac` declare Apache-2.0 in their model cards; neither repository has a standalone LICENSE file. `licenses/llama.cpp-LICENSE.txt` preserves the full MIT copyright and terms copied verbatim from the hash-verified source archive at the fixed backend commit. `licenses/manifest.json` records these exact text hashes, sources, and revisions. Keep these materials with any later model/backend distribution; this local adapter is not a complete signed native redistribution package.

The output limits and lifecycle guard reuse CPython's standard `TemporaryFile`, `fstat` and subprocess process-group APIs. No additional server/RPC target, dynamic model code, or runtime dependency is introduced.

CI runs the eleven original synthetic lifecycle tests plus four guardian/URL tests and verifies the full license text hashes and saved source-bound evidence metadata. The optional `workflow_dispatch` real-model job builds the pinned backend, downloads the hash-verified GGUF, executes `verify_real.py` and the real local Controller/worker jobs loop on macOS. A passing synthetic job does not mean that the optional real-model job has run.

## Minimal whole-model macOS worker

`worker.py` actively polls validated HTTPS, executes one complete fixed GGUF per job and returns bounded generated text. It opens no network listener and does not use the tiny layer-assignment executor. The Controller feature defaults to disabled; enable `CONTROLLER_REAL_JOBS_ENABLED=true` only for a reviewed compatible release. Protocol and limits are in [REAL_JOBS.md](../controller-server/REAL_JOBS.md).

Install the already used HTTP client in an isolated environment with `pip install -r node-agent/requirements.txt`. After the model/backend setup above, save a private mode-0600 JSON configuration (replace placeholders; never commit it):

```json
{
  "controller_url": "https://8.134.161.123",
  "registration_token": "REPLACE_WITH_SERVER_REGISTRATION_TOKEN",
  "state_dir": "~/Library/Application Support/LastCore/real-worker",
  "runtime_root": "/absolute/path/to/real-model-runtime",
  "name": "macos-real-model-worker"
}
```

Run `python real-model-runtime/worker.py --config /private/path/worker.json`. Credentials and the current execution ledger use private files; an exclusive state-directory lock prevents two copies. Registration with an unknown network outcome leaves a marker and refuses blind re-registration. The worker advertises the fixed model/source hashes and is ineligible for legacy layer tasks. Public plaintext HTTP, URL credentials/query/fragment and redirects are rejected. The optional loopback-HTTP switch is restricted to local acceptance.

Each claimed job is persisted before launch. An independent child guardian stops and reaps the native process group on cancellation, local lease deadline or parent pipe EOF, and writes a private positive cleanup record before reporting a result. Restart never regenerates an already claimed job; without positive cleanup evidence it refuses a new launch. A stopped outcome can be retried idempotently. The Controller holds a worker's reservation after lease loss until an authenticated stop acknowledgment; it never equates lease expiry with physical process termination. Untrusted/malicious devices are not attested or forcibly controlled by the server. This is a CLI worker, with no Developer ID signing/notarization claim.

Bounded actual model acceptance (requires separate Controller and worker environments):

```sh
python integration/verify_real_jobs_local.py --runtime-root /absolute/path/to/real-model-runtime \
  --controller-python /path/to/controller/bin/python --worker-python /path/to/worker/bin/python
```

This starts a temporary real HTTP Controller, runs actual model generation and backend timeout, verifies cancellation acknowledgment and idempotency, then checks process/token cleanup. It is local evidence; a public-cloud job-to-worker loop requires separate deployment verification.

## Worker credential and recovery corrections

New `node-credentials.json` includes `controller_origin`, the canonical scheme/ASCII host/port identity of the issuing Controller. Controller URLs must be origins (empty path or `/`); default ports and host case are normalized, non-default ports remain distinct. Existing credentials are checked before constructing the authenticated client. A different configured origin is refused without sending the old token or re-registering.

Legacy credential files without this field are deliberately refused. Do not delete the state directory or supply a new registration token to bypass the old identity/lease. An operator must first verify the **original issuing origin independently** from the prior private registration/configuration records or the deployment operator, retain a private backup, then explicitly migrate offline:

```sh
real-model-runtime/.venv/bin/python real-model-runtime/worker.py \
  --config /absolute/private/worker.json \
  --bind-legacy-origin https://8.134.161.123
```

Use that example only if it is the verified original origin. The explicit argument and configured canonical origin must match. Migration holds the same process lock, adds only the origin to the existing mode-0600 credential file, leaves tokens and execution/cleanup files untouched, sends no HTTP request and never registers. It will not overwrite an already bound different origin. If the original origin cannot be verified, keep the files and ask the operator to reconcile the identity; do not blindly bind the current URL. Restart normally after migration; recovered stopped outcomes are reconciled before model verification.

A confirmed registration rejection with HTTP401/403/422 clears `registration.pending`, permitting a corrected credential to retry. Missing, empty or invalid local registration input is rejected before writing a marker. Transport failure, HTTP5xx or unusable success response retains the unknown-outcome marker; no blind retry occurs. Provider response bodies, URLs with credentials and token values are absent from error output.

Under the state lock and validated credential origin, startup first delivers a durable stopped outcome or positive guardian cleanup proof (or reports a never-launched claimed phase as `worker_restart`). Missing/damaged model cache therefore cannot prevent prior physical-stop reconciliation. Without positive native cleanup, startup remains blocked. Current model/backend provenance and integrity are still required before capability advertisement, readiness or new claims; the native guardian continues to verify artifacts before each actual generation. This is not protection against concurrent tampering by a trusted local installation owner.

A transport/auth/server failure while reporting a stopped outcome keeps its exact ledger for an idempotent retry; only a confirmed409 may fall back to stop acknowledgment after positive stop. Constructor failures deterministically close the HTTP client and release the process lock. The correction uses the existing [HTTPX error/status separation](https://www.python-httpx.org/exceptions/), [RFC6454 origin identity](https://www.rfc-editor.org/rfc/rfc6454.html), atomic private journal and guardian cleanup protocol. No broker, new dependency, Controller/Android change or model algorithm change is introduced.

`tests/test_worker_recovery.py` adds HTTPX MockTransport/private-state regressions for these enrollment, origin, migration and stopped-outcome boundaries. They are protocol fixtures, not real-model evidence. The separate local actual-Controller recovery check uses synthetic stopped state and no native/model execution; normal real-GGUF execution remains a separate acceptance scope.

## CI runner maintenance

The optional `real-model-smoke` job uses GitHub's hosted `macos-15` runner, following the [macOS 14 retirement announcement](https://github.blog/changelog/2026-10-01-github-actions-macos-14-runner-image-retirement/) (retirement: 2026-11-02; earlier brownouts). This reuses the existing supported runner image; it adds no action, dependency or service. Ordinary pushes and PRs execute synthetic lifecycle/license checks on Linux. Real model execution still requires `workflow_dispatch` with `real_model_smoke: true`; its default remains false.

Validate workflow syntax with `actionlint .github/workflows/real-model-runtime.yml` from the repository root. Run synthetic regressions with the node HTTPX dependency installed: `python -m unittest discover -s real-model-runtime/tests -v`. This runner migration has not executed the optional macOS model job and does not establish fresh GGUF, device or production acceptance.
