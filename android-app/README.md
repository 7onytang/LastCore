# LastCore Android native preview

Native Java application with a pinned official llama.cpp JNI backend for **local whole-model CPU inference**. The optional user-started foreground Service registers for explicitly assigned whole-model jobs; its notification provides Stop while the Activity observes through a same-process LocalBinder. Pretrained multi-device layer execution is not integrated. The existing Python android-node and Controller contracts are untouched.

## Build

Pinned AGP8.13.0 / Gradle8.13 / JDK17 / compile+target API35 / min API29 / arm64-v8a. NDK28.2.13676358 and CMake3.22.1 compile the native library; r28 supports 16 KiB ELF alignment. `SDK-APPROVAL.json` records the user's explicit approval of Android SDK terms. Official repository metadata confirms NDK and CMake use the same `android-sdk-license`; unrelated licenses are not accepted. Project-local tools/caches are ignored, no global installation or emulator is required. NDK + CMake downloads total 990,718,426 bytes; local NDK installation uses about 2.8 GiB.

Set JAVA_HOME to an isolated JDK17, ANDROID_HOME to an SDK with platforms;android-35, build-tools;35.0.0, platform-tools, ndk;28.2.13676358 and cmake;3.22.1. Put Gradle8.13 on PATH (the local `.toolchain/gradle-8.13` is automatically used). Commit source changes, then run:

```sh
./build-apk.sh
```

`bootstrap-native.py` verifies and extracts official llama.cpp source commit `8345f333951c661d166b00e6f9362e553768f292`, archive SHA-256 `84050e783dbd0c4a4678f90f253b0881df04e90a98c20f73ae3709751c4a4850`. It downloads only sources, with a 50 MiB bound. Each build re-extracts the verified archive so cached edits cannot silently compile. The build produces debug APK, unsigned release APK and compiled instrumentation APK. `artifact-binding.py` requires clean committed Android sources, embeds exact source SHA + tracked-source hashes and checks compiled manifest/native ABI/licenses/artifact hashes. `verification/artifact-manifest.json` identifies the exact artifacts and separates build evidence from device execution. No publishing, real release signing or store submission occurs.

## Local model flow

