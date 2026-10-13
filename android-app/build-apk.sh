#!/bin/sh
set -eu
cd "$(dirname "$0")"
ANDROID_APP_ROOT=$(pwd)
if [ -z "${JAVA_HOME:-}" ]; then
    for candidate in "$ANDROID_APP_ROOT"/.toolchain/jdk/*/Contents/Home; do
        if [ -x "$candidate/bin/javac" ]; then JAVA_HOME="$candidate"; break; fi
    done
fi
if [ -n "${JAVA_HOME:-}" ]; then export JAVA_HOME; PATH="$JAVA_HOME/bin:$PATH"; fi
ANDROID_HOME=${ANDROID_HOME:-"$ANDROID_APP_ROOT/.toolchain/sdk"}
ANDROID_USER_HOME="$ANDROID_APP_ROOT/.android-user"
GRADLE_USER_HOME="$ANDROID_APP_ROOT/.gradle-cache"
PATH="$ANDROID_APP_ROOT/.toolchain/gradle-8.13/bin:$ANDROID_HOME/platform-tools:$PATH"
export ANDROID_HOME ANDROID_USER_HOME GRADLE_USER_HOME PATH
command -v javac >/dev/null 2>&1 || { echo "BLOCKED: JDK17 required" >&2; exit 2; }
java -version >/dev/null 2>&1 || { echo "BLOCKED: Java runtime unavailable" >&2; exit 2; }
command -v gradle >/dev/null 2>&1 || { echo "BLOCKED: isolated Gradle8.13 missing" >&2; exit 2; }
test -d "$ANDROID_HOME/platforms/android-35" || { echo "BLOCKED: Android platform35 missing" >&2; exit 2; }
python3 artifact-binding.py prepare
gradle --no-daemon --console=plain assembleDebug assembleDebugAndroidTest assembleRelease
mkdir -p verification/admission-classes
javac -d verification/admission-classes app/src/main/java/ai/lastcore/android/AdmissionPolicy.java tests/ai/lastcore/android/AdmissionPolicyCheck.java
java -cp verification/admission-classes ai.lastcore.android.AdmissionPolicyCheck

python3 artifact-binding.py verify
