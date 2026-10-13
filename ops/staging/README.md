# Isolated cloud Controller staging preparation

Prepared for the verified Linux x86_64 staging host. This directory does not deploy anything automatically. It reuses Docker Compose and the existing Controller application; official sources are [Compose project isolation](https://docs.docker.com/compose/how-tos/project-name/), [official Python image sources](https://github.com/docker-library/python) (MIT image recipes; CPython/OS retain their respective licenses) and [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/).

The base image is pinned to the official linux/amd64 OCI manifest digest. Python 3.12 runtime dependencies are pinned with exact Linux wheel hashes; the current application requirements are unchanged. Before use, independently review the fixed Git source archive, detached SHA256, pinned dependency lock, container configuration and rollback steps. Archive creation requires tracked source to be clean. The artifact is prepared source, not a built or deployed image.

## Boundaries and layout

- `/opt/lastcore/staging/releases/<full-source-commit>/` holds a verified archive extracted to a new revision directory. Never extract over an existing release.
- `/opt/lastcore/staging/secrets/<commit>.env` is created exclusively on the server with mode 0600. Distinct random admin and registration credentials are not printed, submitted to Git or passed as build arguments. Initialization refuses to overwrite an existing credential file.
- Compose project is `lastcore-staging-<first-12-commit-characters>`. Its named `controller-data` volume belongs only to that revision. No other project's containers, volumes or files are selected or deleted.
- Linux host networking runs Uvicorn explicitly on `127.0.0.1:8000`. No Docker published port, public listener, reverse proxy, domain/TLS setup or firewall change is made. This avoids depending on older Docker's loopback port publishing behavior. No tiny tensor/RPC listener is deployed.
- Runtime is unprivileged uid/gid 10001, read-only filesystem, dropped capabilities, no-new-privileges, one worker, bounded PIDs, 512 MiB memory and 1 CPU. Logs are size limited. Beijing time is explicit.

## Preparation and future operator sequence

Create a fixed source artifact locally, after committing reviewed changes:

```sh
python3 ops/staging/prepare_release.py --output-dir /private/tmp/lastcore-staging-package
```

After deployment authorization is confirmed, upload that artifact and its detached manifest/checksum through the existing verified SSH connection. Check the archive SHA256 on the server **before** extraction, check the target release directory does not exist, and extract under `/opt/lastcore/staging/releases/`. Do not upload credentials. Inspect the manifest's exact source commit and pinned base digest; retain it as release evidence.

Run the following from the newly extracted release on the server, via sudo only where required:

```sh
bash ops/staging/remote_stage.sh init
bash ops/staging/remote_stage.sh build
bash ops/staging/remote_stage.sh start
bash ops/staging/remote_stage.sh status
```

`init` writes private secrets; `build` creates a revision-tagged image and revision label; `start` verifies that label then waits for health. Neither health nor HTTP 200 alone proves task behavior. Before accepting staging, verify server-side image revision/digest, container health/resource settings, the actual loopback-only socket, registration/admin/node authentication, task lifecycle/idle expiry and online backup/read-only diagnostics. Capture only redacted evidence. Remote image construction and runtime acceptance have not yet been run by this preparation step.

## Stop, preserve and rollback

```sh
bash ops/staging/remote_stage.sh stop
```

This uses Compose down **without volume deletion**. Secret files, source releases and named database volumes remain. A failed new revision is stopped with its own project command; the previous verified revision can be started using its own retained directory, secret file, image and volume. Do not reuse a new schema/data volume with an older image without an explicit compatibility review. This initial staging deployment starts with a dedicated fresh volume; production migration, public TLS ingress and mobile device acceptance are later steps. No automatic restore/overwrite is provided.
