#!/usr/bin/env bash
# Probe a real Android device over adb and run the Android-node admission
# planner with the measured values.  Evidence for LC-13 phase-1: it turns
# "what the phone reports" into the exact inputs budget.py/admission use.
#
# Usage:
#   scripts/probe_device.sh                        # default segment 0:2 of tiny model
#   scripts/probe_device.sh --layers 0:12 --layers-total 24 \
#       --d-model 896 --heads 14 --vocab 151936 --weights-bytes 616000000 \
#       --tokens 2048
#
# Requires: adb on PATH, one device authorised (`adb devices` -> "device").
# Without a device it exits 2 and prints the concrete blocker (no emulator
# fallback: LC-13 forbids passing off emulator results as real-device evidence).

set -euo pipefail
cd "$(dirname "$0")/.."

if [ -x .venv/bin/python ]; then
  PYTHON="$PWD/.venv/bin/python"
fi

if ! command -v adb >/dev/null 2>&1; then
  echo "BLOCKED: adb is not installed; no real Android device can be probed." >&2
  echo "  install: Android SDK platform-tools (developer.android.com/tools/releases/platform-tools)" >&2
  exit 2
fi

state="$(adb get-state 2>/dev/null || true)"
if [ "$state" != "device" ]; then
  echo "BLOCKED: no authorised Android device attached (adb get-state='${state:-none}')." >&2
  echo "  enable USB debugging, reconnect, accept the RSA prompt, then re-run." >&2
  exit 2
fi

sh_device() { adb shell "$1" 2>/dev/null | tr -d '\r' | head -n 1; }

abi="$(sh_device 'getprop ro.product.cpu.abi')"
sdk="$(sh_device 'getprop ro.build.version.sdk')"
release="$(sh_device 'getprop ro.build.version.release')"
model="$(sh_device 'getprop ro.product.model')"
soc="$(sh_device 'getprop ro.soc.model')"
page_size="$(sh_device 'getconf PAGE_SIZE')"

mem_total_kb="$(sh_device "grep -m1 '^MemTotal:' /proc/meminfo" | awk '{print $2}')"
mem_avail_kb="$(sh_device "grep -m1 '^MemAvailable:' /proc/meminfo" | awk '{print $2}')"
mem_total_mb=$(( ${mem_total_kb:-0} / 1024 ))
mem_avail_mb=$(( ${mem_avail_kb:-0} / 1024 ))

battery_raw="$(sh_device 'cat /sys/class/power_supply/battery/capacity')"
battery_status="$(sh_device 'cat /sys/class/power_supply/battery/status')"
thermal_max="$(adb shell 'for z in /sys/class/thermal/thermal_zone*/temp; do cat $z 2>/dev/null; done' 2>/dev/null \
  | tr -d '\r' | sort -n | tail -n 1)"

echo "== device =="
echo "model=$model abi=$abi sdk=$sdk release=$release soc=$soc page_size=$page_size"
echo "mem_total_mb=$mem_total_mb mem_available_mb=$mem_avail_mb"
echo "battery=$battery_raw status=$battery_status thermal_zone_max_millideg=$thermal_max"

case "$abi" in
  arm64-v8a) : ;;
  *) echo "WARNING: abi '$abi' is not arm64-v8a; the Android node targets arm64-v8a only." >&2 ;;
esac
if [ "$page_size" = "16384" ]; then
  echo "NOTE: 16 KB page size device -> native libs must be NDK r28+ (16 KB aligned)."
fi

LASTCORE_ANDROID_MACHINE=arm64
LASTCORE_ANDROID_PLATFORM=Android
LASTCORE_ANDROID_IS_ANDROID=true
LASTCORE_ANDROID_TOTAL_MEMORY_MB="$mem_total_mb"
LASTCORE_ANDROID_AVAILABLE_MEMORY_MB="$mem_avail_mb"
if [ -n "$battery_raw" ]; then
  LASTCORE_ANDROID_BATTERY_PERCENT="$battery_raw"
fi
if [ -n "$battery_status" ]; then
  if [ "$battery_status" = "Charging" ] || [ "$battery_status" = "Full" ]; then
    LASTCORE_ANDROID_CHARGING=true
  else
    LASTCORE_ANDROID_CHARGING=false
  fi
fi
# Same 45/50/60 C heuristic as device.py; calibrate against the framework
# PowerManager.getCurrentThermalStatus() before trusting it (FEASIBILITY.md §3).
if [ -n "$thermal_max" ]; then
  if [ "${thermal_max#-}" -lt 1000 ]; then
    thermal_max=$(( thermal_max * 1000 ))
  fi
  if   [ "$thermal_max" -ge 60000 ]; then LASTCORE_ANDROID_THERMAL=critical
  elif [ "$thermal_max" -ge 50000 ]; then LASTCORE_ANDROID_THERMAL=serious
  elif [ "$thermal_max" -ge 45000 ]; then LASTCORE_ANDROID_THERMAL=fair
  else LASTCORE_ANDROID_THERMAL=nominal
  fi
fi
export LASTCORE_ANDROID_MACHINE LASTCORE_ANDROID_PLATFORM LASTCORE_ANDROID_IS_ANDROID
export LASTCORE_ANDROID_TOTAL_MEMORY_MB LASTCORE_ANDROID_AVAILABLE_MEMORY_MB
export LASTCORE_ANDROID_BATTERY_PERCENT LASTCORE_ANDROID_CHARGING LASTCORE_ANDROID_THERMAL

exec "${PYTHON:-python3}" -m lastcore_android_node plan "$@"
