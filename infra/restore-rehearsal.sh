#!/usr/bin/env bash
# Restore only into a fresh disposable container with no network or host mounts.
set -euo pipefail
backup=${1:?Pass a complete backup directory}
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
python3 "$script_dir/verify-backup.py" "$backup"
report_dir=$(mktemp -d)
name="monica-restore-$(date +%s)-$$"
docker_bin=${DOCKER_BINARY:-docker}
"$docker_bin" run -d --name "$name" --network none -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine >/dev/null
trap '"$docker_bin" rm -f "$name" >/dev/null; rm -rf "$report_dir"' EXIT
ready=false
for _ in {1..30}; do
  if "$docker_bin" exec "$name" pg_isready -U postgres >/dev/null; then ready=true; break; fi
  sleep 1
done
$ready
for dump in "$backup"/*.dump; do
  db=$(basename "$dump" .dump)
  [[ "$db" =~ ^[a-zA-Z0-9_]+$ ]]
  "$docker_bin" exec "$name" createdb -U postgres "$db"
  "$docker_bin" exec -i "$name" pg_restore -U postgres --exit-on-error --no-owner --no-privileges -d "$db" < "$dump"
done
crm=${POSTGRES_DB:-crm}
[[ "$crm" =~ ^[a-zA-Z0-9_]+$ ]]
"$docker_bin" exec "$name" psql -U postgres -d "$crm" -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS dblink'
for provider in wechat instagram linkedin; do
  "$docker_bin" exec -i "$name" psql -U postgres -d "$crm" -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
 IF EXISTS (
  SELECT 1 FROM source_accounts a LEFT JOIN
   dblink('dbname=${crm}_${provider} user=postgres','SELECT id FROM accounts') AS remote(id uuid) ON remote.id=a.id
  WHERE a.provider='${provider}' AND remote.id IS NULL
 ) THEN RAISE EXCEPTION 'Missing ${provider} source accounts'; END IF;
 IF EXISTS (
  SELECT 1 FROM dblink('dbname=${crm}_${provider} user=postgres',
   'SELECT sha256,crm_content_kind FROM payloads WHERE value IS NULL') AS remote(hash text,kind text)
  WHERE NOT EXISTS(SELECT 1 FROM content_objects c WHERE c.sha256=remote.hash AND c.content_kind=remote.kind)
 ) THEN RAISE EXCEPTION 'Missing ${provider} CRM payload references'; END IF;
 IF EXISTS (
  SELECT 1 FROM social_posts p LEFT JOIN dblink('dbname=${crm}_${provider} user=postgres',
   'SELECT account_id,external_id FROM objects WHERE object_kind=''post''') AS remote(account uuid,external text)
   ON remote.account=p.source_account_id AND remote.external=p.external_id
  WHERE p.provider='${provider}' AND p.source_account_id IS NOT NULL AND remote.account IS NULL
 ) THEN RAISE EXCEPTION 'Missing ${provider} source posts'; END IF;
END \$\$;
SQL
done
"$docker_bin" exec "$name" psql -U postgres -d "$crm" -c 'COPY (SELECT sha256,object_path FROM media_assets) TO STDOUT WITH CSV' > "$report_dir/media.csv"
python3 "$script_dir/verify-backup.py" "$backup" --media-csv "$report_dir/media.csv"
printf '%s\n' 'Isolated restore verified: databases, source account/post/payload references, and referenced media bytes.'
