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

Setup verifies archive/model hashes and records the built executable hash in `.cache/installed.json`. Both initialization and every generation verify executable/model hashes before launch, so a reused instance rejects replaced artifacts. The installation must remain trusted and unchanged during generation; this is not protection against concurrent filesystem tampering. Cached native libraries remain a trusted local installation; this is not a signed distribution or a sandbox for untrusted models. Setup uses no remote Python model code. Delete `.cache` and rebuild to replace damaged source/build artifacts.

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
