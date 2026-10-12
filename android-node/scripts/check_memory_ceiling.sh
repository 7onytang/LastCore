#!/usr/bin/env bash
# 1 GiB memory ceiling for the Android-node off-device verification (LC-13).
#
# Two distinct, honestly-labelled guarantees:
#   1. If the kernel supports lowering RLIMIT_AS (Linux), the command runs under
#      a hard 1 GiB address-space cap.
#   2. If it does not (macOS returns EINVAL for `ulimit -v`), the command runs
#      uncapped but its peak RSS is measured and asserted to stay below 1 GiB.
#      That is an observation, not an enforced limit, and the output says so.
#
# Usage:
#   scripts/check_memory_ceiling.sh suite     # the pytest suite (default)
#   scripts/check_memory_ceiling.sh cmd -- <command...>

set -euo pipefail
cd "$(dirname "$0")/.."

CEILING_BYTES=$((1024 * 1024 * 1024))
CEILING_KIB=$((CEILING_BYTES / 1024))
MODE="${1:-suite}"
shift || true

PYTHON="${PYTHON:-$PWD/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

case "$MODE" in
  suite) CMD=("$PYTHON" -m pytest) ;;
  cmd)   CMD=("$@") ;;
  *) echo "usage: $0 {suite|cmd -- <command...>}" >&2; exit 64 ;;
esac

TIME_BIN="/usr/bin/time"
if [ ! -x "$TIME_BIN" ]; then
  echo "SKIP: /usr/bin/time is not available; cannot measure peak RSS" >&2
  exit 2
fi

parse_peak_rss_bytes() {
  local tool
  tool="$(uname -s)"
  if [ "$tool" = "Darwin" ]; then
    awk '/maximum resident set size/ {print $1; exit}'
  else
    awk -F: '/Maximum resident set size/ {gsub(/ /, "", $2); print $2 * 1024; exit}'
  fi
}

HARD_CAP=0
if (ulimit -v "$CEILING_KIB") 2>/dev/null; then
  HARD_CAP=1
fi

echo "-- command: ${CMD[*]}"
if [ "$HARD_CAP" = 1 ]; then
  echo "-- memory guarantee: HARD cap, RLIMIT_AS=${CEILING_BYTES} bytes"
  set +e
  METRICS=$( (ulimit -v "$CEILING_KIB"; "$TIME_BIN" -l "${CMD[@]}") 2>&1 )
  RC=$?
  set -e
else
  echo "-- memory guarantee: MEASURED only. $(uname -s) rejects lowering RLIMIT_AS"
  echo "   (ulimit -v -> EINVAL / 'cannot modify limit: Invalid argument'), so this"
  echo "   run is NOT hard-capped; the peak RSS below is an observation."
  set +e
  METRICS=$("$TIME_BIN" -l "${CMD[@]}" 2>&1)
  RC=$?
  set -e
fi

PEAK_BYTES=$(printf '%s\n' "$METRICS" | parse_peak_rss_bytes)
if [ -z "$PEAK_BYTES" ]; then
  echo "FAIL: could not parse peak RSS from /usr/bin/time output" >&2
  printf '%s\n' "$METRICS" >&2
  exit 1
fi

echo "-- command exit code: $RC"
echo "-- peak RSS: ${PEAK_BYTES} bytes ($((PEAK_BYTES / 1048576)) MiB), ceiling 1024 MiB"

if [ "$RC" -ne 0 ]; then
  echo "FAIL: the measured command itself failed (rc=$RC)" >&2
  printf '%s\n' "$METRICS" >&2
  exit "$RC"
fi
if [ "$PEAK_BYTES" -ge "$CEILING_BYTES" ]; then
  echo "FAIL: peak RSS ${PEAK_BYTES} bytes is not under the 1 GiB ceiling" >&2
  exit 1
fi
echo "-- OK: peak RSS under the 1 GiB ceiling"
