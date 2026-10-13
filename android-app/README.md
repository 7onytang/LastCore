# LastCore Android native preview

Native Java application with a pinned official llama.cpp JNI backend for **local whole-model CPU inference**. Distributed node admission stays disabled: Controller registration, transport and pretrained multi-device layer execution are not integrated. The existing Python android-node and Controller contracts are untouched.

## Build

Pinned AGP8.13.0 / Gradle8.13 / JDK17 / compile+target API35 / min API29 / arm64-v8a. NDK28.2.13676358 and CMake3.22.1 compile the native library; r28 supports 16 KiB ELF alignment. `SDK-APPROVAL.json` records the user's explicit approval of Android SDK terms. Official repository metadata confirms NDK and CMake use the same `android-sdk-license`; unrelated licenses are not accepted. Project-local tools/caches are ignored, no global installation or emulator is required. NDK + CMake downloads total 990,718,426 bytes; local NDK installation uses about 2.8 GiB.

Set JAVA_HOME to an isolated JDK17, ANDROID_HOME to an SDK with platforms;android-35, build-tools;35.0.0, platform-tools, ndk;28.2.13676358 and cmake;3.22.1. Put Gradle8.13 on PATH (the local `.toolchain/gradle-8.13` is automatically used). Commit source changes, then run:

```sh
./build-apk.sh
```

`bootstrap-native.py` verifies and extracts official llama.cpp source commit `8345f333951c661d166b00e6f9362e553768f292`, archive SHA-256 `84050e783dbd0c4a4678f90f253b0881df04e90a98c20f73ae3709751c4a4850`. It downloads only sources, with a 50 MiB bound. Each build re-extracts the verified archive so cached edits cannot silently compile. The build produces debug APK, unsigned release APK and compiled instrumentation APK. `artifact-binding.py` requires clean committed Android sources, embeds exact source SHA + tracked-source hashes and checks compiled manifest/native ABI/licenses/artifact hashes. `verification/artifact-manifest.json` identifies the exact artifacts and separates build evidence from device execution. No publishing, real release signing or store submission occurs.

## Local model flow

