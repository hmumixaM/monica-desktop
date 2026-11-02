#!/usr/bin/env sh
set -eu

DATA_ROOT=${DATA_ROOT:-./volumes/data}
mkdir -p "$DATA_ROOT/raw/wechat" "$DATA_ROOT/raw/instagram" \
  "$DATA_ROOT/raw/linkedin" "$DATA_ROOT/raw/monica" "$DATA_ROOT/media" \
  "$DATA_ROOT/staging" "$DATA_ROOT/exports" "$DATA_ROOT/backups"
echo "Initialized Monica Next data directories under $DATA_ROOT"
