# Public IP HTTPS for LastCore

This reuses [Caddy v2.11.7](https://github.com/caddyserver/caddy/releases/tag/v2.11.7) (Apache-2.0), its pinned CertMagic v0.25.6, and the [official systemd service pattern](https://github.com/caddyserver/dist/blob/master/init/caddy.service). No domain purchase or custom ACME implementation is needed. The Linux amd64 official archive SHA256 is `727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b`; artifact URLs, hashes, licenses and inspected source are in `SOURCES.json`.

[Let's Encrypt supports IP certificates](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability/) with the `shortlived` profile (160 hours). CertMagic v0.25.6 explicitly permits Let's Encrypt IP identifiers and carries the profile into its ACME order. Caddy receives an explicit ACME issuer: its automatic default IP policy can otherwise select an internal certificate. `default_sni` selects the expected IP certificate when numeric-IP clients send no SNI, including servers behind cloud NAT.

## Scope

- Dedicated system user `lastcore-caddy` with `CAP_NET_BIND_SERVICE` only; systemd memory 256 MiB, PIDs 128, no-new-privileges, private temp/devices, protected filesystem and explicit Beijing timezone.
- Port 80: CertMagic's HTTP-01 handler only; all other paths and Host headers receive 400 without redirecting or reaching Controller. Do not send real credentials over HTTP, including when testing rejection.
- Port 443: fixed reverse proxy `127.0.0.1:8000`, HTTP/1.1 and HTTP/2, 1 MB request limit. Controller remains bound to loopback and applies its own bearer authorization.
- Access logs are discarded. Debug logging and credential logging are not enabled. Error logging retains Caddy's default sensitive-header redaction.
- Admin API is disabled. Changes require restart; use the fixed service configuration rather than dynamic APIs.
- Root-owned immutable release `/opt/lastcore/tls/releases/<package-manifest-sha256>/`, with `current` symlink. Separate root-owned `Caddyfile` symlink selects staging or production without modifying release contents.
- ACME accounts, keys, certificates and renewal metadata persist under `/var/lib/lastcore-caddy` owned by the service user with mode 0700 and umask 0077. Never print or export its private files. Controller volumes and credentials are separate.
- No SSH, firewall, password, old service, domain, DNS or existing data changes. Installer refuses existing paths/users/units and port conflicts. Use a fresh installation or separately reviewed update sequence.

## Package and local verification

Commit reviewed source first. Python 3.12+ packages exact Git commit contents; it refuses tracked dirty source and an existing output directory. It downloads official archives with fixed SHA256 (or uses a cache with the same validation).

```sh
python3 ops/public-ip-https/prepare_package.py \
  --output-dir /private/tmp/lastcore-tls-package --local-test
python3 /private/tmp/lastcore-tls-package/tests/proxy_smoke.py
```

`--local-test` is for macOS arm64; it additionally downloads the official Mac binary, runs `adapt` and `validate`, then the smoke script can run. The smoke starts a local mock upstream and local test certificate, exercises all HTTP rejection, no-SNI IP TLS, bearer forwarding, trust/expiry warning, untrusted-certificate rejection and 502 log redaction. It makes **no ACME request** and cleans up processes/temporary certificates. These tests prove local transport configuration, not trusted public CA issuance or real automatic renewal.

`SHA256SUMS` covers package inputs. `source.json` records the exact source commit and Beijing packaging time. Save test output outside the package so checksum verification remains stable. Do not execute `install.sh` directly from this repository layout: scripts are distribution operators and expect the generated `package/ops`, `package/tools`, `package/research` layout.

## Reviewed operator deployment

Upload the generated package via the verified dedicated SSH connection. Compare the detached SHA256 of `SHA256SUMS` against the locally reviewed package, then run `sha256sum -c SHA256SUMS` before executing any uploaded script. Inspect fixed source commit and archive hash. No credentials are uploaded.

On the verified Linux x86_64 target, with sudo:

```sh
sudo bash /path/to/package/ops/install.sh
```

This prepares files/user/units and validates the actual Linux binary. It does not start listeners or register an ACME account. It refuses existing TLS release/config paths, all three unit names, an existing service user and busy TCP 80/443. A partial failure preserves created files for inspection; do not repeatedly rerun to overwrite them.

**Human acceptance is required before first ACME use.** Starting Caddy accepts CA terms implicitly. The operator must have the user's explicit acceptance of [LE-SA-v1.8 (July 6, 2026)](https://letsencrypt.org/documents/LE-SA-v1.8-July-06-2026.pdf) and authorization to obtain and automatically renew a certificate for `8.134.161.123`. The start script checks the live ACME directory still advertises this exact agreement and the shortlived profile; a terms change requires fresh acceptance. The acceptance environment variable is an operator guard, not evidence of a human response by itself.

First test the HTTP challenge on the staging CA; its certificate is intentionally untrusted and cannot establish public readiness. After inspecting staging challenge success, switch to production. Both configurations and switch paths are fixed in the package:

```sh
sudo env LASTCORE_LE_TOS_ACCEPTED=https://letsencrypt.org/documents/LE-SA-v1.8-July-06-2026.pdf \
  bash /path/to/package/ops/start.sh staging
# Confirm successful staging issuance from redacted service logs/certificate metadata.
sudo env LASTCORE_LE_TOS_ACCEPTED=https://letsencrypt.org/documents/LE-SA-v1.8-July-06-2026.pdf \
  bash /path/to/package/ops/start.sh production
```

Production config uses the production issuer with Caddy's official staging fallback for failed challenge retries. CertMagic explicitly discards a fallback staging certificate and requires production reissuance; it cannot serve that discarded fallback as its production certificate. Keeping different `ca` and `test_ca` also preserves CertMagic's production ACME Renewal Information (ARI) support. Staging test itself is a separate fixed config and a separate CA account/storage namespace.

## Acceptance and renewal

Before reporting HTTPS online, prove all of these on the actual server/public network, without bypassing TLS verification or sending real tokens over HTTP:

1. Normal public client trust verifies the chain and IP SAN `8.134.161.123`; inspect public issuer, serial, validity (160 hours) and certificate fingerprint. Staging issuer or a trusted private test root is not public acceptance.
2. `https://8.134.161.123/healthz` returns expected JSON, `/docs` and `/openapi.json` agree with the fixed Controller; absent/wrong bearer on `/v1/*` remains rejected. Complete the authorized controller lifecycle using HTTPS and keep real credentials out of output/logs.
3. Plain HTTP to business/health paths returns 400 and never upstream; 8000 remains publicly unreachable. Only 22/80/443 listen as intended, and Caddy runs under its isolated user/resource/capability settings.
4. Restart Caddy; the same production certificate serial/chain/fingerprint remains available from preserved storage, with certificate maintenance active. No unnecessary reissuance should occur on restart.
5. Run `lastcore-tls-monitor.service` and check successful status/state plus an active hourly timer. Record ARI/renewal metadata and service logs without keys/account secrets.

Caddy automatically checks renewal every 10 minutes by default. ARI, when present, supplies the renewal window; without ARI the default renew window is the final third of lifetime (about 53 hours remaining for this profile), with retries managed by CertMagic. Ports 80 and 443 and outbound CA access must remain available; retain the same state directory through restarts and deployments.

The standard-library monitor verifies the locally served certificate against the system's public trust store, checks IP SAN/short lifetime, performs a token-free HTTPS `/healthz`, and warns below 48 hours remaining. It writes a mode-0600 public-metadata state file `/var/lib/lastcore-caddy/tls-monitor.json` plus a journal entry; failure marks the oneshot unit failed. It does not send external notifications. It is compatible with the target's Python 3.6.8 and OpenSSL 1.1.1k. The timer runs hourly and five minutes after boot; it is a monitor, not the renewal scheduler.

Run the monitor's offline regressions from the repository root with no additional dependencies:

```sh
python3 -m unittest discover -s ops/public-ip-https/tests -p 'test_*.py' -v
```

CI runs these on Python 3.6 and 3.12. Fixtures cover fragmented health responses, the 48-hour warning boundary, wrong IP SAN, excessive certificate lifetime, simulated trust/expiry handshake failures, malformed/unhealthy/oversized health replies, failed state writes and replacement of stale success by error. They create no network connections, ACME requests or compute tasks and do not prove a live renewal. The implementation reuses the maintained [CPython SSL implementation](https://github.com/python/cpython/blob/main/Lib/ssl.py) under the PSF license and existing Caddy/CertMagic renewal; a separate monitoring service or dependency would add unnecessary integration cost.

An initial certificate, persisted restart and active renewal worker establish issuance/persistence/renewal management. They do **not** prove a future scheduled real renewal has happened. Record a changed valid production serial/NotAfter after a later real renewal before claiming that test passed. Inspect:

```sh
sudo systemctl start lastcore-tls-monitor.service
sudo systemctl status lastcore-caddy.service lastcore-tls-monitor.timer --no-pager
sudo journalctl -u lastcore-tls-monitor.service --no-pager -n 20
```

## Stop and rollback

```sh
sudo bash /path/to/package/ops/rollback.sh
```

This disables/stops only the dedicated TLS timer/service and leaves the Controller available on loopback. It retains service user, units, both fixed configs/releases, acceptance record, ACME accounts/keys/certificates/renewal metadata and all Controller volumes. A reverted public ingress means public nodes disconnect; no application data is restored, overwritten or deleted. To resume after inspection, use the same accepted `start.sh production` and preserved storage. Do not delete ACME state, dump private keys or overwrite an earlier release to make a failed install look clean.
