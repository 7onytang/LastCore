#!/usr/bin/env bash
# Prepare a FIRST isolated LastCore TLS installation. Does not start Caddy or contact a CA.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
package_dir=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
[[ $(uname -m) == x86_64 ]] || { echo 'Only Linux x86_64 supported.' >&2; exit 1; }
[[ $(uname -s) == Linux ]] || { echo 'Only Linux supported.' >&2; exit 1; }
cd -- "$package_dir"
sha256sum -c SHA256SUMS
release_id=$(sha256sum SHA256SUMS | cut -d ' ' -f1)
release_dir="/opt/lastcore/tls/releases/$release_id"
expected=727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b
[[ $(sha256sum tools/caddy_2.11.7_linux_amd64.tar.gz | cut -d ' ' -f1) == "$expected" ]] || exit 1
[[ ! -e /opt/lastcore/tls/current && ! -L /opt/lastcore/tls/current ]] || { echo 'Existing TLS release; stop and review update/rollback.' >&2; exit 1; }
[[ ! -e "$release_dir" && ! -L "$release_dir" ]] || { echo 'Existing release directory; stop and review.' >&2; exit 1; }
[[ ! -e /opt/lastcore/tls/Caddyfile && ! -L /opt/lastcore/tls/Caddyfile ]] || { echo 'Existing TLS config link; stop and review.' >&2; exit 1; }
for name in lastcore-caddy.service lastcore-tls-monitor.service lastcore-tls-monitor.timer; do
    [[ ! -e "/etc/systemd/system/$name" && ! -L "/etc/systemd/system/$name" ]] || { echo 'Existing LastCore unit; stop and review.' >&2; exit 1; }
    [[ $(systemctl show "$name" --property=LoadState --value) == not-found ]] || { echo 'Existing loaded LastCore unit; stop and review.' >&2; exit 1; }
done
# Refuse unrelated listeners: no killing old services or firewall/SSH changes.
if ss -H -ltn '( sport = :80 or sport = :443 )' | python3 -c 'import sys; sys.exit(0 if sys.stdin.read().strip() else 1)'; then
    echo 'Port 80/443 already listening; stop and review.' >&2; exit 1
fi
if getent passwd lastcore-caddy >/dev/null; then
    echo 'Service account already exists; stop and review.' >&2; exit 1
fi
useradd --system --home-dir /var/lib/lastcore-caddy --no-create-home --shell /sbin/nologin lastcore-caddy
install -d -m 0755 /opt/lastcore/tls/releases
install -d -m 0755 "$release_dir"
install -d -o lastcore-caddy -g lastcore-caddy -m 0700 /var/lib/lastcore-caddy
# Official archive digest checked before any system resources are created.
tar -xzf tools/caddy_2.11.7_linux_amd64.tar.gz --directory "$release_dir" caddy
chown root:root "$release_dir/caddy"
chmod 0755 "$release_dir/caddy"
install -m 0644 ops/Caddyfile "$release_dir/Caddyfile"
install -m 0644 ops/Caddyfile.staging "$release_dir/Caddyfile.staging"
install -m 0644 ops/tls_monitor.py "$release_dir/tls_monitor.py"
install -m 0644 SHA256SUMS "$release_dir/SHA256SUMS"
install -m 0644 research/CADDY-LICENSE.txt "$release_dir/LICENSE-Caddy.txt"
"$release_dir/caddy" version
# Validation provisions the config but does not start listeners or issue certificates.
runuser -u lastcore-caddy -- env HOME=/var/lib/lastcore-caddy XDG_CONFIG_HOME=/var/lib/lastcore-caddy/config XDG_DATA_HOME=/var/lib/lastcore-caddy/data "$release_dir/caddy" validate --config "$release_dir/Caddyfile" --adapter caddyfile
ln -s "releases/$release_id" /opt/lastcore/tls/current
ln -s current/Caddyfile.staging /opt/lastcore/tls/Caddyfile
for name in lastcore-caddy.service lastcore-tls-monitor.service lastcore-tls-monitor.timer; do
    install -m 0644 "ops/$name" "/etc/systemd/system/$name"
done
systemd-analyze verify /etc/systemd/system/lastcore-caddy.service /etc/systemd/system/lastcore-tls-monitor.service /etc/systemd/system/lastcore-tls-monitor.timer
systemctl daemon-reload
printf 'Installed release %s; no service started, no ACME account or certificate requested.\n' "$release_id"
