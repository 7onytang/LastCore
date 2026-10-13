# LastCore native Android bootstrap

This separate native application is a buildable diagnostics/credential foundation, not a distributed inference node. Start is disabled and admission fails closed because real model export, runtime and transport are not integrated. Existing android-node Python and Controller contracts remain untouched.

## Build

Pinned AGP8.13.0 / Gradle8.13 / JDK17 / compile+target API35 / min API29. Set JAVA_HOME to an isolated JDK17, ANDROID_HOME to the isolated SDK, add isolated Gradle8.13 to PATH. Run `./build-apk.sh`; debug output is `app/build/outputs/apk/debug/app-debug.apk`. The first dependency resolution needs network. SDK auto-download is disabled; Google SDK terms must be accepted by the user before installing SDK packages. No automatic acceptance script is provided.

Project-local caches, SDK and toolchain downloads are ignored. No global installation, no emulator or model download is needed for shell compilation. Required SDK packages: platforms;android-35, build-tools;35.0.0, platform-tools. NDK is only needed once a real native backend is integrated. SDK licenses and release signing remain separate human-controlled actions.

## Current behavior and security

Activity reads Android ActivityManager memory, PowerManager thermal state, BatteryManager battery/charging APIs. It requests INTERNET only; no foreground service, boot receiver, sensitive permissions or background task is enabled. HTTP cleartext is disabled. Startup does not contact Controller or accept work.

CredentialVault is an unused integration component: AES-GCM key generated within AndroidKeyStore, encrypted envelope written atomically to app-private no-backup storage; `clear()` deletes encrypted file and key. Manifest disables backups; secrets are never displayed/logged. No plaintext node credentials are stored. On-device encryption/tamper/clear behavior must be instrumented before integrating account registration. The current Activity never asks for credentials.

Pure Java admission checks reject missing backend, unavailable metadata, severe thermal state, battery constraints and missing measured model metadata. They do not validate actual inference or true device policy. Device checks must additionally cover Keystore tamper, credential deletion, process death, sleep, cancel, listener release and native memory limits.

## Backend path

Prefer official ONNX Runtime Android AAR for the existing proposed per-segment ONNX path, once real engine export and native communication are approved and implemented. Official llama.cpp Android/CMake/NDK is a maintained MIT whole-GGUF reference, but cannot be treated as the current LastCore assignment fragment executor without a separately reviewed adapter. No library, JNI implementation, weights, server daemon or protocol change is included here.

Keep initial inference screen-bound. Android foreground service types/permissions and time limits must be selected based on the actual approved use case; dataSync is not unlimited generic compute. No background/foreground-service permission is preemptively added.

## Sources

- https://developer.android.com/build/releases/agp-8-13-0-release-notes
- https://developer.android.com/studio/terms
- https://developer.android.com/privacy-and-security/keystore
- https://developer.android.com/develop/background-work/services/fgs/service-types
- https://onnxruntime.ai/docs/tutorials/mobile/
- https://github.com/ggml-org/llama.cpp/blob/master/docs/android.md

## Verification status

Actual isolated build completed: `assembleDebug assembleDebugAndroidTest`, 62 tasks, BUILD SUCCESSFUL, exit0. Seven pure Java fail-closed admission checks passed. aapt inspected the compiled manifest: INTERNET only, backup=false, cleartext=false, min29/target35. APK is a debug diagnostics bootstrap; no native inference or distributed work is enabled. See BUILD-VERIFICATION.md for hashes and limits. Instrumentation tests were compiled, not executed; no real Android device result is available.

Instrumented security tests compile into a separate APK and must run only on an explicitly selected device: `gradle connectedDebugAndroidTest`. They cover Keystore roundtrip/non-exportability, ciphertext tamper refusal, clear, backup/cleartext manifest behavior and missing-backend denial. They were not run in the current environment.

## Artifact CI

`android-native.yml` builds debug APK, unsigned release APK and instrumentation APK with JDK17/Gradle8.13. `artifact-binding.py` requires a committed clean Android checkout, embeds the source SHA and tracked-source hashes, then checks the compiled manifest, shipped licenses and APK hashes. Instrumentation compilation is not device execution. `verification/artifact-manifest.json` distinguishes debug-key signing from the unsigned release. No upload to an app store or release signing happens. The macOS workflow independently builds a real bundle, checks all manifest files and frozen project code against checkout source, verifies full license texts, and exercises real local tiny CPU cancellation/restart; it does not establish pretrained distributed LLM support.
