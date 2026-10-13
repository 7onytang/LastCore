# Verified IP HTTPS deployment snapshot — 2026-10-13 Beijing time

This is a dated observation, not a promise of future certificate expiry, renewal success or complete distributed inference. The public Controller is `https://8.134.161.123`; application health, docs and unauthorized API rejection were observed at 12:17 Beijing time.

## Fixed inputs and authorized scope

- Deployed TLS source commit: `e83b1cbb8cf20a5ff4d49bb5da8763f5f9140cbb`.
- Uploaded package archive SHA256: `d8983fbbec9463c6ebe9a9742cdd5d912d5f9ec33a201080fdceda5bc987e4b3`.
- Package manifest SHA256 / installed release ID: `6a92a8d063e7d18b218bb7efa90c45f77dec13b45f99f5f72171a682e4c3a358`.
- Official Caddy Linux amd64 v2.11.7 archive SHA256: `727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b`, verified before installation. Actual Linux binary reported v2.11.7.
- User explicitly accepted [LE-SA-v1.8](https://letsencrypt.org/documents/LE-SA-v1.8-July-06-2026.pdf) and authorized IP certificate issuance and automatic renewal. The live CA directory still advertised the same agreement and the `shortlived` profile before starting each phase.
- Dedicated new user, release/config paths, three units and free ports were checked; no prior TLS service existed. Installer refused replacement. No domain/DNS purchase, firewall/SSH/password change, old-service replacement or data deletion occurred.

The Controller image/source has its own separate deployment record. The TLS source SHA above does not claim the full later release bundle SHA.

## Actual external and server checks

A real staging HTTP-01 challenge succeeded first. Its temporary public certificate contained the IP SAN and a 160-hour lifetime. Standard public trust correctly rejected its `(STAGING) Artificial Amaranth YE1` issuer. The fixed configuration symlink was then switched to production.

Production HTTP-01 completed, production chains were downloaded and Caddy reported successful issuance. External Python `ssl.create_default_context()` and ordinary `curl` verified the public endpoint with their normal trust settings. Numeric-IP TLS used no SNI and still selected the correct IP certificate.

Production leaf at this snapshot:

- SAN: `IP Address:8.134.161.123`.
- Issuer: `Let's Encrypt / YE2`.
- Serial: `05084A833F74D0551DF7E0A4ACE9ABDBDF24`.
- SHA256 fingerprint: `8da1b600e8e1d0e8311c57971f6ca2a515d6a60f9a9f44f74c75f1b15f248647`.
- Validity: UTC `2026-10-13 03:16:34` to `2026-10-19 19:16:33` (160 hours minus one second). Expiry in Beijing: `2026-10-20 03:16:33`.
- The actual server-sent chain was leaf → YE2 → Root YE cross-signed by ISRG Root X2 → ISRG Root X2 cross-signed by ISRG Root X1. The local Python verified path selected its installed self-signed ISRG Root X2 trust anchor. This observed bridge does not constitute an Android device handshake test.

After restarting the dedicated Caddy service, the external trusted leaf serial and SHA256 fingerprint were unchanged. Certificate maintenance restarted using the persisted store, without another issuance.

Public `/healthz` returned `{"status":"ok"}`; `/docs` and `/openapi.json` returned 200, with eight protected `/v1/` paths in OpenAPI. Missing or deliberately invalid bearer on `/v1/nodes` returned 401. These checks transmitted no real token. The separate Controller deployment work exercises authorized stateful API flows.

Public HTTP `/healthz`, `/docs`, `/v1/nodes` and a nonexistent ACME challenge path all returned 400. No business route was proxied or redirected from port 80. Public port 8000 was unreachable; server sockets showed Uvicorn on `127.0.0.1:8000`, Caddy on TCP 80/443, and existing SSH on 22.

Actual Caddy service properties matched the reviewed unit: non-root `lastcore-caddy`, sole `CAP_NET_BIND_SERVICE`, `NoNewPrivileges=yes`, memory 268435456 bytes, CPU quota 500 ms/second and TasksMax 128. ACME storage was mode 0700 and generated key files mode 0600. Private keys and Controller credentials were not read or emitted as evidence.

## Renewal management and remaining proof

Real production ARI was stored and retained through restart. At the snapshot, the persisted selected renewal time was UTC `2026-10-16 11:02:16`, or Beijing `2026-10-16 19:02:16`, within the CA window Beijing `18:10:23–21:21:13` that day. ARI is refreshable, so this time may change.

The dedicated hourly monitor was enabled, and after Controller readiness a manual systemd invocation completed with `Result=success`, `ExecMainStatus=0`, trusted chain, IP SAN, HTTPS health `ok`, TLSv1.3 and about 158.99 hours remaining. Its public-metadata state file was mode 0600. An initial run before Controller readiness had failed on upstream health; the successful check was rerun against the actual ready Controller. The timer's next observed execution was 13:16 Beijing time. It logs/records locally and does not send external notifications.

**A future scheduled real renewal has not yet occurred.** Initial production issuance, restart persistence, a stored ARI plan, active certificate maintenance and a successful monitor establish renewal management. A later changed valid production serial/NotAfter is required to claim actual automatic renewal proved. No keys were deleted and no forced production reissuance was used to manufacture that evidence.

Rollback remains the reviewed `rollback.sh`: disable/stop only the dedicated TLS timer/service; retain all certificate/account state, source releases, acceptance record, service user, Controller secrets and database volumes. No rollback deletion or restoration was performed.
