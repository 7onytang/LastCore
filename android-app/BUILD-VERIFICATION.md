# Native Android bootstrap verification

Build source: isolated android-app, AGP8.13.0 / Gradle8.13 / Temurin17.0.20.1+1 / Android platform35 + build-tools35.0.0. No global tool installation. User explicitly accepted Android SDK agreement; license acknowledgement recorded separately.

Actual command: ./build-apk.sh. Result: exit0; BUILD SUCCESSFUL in 1m5s, 62 tasks executed, assembleDebug and assembleDebugAndroidTest. javac/java separately executed seven admission-denial checks, all PASS. Android native sources and device instrumentation tests compiled.

| Artifact | Bytes | SHA256 |
| --- | ---: | --- |
| app/build/outputs/apk/debug/app-debug.apk | 12885 | 3a7a877a7612c0c35c39085576495b2b69a84350ffd86a62596d712059e6cda6 |
| app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk | 4469143 | 83dfde6ee0c6f1b4adc011507aaa20f15481b952864ec8a6bcc3dc34881ef0e9 |

Actual aapt manifest checks: package ai.lastcore.android; INTERNET only; allowBackup false; usesCleartextTraffic false; minSdk29/targetSdk35; debuggable true; launcher Activity only; no service/receiver. apksigner verification is recorded in local verification logs. No release signing.

Device boundary: local adb initial server startup did not return a device list and the client was interrupted; no devices were controlled, no APK was installed. Keystore/tamper/delete/manifest instrumentation execution and real framework values remain unverified. No physical Android, native runtime/JNI, model export, Controller/transport integration, cancellation memory/device behavior or product launch claim.

Other observations: one pinned-Gradle deprecation notice warns about Gradle9; build uses8.13 and succeeds. Early preflight correctly refused missing Java runtime before isolated installation. SDK auto-download disabled; caches/toolchain/binary outputs ignored and retained locally. No emulator, NDK or model downloaded. Existing android-node and Controller unchanged.

Raw local build output: verification/build.log. Application and test APK paths above are relative to android-app. Hashes identify these exact debug artifacts; debug signing/environment can change bytes on another build.
