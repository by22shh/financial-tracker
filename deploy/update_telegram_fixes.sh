#!/usr/bin/env bash
# Deploy the audited Telegram fixes; retain previous image/source for rollback.
set -euo pipefail
base=/opt/fintracker
release=telegram-fixes-20260920-r4
new_image=fintracker:$release
cd "$base/deploy"
compose=(docker compose -f compose.production.yml)
docker image inspect "$new_image" --format '{{.Id}}' >/dev/null
test -L "$base/source"
test -f "$base/releases/$release/release-manifest.json"
test -f "$base/repair_tg03_test_schedule.py"
previous_source=$(readlink "$base/source")
cp -p .env ".env.before-$release"
bash "$base/deploy/backup.sh"
rollback() {
  status=$?
  trap - ERR
  cp -p ".env.before-$release" .env
  ln -sfn "$previous_source" "$base/source"
  "${compose[@]}" --profile app --profile polling up -d --wait --wait-timeout 180
  exit "$status"
}
trap rollback ERR
"${compose[@]}" stop polling scheduler worker api
python3 - <<'PY'
from pathlib import Path
p=Path('.env')
lines=p.read_text().splitlines()
lines=[line for line in lines if not line.startswith('FINTRACKER_IMAGE=')]
lines.append('FINTRACKER_IMAGE=fintracker:telegram-fixes-20260920-r4')
p.write_text('\n'.join(lines)+'\n')
PY
ln -sfn "releases/$release" "$base/source"
"${compose[@]}" run --rm --no-deps -T --entrypoint python api - --apply < "$base/repair_tg03_test_schedule.py"
"${compose[@]}" --profile app --profile polling up -d --wait --wait-timeout 180
curl --fail --silent --show-error http://127.0.0.1:18080/health/ready
trap - ERR
