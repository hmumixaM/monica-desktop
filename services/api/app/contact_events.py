"""Source-owned call and money records, separate from handwritten activities."""
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from .content import canonical_json
from .db import SessionLocal
from .platform_migrate import account_lookup
from .platform_store import ensure_account, object_id, observe_many, platform_engine

CALL_FIELDS=('media_type','media_type_confidence','direction','status','status_label','duration_seconds','started_at','ended_at','initiator','is_group')
MONEY_FIELDS=('kind','direction','direction_source','amount_minor','currency','status','status_label','settled_at','counterparty','memo','is_group')


def import_archive(db,path):
    archive=json.loads(path.read_text());_,aliases,_=account_lookup(db,'wechat');reports={}
    for table,kind,fields in [('call_interactions','call',CALL_FIELDS),('money_interactions','money',MONEY_FIELDS)]:
        rows=archive[table];changed=0;unresolved=[]
        for start in range(0,len(rows),100):
            records=[];updates=[]
            with platform_engine('wechat').begin() as source:
                for row in rows[start:start+100]:
                    external=row.get('session_name') if kind=='call' else row.get('context_id')
                    if not external or row['provider']!='wechat':unresolved.append(row['id']);continue
                    aid=aliases.get(external)
                    if not aid:
                        account_kind='group' if external.endswith('@chatroom') else 'person'
                        aid=ensure_account(source,'wechat',external,kind=account_kind);aliases[external]=str(aid)
                        db.execute(text("""INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name)
                            VALUES(:id,'wechat',:external,:kind,:external) ON CONFLICT DO NOTHING"""),{'id':aid,'external':external,'kind':account_kind})
                    event_id=str(row['external_id'] or row['id']);stamp=datetime.fromisoformat(row['snapshot_at']).replace(tzinfo=UTC)
                    value={k:v for k,v in row.items() if k not in ('created_at','updated_at','snapshot_at')}
                    records.append({'account':aid,'kind':kind,'external_id':event_id,'value':value,
                        'source_event':f'monica:{table}:{row["id"]}:{row.get("updated_at") or row["snapshot_at"]}',
                        'observed_at':stamp,'occurred_at':datetime.fromtimestamp(row['occurred_at'],UTC) if row.get('occurred_at') else None})
                    content={k:row.get(k) for k in fields}
                    updates.append({'account':aid,'kind':kind,'external':event_id,'at':row.get('occurred_at'),
                        'snapshot':stamp,'content':canonical_json(content).decode()})
                ids=[object_id(r['account'],r['kind'],r['external_id']) for r in records]
                before=dict(source.execute(text('SELECT id,current_hash FROM objects WHERE id=ANY(:ids)'),{'ids':ids}).all()) if ids else {}
                states=observe_many(source,records);changed+=sum(before.get(r['object_id'])!=r['hash'] for r in states)
            if updates:
                db.execute(text("""INSERT INTO source_contact_events(account_id,event_kind,external_id,occurred_at,snapshot_at,content)
                    VALUES(:account,:kind,:external,to_timestamp(:at),:snapshot,CAST(:content AS jsonb))
                    ON CONFLICT(account_id,event_kind,external_id) DO UPDATE SET occurred_at=EXCLUDED.occurred_at,
                        snapshot_at=EXCLUDED.snapshot_at,content=EXCLUDED.content
                    WHERE (source_contact_events.occurred_at,source_contact_events.snapshot_at,source_contact_events.content)
                        IS DISTINCT FROM (EXCLUDED.occurred_at,EXCLUDED.snapshot_at,EXCLUDED.content)"""),updates)
            db.commit()
        reports[kind]={'source_rows':len(rows),'projected_rows':len(rows)-len(unresolved),'changed_source_objects':changed,'unresolved_ids':unresolved}
    return reports


def summary(db,profile_id):
    rows=db.execute(text("""SELECT e.event_kind,e.content,e.snapshot_at FROM source_contact_events e
        JOIN source_account_links l ON l.account_id=e.account_id WHERE l.profile_id=:profile"""),{'profile':profile_id}).mappings()
    calls={'total':0,'voice':0,'video':0,'completed':0,'incoming':0,'outgoing':0,'duration_seconds':0,'known_duration':0,'inferred_type':0}
    money={'total':0,'transfer':0,'red_packet':0,'unknown_amount':0,'unknown_direction':0,'currencies':{}}
    snapshots=[]
    for row in rows:
        c=row['content'];snapshots.append(row['snapshot_at'])
        if row['event_kind']=='call':
            calls['total']+=1
            if c['media_type'] in ('voice','video'):calls[c['media_type']]+=1
            calls['completed']+=c['status']=='completed'
            calls['incoming']+=c['direction']=='in';calls['outgoing']+=c['direction']=='out'
            calls['inferred_type']+=c['media_type_confidence']!='high'
            if c['duration_seconds'] is not None:calls['known_duration']+=1;calls['duration_seconds']+=c['duration_seconds']
        else:
            money['total']+=1
            if c['kind'] in ('transfer','red_packet'):money[c['kind']]+=1
            money['unknown_amount']+=c['amount_minor'] is None
            money['unknown_direction']+=c['direction'] not in ('in','out')
            # Show observed amounts separately by currency and direction; no
            # debt balance is inferred, and unknown amounts are never zeroed.
            if c['amount_minor'] is not None and c['currency'] and c['direction'] in ('in','out'):
                amounts=money['currencies'].setdefault(c['currency'],{'in':0,'out':0})
                amounts[c['direction']]+=c['amount_minor']
    return {'calls':calls,'money':money,'snapshot_at':min(snapshots) if snapshots else None}


def records(db,profile_id,kind,offset=0,limit=30):
    return [dict(r) for r in db.execute(text("""SELECT e.account_id,e.event_kind,e.external_id,e.occurred_at,e.content
        FROM source_contact_events e JOIN source_account_links l ON l.account_id=e.account_id
        WHERE l.profile_id=:profile AND e.event_kind=:kind ORDER BY e.occurred_at DESC NULLS LAST,e.account_id,e.external_id
        LIMIT :limit OFFSET :offset"""),{'profile':profile_id,'kind':kind,'limit':min(max(limit,1),100),'offset':max(offset,0)}).mappings()]


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--archive',type=Path,required=True);args=parser.parse_args()
    with SessionLocal() as db:print(json.dumps(import_archive(db,args.archive)))


if __name__=='__main__':main()
