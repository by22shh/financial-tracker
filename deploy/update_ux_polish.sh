#!/usr/bin/env bash
# Deploy the UX/copy release and restore the previous image/source on failure.
set -euo pipefail

base=/opt/fintracker
release=ux-polish-20260920-r9
new_image=fintracker:$release

cd "$base/deploy"
compose=(docker compose -f compose.production.yml)

wait_healthy() {
  local deadline=$((SECONDS + 360))
  local service container status all_healthy
  while ((SECONDS < deadline)); do
    all_healthy=true
    for service in api worker scheduler polling postgres; do
      container=$("${compose[@]}" ps -q "$service")
      if [[ -z "$container" ]]; then
        all_healthy=false
        continue
      fi
      status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")
      if [[ "$status" != healthy ]]; then
        all_healthy=false
      fi
    done
    if [[ "$all_healthy" == true ]] && curl --fail --silent --show-error \
      http://127.0.0.1:18080/health/ready >/dev/null; then
      return 0
    fi
    sleep 5
  done
  "${compose[@]}" --profile app --profile polling ps
  return 1
}

docker image inspect "$new_image" --format '{{.Id}}' >/dev/null
test -L "$base/source"
test -f "$base/releases/$release/release-manifest.json"

previous_source=$(readlink "$base/source")
cp -p .env ".env.before-$release"
bash "$base/deploy/backup.sh"

rollback() {
  status=$?
  trap - ERR
  cp -p ".env.before-$release" .env
  ln -sfn "$previous_source" "$base/source"
  "${compose[@]}" --profile app --profile polling up -d
  wait_healthy || true
  exit "$status"
}
trap rollback ERR

"${compose[@]}" stop polling scheduler worker api
python3 - <<'PY'
from pathlib import Path

path = Path('.env')
lines = [line for line in path.read_text().splitlines() if not line.startswith('FINTRACKER_IMAGE=')]
lines.append('FINTRACKER_IMAGE=fintracker:ux-polish-20260920-r9')
path.write_text('\n'.join(lines) + '\n')
PY
ln -sfn "releases/$release" "$base/source"
"${compose[@]}" --profile app --profile polling up -d
wait_healthy
curl --fail --silent --show-error http://127.0.0.1:18080/health/ready

trap - ERR
