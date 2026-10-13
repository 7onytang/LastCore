#!/usr/bin/env bash
# Invoke only after user explicitly accepts this exact CA agreement + automatic renewal.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
accepted='https://letsencrypt.org/documents/LE-SA-v1.8-July-06-2026.pdf'
mode=${1:-}
case "$mode" in
    staging) selected=current/Caddyfile.staging ;;
    production) selected=current/Caddyfile ;;
    *) echo 'usage: start.sh staging|production' >&2; exit 1 ;;
esac
[[ ${LASTCORE_LE_TOS_ACCEPTED:-} == "$accepted" ]] || { echo 'User acceptance of the current LE subscriber agreement is required.' >&2; exit 1; }
# Check directory agreement has not changed; do NOT create an account here.
python3 - "$accepted" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen('https://acme-v02.api.letsencrypt.org/directory', timeout=15) as response:
    directory = json.load(response)
assert directory['meta']['termsOfService'] == sys.argv[1], 'CA terms changed; obtain fresh user acceptance'
assert 'shortlived' in directory['meta']['profiles'], 'CA no longer offers shortlived profile'
PY
existing=$(readlink /opt/lastcore/tls/Caddyfile)
[[ "$existing" == current/Caddyfile || "$existing" == current/Caddyfile.staging ]] || { echo 'Unexpected TLS config link; stop and review.' >&2; exit 1; }
[[ -L /opt/lastcore/tls/current ]] || { echo 'TLS release is not installed.' >&2; exit 1; }
umask 077
printf 'User accepted LE-SA-v1.8 and authorized IP8.134.161.123 issuance and automatic renewal.\n%s\n' "$accepted" > /opt/lastcore/tls/LE-SA-v1.8-accepted
ln -sfn "$selected" /opt/lastcore/tls/Caddyfile
systemctl enable lastcore-caddy.service
systemctl restart lastcore-caddy.service
# Readiness must be assessed by external trusted TLS probe, not unit startup alone.
if [[ "$mode" == production ]]; then
    systemctl enable --now lastcore-tls-monitor.timer
else
    # Staging is untrusted by design: do not report it as a healthy public CA deployment.
    systemctl disable --now lastcore-tls-monitor.timer
fi
printf 'Services started. Check trusted-chain/SAN/health/auth and timer status before declaring HTTPS ready.\n'
