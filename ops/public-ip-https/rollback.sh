#!/usr/bin/env bash
# Return to loopback-only Controller. Keep all certificate/account material and business data.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
systemctl disable --now lastcore-tls-monitor.timer
systemctl stop lastcore-tls-monitor.service
systemctl disable --now lastcore-caddy.service
ss -ltnp
printf 'TLS services stopped. /var/lib/lastcore-caddy and all Controller volumes are preserved.\n'
