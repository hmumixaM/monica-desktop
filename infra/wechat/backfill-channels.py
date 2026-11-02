"""Run against deployed API helpers, with reviewed parser/repair modules staged beside it."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT=Path('/data/staging/wechat-media-backfill-20260920')
for name in ('moments_xml','repair_moments_channels'):
    spec=importlib.util.spec_from_file_location('app.'+name,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
from app.db import SessionLocal
from app.repair_moments_channels import repair

with SessionLocal() as db:
    report=repair(db,'/data/staging/media-snapshot-20260919/sns.db',apply=True)
(ROOT/'channels-backfill.json').write_text(json.dumps(report))
print(json.dumps({'channels_backfill_complete':report}),flush=True)
