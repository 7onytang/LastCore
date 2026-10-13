#!/usr/bin/env bash
# Run only on the staging host after source archive hash verification/extraction.
set -euo pipefail
umask 077
export TZ=Asia/Shanghai
release_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
commit="$(basename "$release_dir")"
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo 'release directory must be named by full source commit' >&2; exit 2; }
[[ "$release_dir" == "/opt/lastcore/staging/releases/$commit" ]] || { echo 'unexpected staging root' >&2; exit 2; }
project="lastcore-staging-${commit:0:12}"
secret_dir=/opt/lastcore/staging/secrets
secret_file="$secret_dir/$commit.env"
action="${1:-status}"
if [ "$action" = init ]; then
  install -d -m 0700 "$secret_dir"
  test ! -e "$secret_file" || { echo 'existing secrets retained; init refused' >&2; exit 2; }
  python3 - "$secret_file" "$commit" <<'PY'
import os,secrets,sys
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
 f.write('SOURCE_COMMIT='+sys.argv[2]+'\n')
 f.write('CONTROLLER_ADMIN_TOKEN='+secrets.token_hex(32)+'\n')
 f.write('CONTROLLER_REGISTRATION_TOKEN='+secrets.token_hex(32)+'\n')
 f.flush();os.fsync(f.fileno())
PY
  echo 'private server-side credentials initialized'
  exit 0
fi
test -f "$secret_file" || { echo 'initialize credentials first' >&2; exit 2; }
test "$(stat -c %a "$secret_file")" = 600 || { echo 'credentials file must be mode 0600' >&2; exit 2; }
compose=(docker compose --project-name "$project" --env-file "$secret_file" -f "$release_dir/ops/staging/compose.yaml")
case "$action" in
  build) "${compose[@]}" config --quiet; "${compose[@]}" build --pull controller ;;
  start)
    docker image inspect "lastcore-controller-staging:$commit" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' | grep -Fxq "$commit"
    "${compose[@]}" up --no-build --wait --wait-timeout 120
    curl --fail --silent http://127.0.0.1:8000/healthz
    ;;
  status) "${compose[@]}" ps --all ;;
  stop) "${compose[@]}" down --timeout 20 ;; # No --volumes, no database deletion.
  *) echo 'usage: remote_stage.sh init|build|start|status|stop' >&2; exit 2 ;;
esac
