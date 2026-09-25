#!/usr/bin/env bash
# Upload the committed release, check dependencies, then switch the polling process.
set -euo pipefail

if [[ $# != 1 ]]; then
  echo "Usage: bash deploy/release.sh SSH_TARGET" >&2
  exit 2
fi
remote_target=$1
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
git diff --quiet
git diff --cached --quiet
release_sha=$(git rev-parse HEAD)
remote_release="/opt/fintracker/releases/sheets-$release_sha"
ssh "$remote_target" "mkdir -p '$remote_release'"
git archive HEAD | ssh "$remote_target" "tar -x -C '$remote_release'"
ssh "$remote_target" bash -s -- "$release_sha" <<'REMOTE'
set -euo pipefail
release_sha=$1
base=/opt/fintracker
release="$base/releases/sheets-$release_sha"
export FINTRACKER_SHEETS_IMAGE="fintracker:sheets-$release_sha"
new_compose=(docker compose -f "$release/deploy/compose.sheets.yml")
legacy_compose=(docker compose --project-directory "$base/deploy" -f "$base/deploy/compose.production.yml")

if [[ ! -s "$base/env/sheets.env" ]]; then
  echo "Missing /opt/fintracker/env/sheets.env; see deploy/README.md. Existing bot unchanged." >&2
  exit 3
fi
"${new_compose[@]}" config --quiet
docker build --label "org.opencontainers.image.revision=$release_sha" \
  -t "$FINTRACKER_SHEETS_IMAGE" "$release"
# Does not receive updates, register commands or write expenses.
# Do not let Compose consume the remaining SSH heredoc as container input.
"${new_compose[@]}" run --rm -T --no-deps bot check </dev/null
"${new_compose[@]}" run --rm -T --no-deps --entrypoint python bot - <<'PY'
import asyncio
from aiogram import Bot
from fintracker.sheetbot.config import BotSettings
async def check():
    bot = Bot(BotSettings().telegram.bot_token.get_secret_value())
    try:
        await bot.get_me()
        info = await bot.get_webhook_info()
        if info.url:
            raise RuntimeError("Webhook is enabled. Disable it before polling cutover.")
    finally:
        await bot.session.close()
asyncio.run(check())
PY

mkdir -p "$base/deploy-sheets"
previous_image=''
if [[ -f "$base/deploy-sheets/image.env" ]]; then
  previous_image=$(sed -n 's/^FINTRACKER_SHEETS_IMAGE=//p' "$base/deploy-sheets/image.env")
fi
legacy_running=()
if [[ -f "$base/deploy/compose.production.yml" ]]; then
  while IFS= read -r service; do
    case "$service" in api|worker|scheduler|polling) legacy_running+=("$service");; esac
  done < <("${legacy_compose[@]}" ps --services --status running)
fi
rollback() {
  status=$?
  trap - ERR
  "${new_compose[@]}" stop bot || true
  if [[ -n "$previous_image" ]]; then
    export FINTRACKER_SHEETS_IMAGE="$previous_image"
    "${new_compose[@]}" up -d bot || true
  elif ((${#legacy_running[@]})); then
    "${legacy_compose[@]}" start "${legacy_running[@]}" || true
  fi
  echo "Deployment failed; previous processes restored where available." >&2
  exit "$status"
}
trap rollback ERR
if ((${#legacy_running[@]})); then
  "${legacy_compose[@]}" stop "${legacy_running[@]}"
fi
"${new_compose[@]}" up -d bot
container_id=$("${new_compose[@]}" ps -q bot)
test -n "$container_id"
# Detect immediate startup failures and restart loops before reporting success.
for attempt in {1..6}; do
  sleep 5
  test "$(docker inspect --format '{{.State.Running}}' "$container_id")" = true
  test "$(docker inspect --format '{{.RestartCount}}' "$container_id")" = 0
done
printf 'FINTRACKER_SHEETS_IMAGE=%s\n' "$FINTRACKER_SHEETS_IMAGE" > "$base/deploy-sheets/image.env"
cp "$release/deploy/compose.sheets.yml" "$base/deploy-sheets/compose.sheets.yml"
ln -sfn "releases/sheets-$release_sha" "$base/sheets-source"
trap - ERR
printf 'Deployed commit %s\n' "$release_sha"
"${new_compose[@]}" ps
REMOTE
