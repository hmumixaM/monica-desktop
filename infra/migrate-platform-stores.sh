#!/bin/sh
# Run inside the exact deployed API image. Copies existing objects in bounded
# transactions; never invokes the legacy full importer or overwrites a Profile.
set -eu
export PATH="/app/.venv/bin:$PATH"
provider=${1:?Usage: migrate-platform-stores.sh wechat|instagram|linkedin}
case "$provider" in wechat|instagram|linkedin) ;; *) exit 2;; esac
python -m app.platform_store initialize --provider "$provider"
python -m app.platform_migrate accounts --provider "$provider"
python -m app.platform_migrate fields --provider "$provider"
python -m app.platform_migrate facts --provider "$provider"
case "$provider" in
  instagram) python -m app.platform_migrate posts --provider instagram --archive /data/staging/instagram-current/profiles ;;
  linkedin) python -m app.platform_migrate posts --provider linkedin --archive /data/raw/linkedin/linkedin_enrichment.incoming.json ;;
  wechat) python -m app.platform_migrate posts --provider wechat ;;
esac
if [ "$provider" = wechat ]; then
  python -m app.platform_migrate pointers --provider wechat --archive /data/staging/import-source/covers/manifest.json
else
  python -m app.platform_migrate pointers --provider "$provider"
fi
python -m app.platform_migrate notebook --provider "$provider"
if [ "$provider" = wechat ]; then
  python -m app.platform_migrate interactions --provider wechat --archive /data/raw/monica/moments-graph.json
  python -m app.platform_migrate tags --provider wechat --archive /data/staging/moments-tags-current.json
else
  python -m app.platform_migrate interactions --provider "$provider"
fi
if [ "$provider" = wechat ]; then
  python -m app.platform_migrate groups --provider wechat
  python -m app.platform_migrate messages --provider wechat
fi
python -m app.platform_migrate verify --provider "$provider" --activate
