"""Create independent CRM people for already archived, unlinked LinkedIn accounts."""
import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import text

from app.account_links import set_link
from app.db import SessionLocal
from app.platform_migrate import account_lookup
from app.profile_presentation import identity_key
from app.source_projection import enabled

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--input', type=Path, required=True)
parser.add_argument('--report', type=Path, required=True)
parser.add_argument('--apply', action='store_true')
args = parser.parse_args()
raw = args.input.read_bytes()
rows = json.loads(raw)['profiles']
report = {'sha256':hashlib.sha256(raw).hexdigest(), 'source_rows':len(rows), 'unique_accounts':0,
          'already_linked':0, 'created_profiles':0, 'missing_accounts':[], 'conflicts':[], 'apply':args.apply}
with SessionLocal() as db:
    if not enabled(db,'linkedin'):
        raise RuntimeError('LinkedIn independent source store must be enabled')
    accounts, aliases, _ = account_lookup(db,'linkedin')
    links = {str(r.account_id):str(r.profile_id) for r in db.execute(text('SELECT account_id,profile_id FROM source_account_links'))}
    report['profiles_before'] = db.execute(text('SELECT count(*) FROM profiles')).scalar_one()
    seen = set()
    db.commit()
    for row in rows:
        external = str(row.get('provider_user_id') or row.get('profile_url') or '')
        key = identity_key('linkedin', external)
        aid = aliases.get(external) or aliases.get(key)
        if not aid:
            report['missing_accounts'].append(external)
            continue
        if aid in seen:
            continue
        seen.add(aid)
        if aid in links:
            report['already_linked'] += 1
            continue
        name = (row.get('profile') or {}).get('name') or accounts[aid]['display_name'] or external
        if args.apply:
            # Serialize with the existing account-link API; recheck under its lock.
            db.execute(text('SELECT pg_advisory_xact_lock(11803295)'))
            current = db.execute(text('SELECT profile_id FROM source_account_links WHERE account_id=:id'),{'id':aid}).scalar_one_or_none()
            if current:
                report['already_linked'] += 1
                db.commit()
                continue
            pid = db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person',:name) RETURNING id"),{'name':name}).scalar_one()
            set_link(db,aid,pid,expected_profile_id=None,evidence={
                'kind':'user-requested-separate-linkedin-profile','source_external_id':external,'snapshot_sha256':report['sha256']})
            db.commit()
        report['created_profiles'] += 1
        if report['created_profiles'] % 25 == 0:
            print(json.dumps({'created_profiles':report['created_profiles']}),flush=True)
    report['unique_accounts'] = len(seen)
    report['profiles_after'] = db.execute(text('SELECT count(*) FROM profiles')).scalar_one()
    report['posts_without_source'] = db.execute(text("SELECT count(*) FROM social_posts WHERE provider='linkedin' AND source_account_id IS NULL")).scalar_one()
args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False),flush=True)
