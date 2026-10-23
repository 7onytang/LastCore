# Initial model/runtime choice and evidence limits

Source inventory: `2f66bcbffc30eb80821652fd5c5799df5262cd50`.
This records the reproducible choice requested by
[SPEC-03 #42](https://github.com/7onytang/LastCore/issues/42). Unknown hardware
and memory limits remain unknown; this is not a completed device qualification
or a change to that issue's acceptance criteria.

## Chosen whole-model integration baseline

| Item | Fixed choice / source |
| --- | --- |
| Runtime | Official [llama.cpp](https://github.com/ggml-org/llama.cpp/tree/8345f333951c661d166b00e6f9362e553768f292), source commit `8345f333951c661d166b00e6f9362e553768f292`, MIT |
| Backend archive SHA256 | `84050e783dbd0c4a4678f90f253b0881df04e90a98c20f73ae3709751c4a4850` |
| Native entry point | `llama-completion`, local CPU; no server/RPC listener; macOS setup disables Metal |
| Model | `SmolLM2-135M-Instruct-Q4_K_M.gguf`, 135M-parameter instruction model, Q4_K_M only |
| Download source | [lmstudio-community fixed revision](https://huggingface.co/lmstudio-community/SmolLM2-135M-Instruct-GGUF/tree/04f7f8a05a8e4bc208e88d11e8c9122be36c2f9b), `04f7f8a05a8e4bc208e88d11e8c9122be36c2f9b` |
| Model bytes / SHA256 | 105,454,144 / `bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169` |
| Original model | [HuggingFaceTB fixed revision](https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct/tree/12fd25f77366fa6b3b4b768ec3050bf629380bac), `12fd25f77366fa6b3b4b768ec3050bf629380bac` |
| Model license | Both pinned model cards declare Apache-2.0. Complete terms, declarations and text hashes are preserved in [licenses/manifest.json](../real-model-runtime/licenses/manifest.json) |
| Local install identity | `bootstrap.py` records the compiled executable hash in private `.cache/installed.json`; model and executable hashes are checked before execution |

Choice rationale from [the runtime implementation record](../real-model-runtime/README.md):
llama.cpp offers a maintained native CPU implementation and a small local
completion entry point with low integration cost. MLC was considered but needs
broader platform packaging; it remains deferred, not measured inferior.
The existing NumPy tiny engine is a separate deterministic layer-splitting
correctness prototype; its generated weights are not a pretrained text model.
No throughput/quality comparison or Windows/Android qualification is implied by
this selection. This document adds no upstream code or dependency.

## Reproduction and license handling

Use [real-model-runtime/README.md](../real-model-runtime/README.md) for the exact
Python 3.12+, Apple command-line-tools, pinned CMake, bootstrap and verification
commands. Bootstrap downloads the pinned archive/model and verifies hashes;
the native build is limited to four build jobs. Installation guidance estimates
roughly 1 GB free disk for that local runtime path. That is a planning estimate,
not a measured universal disk minimum, and excludes an existing Python/compiler
installation. Android SDK/NDK builds have their own larger toolchain footprint.

The MIT backend copyright/permission text and Apache-2.0 model terms must accompany
later redistribution as applicable; keep upstream attribution/declarations and
record any modifications. The project MIT license does not replace upstream
terms. Current public delivery supplies fixed download metadata and license
materials; it does not upload the model cache or claim a signed native model
redistribution package. These recorded licenses permit redistribution subject
to their terms; any additional distribution/store requirements must be checked
for the actual package. See [the full license manifest](../real-model-runtime/licenses/manifest.json).

## Hardware and observed compatibility

| Execution path / evidence | Confirmed observation | Unknown or excluded |
| --- | --- | --- |
| [Local runtime verification](../real-model-runtime/evidence/verification.json), [actual output/diagnostics](../real-model-runtime/evidence/smoke.json) | 2026-10-13 Beijing time; macOS 15.8.1 arm64; CPU generation returned Paris; real backend timeout/cancellation reaped child; model/backend/source hashes recorded | Public records do not name hardware model, CPU marketing name or installed RAM. Minimum RAM and peak native memory are unmeasured |
| [Repaired worker local record](../submission/evidence/mac-worker-30b9efc-local.json), [independent review](../submission/evidence/mac-worker-30b9efc-review.md) | Fixed worker `30b9efc` + local HTTP Controller `85ddad9`; one actual GGUF job, Paris output, native/guardian reaped, lock reacquired; 2026-10-13 Beijing time | Local test, not additional target-device/GUI acceptance or a performance benchmark |
| [Public Controller → Mac mini record](../submission/evidence/public-gguf-f913-generation-summary.json) | Fixed Controller `f9139762` → worker `30b9efc`; one actual GGUF job, 0.253-second single observation, stop acknowledged and native/guardian reaped | Summary identifies Mac mini but publishes no model identifier/CPU/RAM. It is not repeated-run, cross-device split-model, Android or MacBook Pro GUI evidence |
| [Historical MacBook Pro GUI record](../submission/evidence/mac-client-0.1.1-linear-acceptance.md) | `f9139762` native client displayed one targeted linear arithmetic result, confirmed by user | Not SmolLM2 execution; cannot qualify whole-model hardware or a newer preview |
| [Android native preview](../android-app/README.md) | Build target API29+, arm64-v8a; pinned JNI runtime and host-side verification; development APK builds | No named physical Android device tested; real install/inference, Keystore/lifecycle/TLS, thermal/battery impact and minimum RAM remain unverified |
| [Windows/Linux preview](../edge-client/README.md) | Fixed-source native compilation/static artifacts exist for bounded linear arithmetic | No pretrained model runtime support or GUI/network/device execution qualification |

No public artifact containing a Mac model identifier, M-series chip name and
installed RAM was found in the checked-in evidence inventory. Those values are
therefore not inferred from a hostname, developer machine or private recollection.
The reproducible model choice does not establish a hardware minimum.

The Android source admission gate (512 MiB **available** memory, conservative
thermal/battery checks) is a policy gate, not a measured model requirement.
Likewise Controller `available_memory_mb` is self-reported scheduling input;
it does not measure peak RSS or qualify device suitability.

## Remaining acceptance

[SPEC-03 #42](https://github.com/7onytang/LastCore/issues/42) retains the missing
named-hardware configuration/minimum-memory evidence. Model-manifest-driven
resource budgets and legacy model policy remain
[#41](https://github.com/7onytang/LastCore/issues/41).
The real model uses a separate runtime/whole-model worker, rather than the old
Node Agent `StageExecutor`; original Executor integration/repeated output and
cleanup acceptance remain [#48](https://github.com/7onytang/LastCore/issues/48).
Repeated split-model runs on two physical devices remain
[#49](https://github.com/7onytang/LastCore/issues/49).
Android device matrix/observations and foreground-service support remain
[#52](https://github.com/7onytang/LastCore/issues/52) and
[#53](https://github.com/7onytang/LastCore/issues/53).

New real-model/device tasks are paused, and Android physical-device validation
is explicitly deferred. Reading and organizing existing receipts does not
create fresh execution acceptance or remove those requirements.
