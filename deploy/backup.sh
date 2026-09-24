#!/usr/bin/env bash
# Consistent application snapshot. Stops only this project's running services.
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
compose=(docker compose -f compose.production.yml)
backup_root=../backups
mkdir -p "$backup_root"
exec 9>"$backup_root/.backup.lock"
flock -n 9 || { echo 'A backup is already running.' >&2; exit 1; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
destination="$backup_root/$stamp.partial"
mkdir "$destination"
running=()
while IFS= read -r service; do
  case "$service" in api|worker|scheduler|polling) running+=("$service");; esac
done < <("${compose[@]}" ps --status running --services)
resume() {
  if ((${#running[@]})); then "${compose[@]}" start "${running[@]}"; fi
}
trap resume EXIT
if ((${#running[@]})); then "${compose[@]}" stop -t 120 "${running[@]}"; fi
"${compose[@]}" exec -T postgres pg_dump -U fintracker_owner -d fintracker -Fc \
  > "$destination/database.dump"
api_container=$("${compose[@]}" ps -a -q api)
api_image=$(docker inspect --format '{{.Image}}' "$api_container")
# A Compose one-off inherits the application's healthcheck, which writes probes
# into the very directories being archived. Snapshot with read-only volumes and
# no healthcheck, network, runtime environment or extra privileges instead.
docker run --rm --network none --no-healthcheck --cap-drop ALL \
  --security-opt no-new-privileges:true --volumes-from "$api_container:ro" \
  --entrypoint tar "$api_image" \
  -C /var/lib/fintracker -czf - objects security-log > "$destination/files.tar.gz"
cp -a ../env "$destination/env"
cp compose.production.yml migrate.py "$destination/"
printf '%s\n' "$api_image" > "$destination/image-id.txt"
if [[ -f .env ]]; then cp .env "$destination/compose.env"; fi
"${compose[@]}" exec -T postgres pg_restore --list < "$destination/database.dump" >/dev/null
tar -tzf "$destination/files.tar.gz" >/dev/null
(cd "$destination" && sha256sum database.dump files.tar.gz > SHA256SUMS)
mv "$destination" "$backup_root/$stamp"
echo "Snapshot saved to $backup_root/$stamp. Off-server replication is not configured."
