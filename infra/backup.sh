#!/usr/bin/env sh
# Complete backup set. A COMPLETE marker is written only after every archive succeeds.
set -eu
umask 077
DATA_ROOT=${DATA_ROOT:-./volumes/data}
BACKUP_ROOT=${BACKUP_ROOT:-$DATA_ROOT/backups}
COMPOSE_FILE=${COMPOSE_FILE:-infra/docker-compose.yml}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
SET="$BACKUP_ROOT/$STAMP"
mkdir -p "$SET"
compose() {
  if [ -n "${COMPOSE_BINARY:-}" ]; then "$COMPOSE_BINARY" -f "$COMPOSE_FILE" "$@"
  else "${DOCKER_BINARY:-docker}" compose -f "$COMPOSE_FILE" "$@"; fi
}
for database in "${POSTGRES_DB:-crm}" "${POSTGRES_DB:-crm}_wechat" "${POSTGRES_DB:-crm}_instagram" "${POSTGRES_DB:-crm}_linkedin"; do
  compose exec -T postgres pg_dump --format=custom --no-owner --no-privileges \
    -U "${POSTGRES_USER:-crm}" -d "$database" > "$SET/$database.dump.tmp"
  mv "$SET/$database.dump.tmp" "$SET/$database.dump"
done
# Immutable source stores are dumped before final recovery verification. Restore
# checks must confirm all CRM source references exist; these are not atomic
# cross-database snapshots while imports are writing.
tar -czf "$SET/raw-staging-exports.tar.gz.tmp" -C "$DATA_ROOT" raw staging exports
mv "$SET/raw-staging-exports.tar.gz.tmp" "$SET/raw-staging-exports.tar.gz"
if [ -n "${MEDIA_SNAPSHOT_REFERENCE:-}" ]; then
  printf '%s\n' "$MEDIA_SNAPSHOT_REFERENCE" > "$SET/media-snapshot-reference.txt"
else
  tar -cf "$SET/media.tar.tmp" -C "$DATA_ROOT" media
  mv "$SET/media.tar.tmp" "$SET/media.tar"
fi
config_dir=$(dirname "$COMPOSE_FILE")
# Include live routing, release state, recovery journal and publisher/deploy tools.
tar -czf "$SET/deployment-config.tar.gz.tmp" --exclude='./volumes' --exclude='./.git' -C "$config_dir" .
mv "$SET/deployment-config.tar.gz.tmp" "$SET/deployment-config.tar.gz"
python3 - "$SET" <<'PY'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]); files={}
for path in sorted(root.iterdir()):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
 files[path.name]={'sha256':h.hexdigest(),'bytes':path.stat().st_size}
(root/'manifest.json').write_text(json.dumps({'version':1,'files':files},indent=2)+'\n')
(root/'COMPLETE').write_text('Restore rehearsal required; media snapshot references need separate verification.\n')
PY
printf 'Created backup set %s\n' "$SET"