Obtain the pinned `SmolLM2-135M-Instruct-Q4_K_M.gguf` from [the fixed Hugging Face revision](https://huggingface.co/lmstudio-community/SmolLM2-135M-Instruct-GGUF/tree/04f7f8a05a8e4bc208e88d11e8c9122be36c2f9b). The app does not download weights or open a server. Choose **Import model** using Android's document picker. It copies at most 105,454,144 bytes into app-private no-backup storage, checks exact size and SHA-256 `bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169`, and atomically commits only the valid model. Invalid/cancelled import preserves any existing model. Verify integrity again before each inference. **Delete imported model** removes this private copy while idle.

Enter a short prompt and choose **Generate locally**. A single worker calls JNI and official llama.cpp C APIs; ChatML formatting follows the pinned SmolLM2 model. CPU only, 2 threads, 256-token context, prompt at most192 tokens, output at most64 tokens /16 KiB, cooperative90-second deadline. Java/native boundaries use UTF-8 byte arrays. Shared native-session ownership prevents concurrent close/cancel from freeing active state; every run frees model/context/sampler on completion or failure. Sessions are single use and limited to four. Cancel, leaving the screen and Activity destruction request abort. No foreground service, boot receiver or background scheduling is added. Actual cancellation latency, native peak memory and Android inference quality still require the selected physical device.

Local generation checks ActivityManager memory/low-memory, PowerManager thermal and BatteryManager battery state. It rejects less than512 MiB available memory, severe/unavailable thermal metadata and low/non-measured battery. These are conservative gates, not measured model memory proof. Native logging is suppressed; the UI does not display provider exception paths or credentials. It never registers a node or accepts assignments. The optional **Check Controller connection** action validates an HTTPS origin, uses default platform certificate and hostname checks, refuses redirects, and reads at most4 KiB of `/healthz` with5-second connect/read timeouts. It sends no credentials or model prompts. A successful `status=ok` persists only the public origin; it proves health/TLS reachability, not cloud job execution. Leaving the screen suppresses its result; a socket read may continue until its bounded timeout. The disabled distributed-node button makes this boundary explicit.

## Security and licenses

Manifest requests INTERNET only, disables backups and HTTP cleartext; launcher Activity is the only exported component. CredentialVault is an unused integration component: AndroidKeyStore AES-GCM, app-private no-backup AtomicFile, non-exportable key, no plaintext credential journal; no account enrollment is included. Device execution must verify Keystore tamper/clear, process death/sleep, lifecycle abort, listener absence and memory limits before integrating registration.

`app/src/main/assets/licenses/` ships complete llama.cpp MIT, model Apache-2.0 and LastCore MIT texts, and the official NDK LLVM/third-party NOTICE for statically linked C++/compiler runtime, plus exact upstream license provenance. Model cards of the pinned GGUF and original model declare Apache-2.0. This preview does not bundle or redistribute weights. llama.cpp C++/GGML code implements the transformer; the project only adds a bounded JNI/lifecycle adapter. Optional KleidiAI/OpenMP/RPC/OpenSSL/dynamic backends are disabled; no extra acceleration dependencies are downloaded.

## Verification and CI

`android-native.yml` builds APKs, verifies debug signature, checks seven pure Java distributed-admission denials and stores source/artifact evidence. Instrumentation compilation is not execution. Physical-device tests compile under `CredentialSecurityTest` and `NativeRuntimeTest`; run `gradle connectedDebugAndroidTest` only after root selects and authorizes the device. Tests cover Keystore ciphertext/tamper/clear/non-exportability, manifest policy, closed/cancelled native handles, missing model refusal and invalid imports. They do not establish real-model inference automatically; select/import the pinned model and exercise actual UI generation/cancel/background/restart on the device.

Prior bootstrap fixed-source independent rebuild passed62 tasks and seven Java checks; compiled DEX and manifest matched original artifact bytes. See BUILD-VERIFICATION.md for that checkpoint. Current native build/device results are recorded separately and must not be inferred from the earlier bootstrap.

The separate macOS workflow builds a real bundle, checks full manifest files, frozen project code objects and own/third-party license texts, and executes real local tiny CPU cancel/restart. Neither CI nor local whole-GGUF inference proves pretrained multi-device inference or product launch.

Sources: [official Android build](https://developer.android.com/build/releases/agp-8-13-0-release-notes), [SDK terms](https://developer.android.com/studio/terms), [Keystore](https://developer.android.com/privacy-and-security/keystore), [llama.cpp Android](https://github.com/ggml-org/llama.cpp/blob/8345f333951c661d166b00e6f9362e553768f292/docs/android.md), [16 KiB pages](https://developer.android.com/guide/practices/page-sizes).

Default production origin is `https://8.134.161.123`. Actual served chain is YE2 → Root YE (cross-signed by X2) → X2 (cross-signed by X1). AOSP Android10 includes ISRG Root X1 certificate `6187b673.0`, SHA256 `96BCEC06264976F37460779ACF28C5A7CFE8A3C0AAE11A8FFCEE05C0BDDF08C6`; the CA [documents the default cross-chain](https://letsencrypt.org/certificates/) and [Android7.1.1+ X1 compatibility](https://letsencrypt.org/docs/certificate-compatibility/). This is source/trust-anchor compatibility evidence, not a selected Android device handshake. No custom TrustManager, root injection or hostname-verifier bypass is included.

The user explicitly deferred physical Android verification. Keep device inference/Keystore/lifecycle/TLS as unverified until resumed; do not ask for a device during this phase. Sideload/self-host previews retain target35. GooglePlay submission has a separate target36/signing/store boundary and is outside current scope.