Obtain the pinned `SmolLM2-135M-Instruct-Q4_K_M.gguf` from [the fixed Hugging Face revision](https://huggingface.co/lmstudio-community/SmolLM2-135M-Instruct-GGUF/tree/04f7f8a05a8e4bc208e88d11e8c9122be36c2f9b). The app does not download weights or open a server. Choose **Import model** using Android's document picker. It copies at most 105,454,144 bytes into app-private no-backup storage, checks exact size and SHA-256 `bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169`, and atomically commits only the valid model. Invalid/cancelled import preserves any existing model. Verify integrity again before each inference. **Delete imported model** removes this private copy while idle.

Enter a short prompt and choose **Generate locally**. A single worker calls JNI and official llama.cpp C APIs; ChatML formatting follows the pinned SmolLM2 model. CPU only, 2 threads, 256-token context, prompt at most192 tokens, output at most64 tokens /16 KiB, cooperative90-second deadline. Java/native boundaries use UTF-8 byte arrays. Shared native-session ownership prevents concurrent close/cancel from freeing active state; every run frees model/context/sampler on completion or failure. Sessions are single use and limited to four. Cancel, leaving the screen and Activity destruction request abort. Local generation remains Activity-bound. The separate cloud worker Service is described below; no boot receiver or automatic scheduling is added. Actual cancellation latency, native peak memory and Android inference quality still require the selected physical device.

Local generation checks ActivityManager memory/low-memory, PowerManager thermal and BatteryManager battery state. It rejects less than512 MiB available memory, severe/unavailable thermal metadata and low/non-measured battery. These are conservative gates, not measured model memory proof. Native logging is suppressed; the UI does not display provider exception paths or credentials. Local generation/model mutations and cloud work share a process ownership gate across Activity recreation; the unchanged JNI backend also rejects overlapping native execution. A stopping or checkpoint-failed cloud session retains that gate. The optional **Check Controller connection** action validates an HTTPS origin, uses default platform certificate and hostname checks, refuses redirects, and reads at most4 KiB of `/healthz` with5-second connect/read timeouts. It sends no credentials or model prompts. A successful `status=ok` persists only the public origin; it proves health/TLS reachability, not cloud job execution. Each read checks the operation cancellation flag and a cooperative10-second request budget. The5-second read timeout applies per read, not to the whole response. Cancellation can wait for a blocking read; DNS/TLS/provider behavior is platform-dependent and a hard overall deadline is not claimed. Leaving the screen suppresses the result. The cloud worker is started separately and explicitly.

## Security and licenses

Manifest requests exactly INTERNET, FOREGROUND_SERVICE, FOREGROUND_SERVICE_SPECIAL_USE and POST_NOTIFICATIONS. Backups and cleartext stay disabled; the launcher Activity is the only exported component. The single CloudWorkerService is exported=false, uses specialUse with an explicit subtype, and runs in the default app process. No receiver, provider, secondary process, WakeLock or battery-exemption permission is added. CredentialVault stores node credentials and the lease ledger: AndroidKeyStore AES-GCM, app-private no-backup AtomicFile, non-exportable key, no plaintext credential journal; registration credentials are entered once in a password field, excluded from saved UI state and cleared on submission. Device execution must verify Keystore tamper/clear, process death/sleep, lifecycle abort, listener absence and memory limits.

`app/src/main/assets/licenses/` ships complete llama.cpp MIT, model Apache-2.0 and LastCore MIT texts, and the official NDK LLVM/third-party NOTICE for statically linked C++/compiler runtime, plus exact upstream license provenance. Model cards of the pinned GGUF and original model declare Apache-2.0. This preview does not bundle or redistribute weights. llama.cpp C++/GGML code implements the transformer; the project only adds a bounded JNI/lifecycle adapter. Optional KleidiAI/OpenMP/RPC/OpenSSL/dynamic backends are disabled; no extra acceleration dependencies are downloaded.

## Verification and CI

`android-native.yml` builds APKs, verifies debug signature, checks seven pure Java distributed-admission denials and stores source/artifact evidence. Instrumentation compilation is not execution. Physical-device tests compile under `CredentialSecurityTest` and `NativeRuntimeTest`; run `gradle connectedDebugAndroidTest` only after root selects and authorizes the device. Tests cover Keystore ciphertext/tamper/clear/non-exportability, manifest policy, closed/cancelled native handles, missing model refusal and invalid imports. They do not establish real-model inference automatically; select/import the pinned model and exercise actual UI generation/cancel/background/restart on the device.

Prior bootstrap fixed-source independent rebuild passed62 tasks and seven Java checks; compiled DEX and manifest matched original artifact bytes. See BUILD-VERIFICATION.md for that checkpoint. Current native build/device results are recorded separately and must not be inferred from the earlier bootstrap.

The separate macOS workflow builds a real bundle, checks full manifest files, frozen project code objects and own/third-party license texts, and executes real local tiny CPU cancel/restart. Neither CI nor local whole-GGUF inference proves pretrained multi-device inference or product launch.

Sources: [official Android build](https://developer.android.com/build/releases/agp-8-13-0-release-notes), [SDK terms](https://developer.android.com/studio/terms), [Keystore](https://developer.android.com/privacy-and-security/keystore), [llama.cpp Android](https://github.com/ggml-org/llama.cpp/blob/8345f333951c661d166b00e6f9362e553768f292/docs/android.md), [16 KiB pages](https://developer.android.com/guide/practices/page-sizes).

Default production origin is `https://8.134.161.123`. Actual served chain is YE2 → Root YE (cross-signed by X2) → X2 (cross-signed by X1). AOSP Android10 includes ISRG Root X1 certificate `6187b673.0`, SHA256 `96BCEC06264976F37460779ACF28C5A7CFE8A3C0AAE11A8FFCEE05C0BDDF08C6`; the CA [documents the default cross-chain](https://letsencrypt.org/certificates/) and [Android7.1.1+ X1 compatibility](https://letsencrypt.org/docs/certificate-compatibility/). This is source/trust-anchor compatibility evidence, not a selected Android device handshake. No custom TrustManager, root injection or hostname-verifier bypass is included.

The user explicitly deferred physical Android verification. Keep device inference/Keystore/lifecycle/TLS as unverified until resumed; do not ask for a device during this phase. Sideload/self-host previews retain target35. GooglePlay submission has a separate target36/signing/store boundary and is outside current scope.

Linux CI explicitly provisions the pinned SDK command-line tools with the maintained setup-android action; it does not depend on the hosted runner PATH. Automatic agreement acceptance and default package installation are disabled; only SDK-APPROVAL.json hashes and the explicit required package list are used. Builds stream their real inner diagnostics and preserve a failure log separately. Pull-request CI checks out GitHub merge commits, so its embedded source_commit identifies that exact merge checkout rather than claiming the PR head artifact.

## Explicit foreground Controller worker

Import the fixed GGUF, enter the HTTPS origin and first-enrollment registration
credential, then explicitly choose **Register cloud worker**. **Start cloud
worker** is a separate user action. The resumed Activity binds the native
`CloudWorkerService`; only that explicit local Binder command promotes and starts
work. Registration credentials cross this same-process call temporarily; Intents
contain only a non-sensitive action/generation. They never carry node tokens,
registration credentials, execution keys, prompts or model paths. Nothing is
restored from an Intent to automatically start a worker. Existing encrypted
credential/ledger files and Keystore aliases are retained.

The Service creates a LOW-importance notification channel and promotes before
model hashing, state loading or network work. API34+ uses `specialUse` and its
specific permission/purpose property; API29–33 pass type0. Native Service,
LocalBinder, Notification and PendingIntent APIs avoid an additional runtime
framework. AndroidX's Apache-2.0
[ServiceCompat version guards](https://github.com/androidx/androidx/blob/androidx-main/core/core/src/main/java/androidx/core/app/ServiceCompat.java)
were reviewed; no AndroidX runtime or copied helper is added.
The [specialUse rules](https://developer.android.com/develop/background-work/services/fgs/service-types)
cover otherwise unmatched valid foreground work; this user-visible whole-model
CPU worker is not labeled media processing or shortService. This is sideload
source compatibility, not Play approval of the use case.

For a visible Stop affordance, this app refuses to start when notification
permission or the channel is disabled. On API33+, the user requests permission
from the operation button, then explicitly clicks again after granting it. This
is an application policy: the
[platform permits FGS launch without POST_NOTIFICATIONS](https://developer.android.com/develop/ui/views/notifications/notification-permission),
but may then omit the drawer notification. A permission prompt is not evidence
that a worker started. Background-start/security rejection never launches a
backend. The Service returns START_NOT_STICKY; there is no Boot/Alarm/WorkManager
start, re-delivery execution, or automatic restart.

Activity pause/destruction cancels only local Activity work and removes its cloud
observer. It does not stop or close the cloud Worker, JNI session or state lock.
Reopening binds only to observe an existing Service; it does not start work.
The ongoing notification and Activity Stop use one idempotent stop path. During
STOPPING, new local/cloud/model-mutation work is refused. The Service waits for
`CloudWorker.run()` to finish its real `awaitStopped()` and guard join, then
checkpoints the encrypted state and closes the state lock before releasing the
process gate or removing the notification/stopping itself. There is no timeout
that declares cleanup successful. If checkpoint/close fails, the gate and
STOPPING notification remain; **Retry stop confirmation** retries only cleanup,
never inference. Generation/revision checks and stopSelfResult prevent an older
completion from removing a newer session. Observer failure cannot cancel work.

Registration atomically sends `accepting_tasks:false` and an inert loopback
endpoint; this worker opens no listener and never accepts legacy layers.
The existing random 64-hex execution identity is durable before native launch.
Uncertain registration/claim, damaged state or missing keys do not reexecute a
lease. Recovery still precedes model verification. Do not clear app data or keys
to bypass an ownership/cleanup barrier. A transport failure can leave a stopped
outcome pending; a stopped local Service does not assert server acknowledgment.

All Java lease, overall, renewal and heartbeat scheduling uses one injected
clock. Android supplies `SystemClock.elapsedRealtimeNanos`, including deep sleep;
pure Java fixtures use a controlled supplier. Server-relative deadlines retain
the conservative margin. The
[uptime versus elapsed distinction](https://developer.android.com/reference/android/os/SystemClock)
matters after allowing an Activity to leave: sleep must not extend a lease.
This does not wake the CPU or promise screen-off execution/network access. On
resume the existing cooperative cancellation handles elapsed expiry; native
C++ algorithms and backend selection are unchanged.

App-controlled Stop retains notification/resources until cleanup. System Task
Manager Stop, force-stop or process death can remove the entire app and
notification without callbacks; they are not graceful cleanup acknowledgments.
The next user-started session follows the durable recovery path under the file
lock. No survival, uninterrupted background execution, cancellation latency,
thermal/battery or OEM lifecycle result is claimed without physical-device
verification. Android physical-device testing remains explicitly deferred, and
[issue53](https://github.com/7onytang/LastCore/issues/53) remains open.

`CloudWorkerHostCheck` exercises the actual pure Java ownership/cleanup state
machine and LeaseClock with controlled work only. It covers duplicate starts,
stop during startup, delayed real-work-return fixtures, checkpoint failure and
cleanup-only retry, stale generations/progress, observer detach/failure, local
ownership across Activity recreation, and fake elapsed-time budget jumps.
It does not execute Service/Keystore/HTTP/JNI or prove full Android lifecycle.
Run independently with JDK17:

```sh
mkdir -p verification/host-classes
javac -d verification/host-classes app/src/main/java/ai/lastcore/android/CloudWorkerHost.java app/src/main/java/ai/lastcore/android/LeaseClock.java tests/ai/lastcore/android/CloudWorkerHostCheck.java
java -cp verification/host-classes ai.lastcore.android.CloudWorkerHostCheck
python3 -m unittest discover -s tests-python -p 'test_*.py' -v
```

The existing build script repeats these host checks before native compilation.
`artifact-binding.py` validates the compiled APK's exact four permissions,
unique non-exported Service/type/subtype, launcher and no-extra-component policy;
negative fixtures exercise that actual validator. Owner-signing tests still use
Fake SDK outputs and placeholder files, not keys. APK/Lint/instrumentation
compilation and source binding are separate from device execution. Do not run
ADB, connectedAndroidTest, an emulator or actual model/service tasks during this
source-only phase.

## Unified interface

`MainActivity` uses the [shared LastCore design system](../design-system/lastcore/MASTER.md): sage primary actions on opaque ink surfaces, semantic resource colors, visible field labels, 52dp native buttons with ripple/focus/disabled states, progress and stopping feedback, selectable output, and a confirmation before deleting the imported model. The four sections are Controller connection, local inference, activity/results and foreground cloud worker. User-facing actions/feedback are Chinese; model and license identifiers remain unchanged. Existing protocol/executor and credential protection remain in place.

API29 uses system-window insets; API30+ applies system bars, cutout and IME insets explicitly for the target35 edge-to-edge policy. Layouts scroll and text uses sp to respect font scaling. Resource compilation and host Java compilation are not physical-device visual/lifecycle acceptance; the existing device deferral still applies.

The simplified workflow has a shared model preparation card, then mutually exclusive **本机推理 / 云端节点** modes. Local mode exposes one **生成** action; without an imported file it is disabled with an adjacent explanation. A model's presence does not prove integrity; existing hash verification remains mandatory. Cloud mode exposes **启动节点** and an expandable first-enrollment form. Start/recovery is deliberately still available if the model is missing, so saved lease outcomes are not blocked by a UI prerequisite. Fields have stable IDs and inline validation; enrollment credentials remain excluded from saved state/autofill. Mode switching and duplicate actions are disabled during work. **停止** appears only while busy, then reads **正在停止…** until the existing executor completes. Starting work dismisses the keyboard and scrolls to the result/stop controls. Only the selected mode is restored, not operational state.

## Explicit owner-signing preparation (initial sideload)

The default no-argument build remains Debug + unsigned release + compiled
instrumentation, with its existing version and checks. The opt-in path uses the
same build/toolchain in an isolated detached fixed-HEAD Git checkout; Gradle,
source binding, licenses and source hashes all use that snapshot, so live edits
cannot change compiled bytes under an old provenance label. It then aligns a
separate candidate before official
apksigner signing and certificate verification:

```sh
./build-apk.sh --production --signing-config /absolute/private/owner-signing.properties
```

Copy owner-signing.properties.example outside every Git checkout, give the
private configuration and owner-provided keystore owner-only permissions
(0400/0600), and fill all seven fields: storeFile, keyAlias, storePassword,
keyPassword, expectedCertificateSha256, versionCode and versionName. Values
are literal UTF-8 single lines, including spaces and punctuation; do not source
this file or apply shell expansion. Duplicate/unknown/missing fields, unsafe
permissions or repository paths fail before any build or signing subprocess.
The helper only stats the keystore during configuration checks; it never
generates a key or selects an owner identity. The certificate fingerprint is
the expected public SHA256 of the actual app signing certificate, not a
Google Play upload identity. The owner chooses the version (positive signed
32-bit versionCode and bounded versionName); the helper assigns no new release.

Only public version properties reach Gradle. Passwords reach apksigner through
named child environment variables, never command-line arguments, build logs,
Git, APK assets or provenance. Sign/verify/tool errors are captured and reported
with a fixed safe summary. Existing live Debug and unsigned files are not changed:
production never substitutes Debug when release/signing is missing. The SDK
zipalign step precedes signing; the aligned/signed candidate is temporary.

The signed APK must have one signer, verified v2 signature, exact expected
certificate SHA256, no Android Debug identity, and a non-debuggable release
manifest with the configured app version. The shared source/license/native/
permission/API policy checks still apply. Only after all actual checks pass
are the signed APK, JSON and SHA256 sidecar exclusively published under
verification/production (or an explicit --output-dir outside the checkout).
Existing artifacts are not overwritten; failure removes temporary/new
publication outputs and does not leave a package or metadata labeled
production. Provenance does not include private configuration paths, aliases
or passwords. The production_signed flag means verified owner certificate
signing only: it does not establish physical-device acceptance or launch readiness.

This path is prepared using synthetic configurations and Fake subprocess
fixtures only. No real key, keystore/password, signed production build, device
installation or account was used for this implementation. Production identity
and hardware acceptance remain owner-controlled prerequisites. A new owner
certificate cannot update the historical Debug-signed app in place; do not
delete app data/credentials to bypass recovery or signer mismatch. Published
1760373 development APKs and existing archives remain immutable.

Initial sideload/self-host target API35 remains the explicit distribution
scope. Google Play target API36, store account, upload/app signing identities
and policy review are a separate task. No signing framework or SDK/JNI/business
logic dependency is added. Reused official APIs:
[APK signing](https://developer.android.com/studio/publish/app-signing),
[apksigner environment password inputs and verification](https://developer.android.com/tools/apksigner),
[private signing properties](https://developer.android.com/build/gradle-tips#remove-private-signing-information-from-your-project).
Gradle and Android build tools retain their Apache-2.0 licenses; the project
MIT license does not replace tool/upstream terms.

Run the meaningful standard-library boundary fixtures with
python3 -m unittest discover -s android-app/tests-python -p 'test_*.py' -v.
The existing Android workflow runs them explicitly before native builds.
They use placeholder files that are not keystores and Fake SDK subprocesses;
a fixture pass cannot establish real signing or device acceptance.
