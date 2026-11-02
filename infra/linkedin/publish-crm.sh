#!/usr/bin/env bash
set -euo pipefail
umask 077
payload=${1:?Pass the LinkedIn export path}
host=${CRM_NAS_HOST:-linkedin-nas}
destination=${CRM_LINKEDIN_PATH:-/volume1/docker/monica-next/data/raw/linkedin/linkedin_enrichment.incoming.json}
# All publishers use the same immutable snapshot contract.
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
python3 - "$script_dir/.." "$host" "$destination" "$payload" <<'PYTHON'
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from snapshot_publish import publish
path=Path(sys.argv[3])
publish(sys.argv[2],str(path.parent),[(Path(sys.argv[4]),path.name)])
PYTHON
