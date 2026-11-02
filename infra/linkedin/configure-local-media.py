"""Restore the CRM export hook without changing collector budgets or scheduling."""
import argparse
from datetime import datetime
from pathlib import Path


def patch(path, old, new):
    source=path.read_text()
    if new in source:return
    if old not in source:raise ValueError('Collector layout changed: '+path.name)
    backup=path.with_name(path.name+'.before-local-media-'+datetime.now().strftime('%Y%m%d%H%M%S'))
    backup.write_text(source);path.write_text(source.replace(old,new,1))


def configure(root):
    src=root/'src/linkedin_enrichment'
    patch(src/'storage.py','        snapshot_hash: str | None = None,\n    ) -> dict[str, Any]:','        snapshot_hash: str | None = None,\n        include_unlinked: bool = False,\n    ) -> dict[str, Any]:')
    patch(src/'storage.py','                if not isinstance(contact_id, int) or not provider_user_id:','                if not provider_user_id or (not include_unlinked and not isinstance(contact_id, int)):')
    patch(src/'cli.py','    export.add_argument("--snapshot-hash")','    export.add_argument("--snapshot-hash")\n    export.add_argument("--include-unlinked", action="store_true")')
    patch(src/'cli.py','                snapshot_hash=args.snapshot_hash,','                snapshot_hash=args.snapshot_hash,\n                include_unlinked=args.include_unlinked,')
    hook='''# Publish local bytes before the complete CRM export, including unlinked accounts.
crm_payload="${PAYLOAD%.json}.crm.json"
"$UV" run --no-sync linkedin-enrichment --db "$DB" export "$crm_payload" --include-unlinked
python3 "$ROOT/scripts/publish-local-media.py" --payload "$crm_payload" --db "$DB" --assets "$ASSET_DIR"
'''
    script=root/'scripts/sync-host.sh'
    if hook not in script.read_text():patch(script,'chmod 600 "$PAYLOAD"','chmod 600 "$PAYLOAD"\n\n'+hook)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('root',type=Path)
    configure(parser.parse_args().root)
