# Static-only Demo deployment

This adds only Caddy's built-in [file_server](https://caddyserver.com/docs/caddyfile/directives/file_server), mutually exclusive [handle_path](https://caddyserver.com/docs/caddyfile/directives/handle_path) and the original API fallback. Caddy2.11.7/Apache-2.0 is already installed; no new service, library, domain or Controller deployment is needed. The operator is deliberately bounded to the exact existing Caddyfile SHA; another configuration requires review, not overwrite.

The public staged site must contain the complete `index.html` and `lastcore-demo-kit.zip` before preparation. Copy only public HTML/CSS/JS/JSON/SVG/images/fonts/licenses/readme and the reviewed kit. The exporter Python helper is excluded. Symlinks, private filenames/credential text, unsupported files, recursive zip/self-members and oversized content fail closed. The unique material owner supplies the final independently reviewed unified ZIP (source, redacted AI logs, actual PNGs, offline HTML, script and provenance). Copy that same byte sequence to lastcore-demo-kit.zip; record its original canonical name and identical SHA. This operator never creates another ZIP/source archive.

After fixed frontend source and independent QA/review:

```sh
python3 ops/demo/prepare_static.py --site-dir /private/public-staged-site --output /private/new-packet --source-commit FINAL_MAIN_SHA --frontend-commit REVIEWED_FRONTEND_SHA --canonical-zip-name ORIGINAL_MATERIAL_ZIP_NAME
python3 -m unittest discover -s ops/demo/tests -v
```

Review manifest hashes and both copied operator script hashes before uploading/executing. On the existing host, verify uploaded manifest SHA and operator bytes against the locally reviewed packet, then invoke:

```sh
sudo python3 /uploaded/packet/deploy_static.py --packet /uploaded/packet --manifest-sha256 REVIEWED_MANIFEST_SHA256
```

It creates a root-owned public static release/config under `/opt/lastcore/demo/`, mode0755 directories/0644 files. Never use repository, database, private state or TLS storage as the static root. The original Caddyfile symlink and any old static pointer are retained for rollback. TLS/global/HTTP80 challenge/rejection, logs, headers, upstream and service units remain identical except the reviewed HTTPS routes. GET/HEAD `/` and `/demo` redirect to `/demo/`; its missing assets are404 and non-GET/HEAD405. All other paths keep original proxy behavior.

The actual service uses `caddy run --config` without `--resume` and admin off. Use official [SIGUSR1 configuration reload](https://caddyserver.com/docs/command-line#signals), preserving the main PID; do not enable admin API or restart Controller. Validate as the existing service user first. The operator verifies default public TLS and unchanged certificate fingerprint, root/demo, served ZIP SHA, health/docs/openapi and API401. Any switching/reload/acceptance error restores original aliases and reloads the original configuration. Created releases remain for diagnosis; no credential, certificate, database or old release is removed. The deployment receipt is outside the public static root.

Sources freeze before packaging; screenshot manifest separately records its actual frontend SHA/URL/time, ZIP has its own hash, final main source supplies the source archive. Do not overwrite the old alpha.1 thirteen assets or recursively package the ZIP itself. Source/log redaction is verified by the material owner and independent reviewer; the static packet verifies its identity, safe ZIP paths, absence of keys/models/APKs and CRC. Tests use private temporary fixtures/mock routing and do not prove a cloud deployment or create model jobs.
