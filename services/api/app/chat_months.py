"""Account-owned monthly counts; Profile requests never read message bodies."""
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from .db import SessionLocal
from .platform_migrate import account_lookup
from .platform_store import ensure_account, object_id, observe_many, platform_engine


def import_archive(db,path):
    archive=json.loads(path.read_text());rows=archive['rows']
    _,aliases,_=account_lookup(db,'wechat');count=0;changed=0
    for start in range(0,len(rows),100):
        records=[];updates=[]
        with platform_engine('wechat').begin() as source:
            for row in rows[start:start+100]:
                external=row['wechat_username'];aid=aliases.get(external)
                if not aid:
                    aid=ensure_account(source,'wechat',external)
                    aliases[external]=str(aid)
                    db.execute(text("""INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name)
                        VALUES(:id,'wechat',:external,'person',:external) ON CONFLICT DO NOTHING"""),{'id':aid,'external':external})
                value={k:row[k] for k in ('month','sent','received','total','first_message_at','last_message_at')}
                records.append({'account':aid,'kind':'chat_month','external_id':row['month'],'value':value,
                    'source_event':'monica-chat-month:'+str(row['id'])+':'+row['snapshot_at'],
                    'observed_at':datetime.fromisoformat(row['snapshot_at']).replace(tzinfo=UTC)})
                updates.append({'account':aid,'month':row['month']+'-01','sent':int(row['sent']),'received':int(row['received']),
                    'total':int(row['total']),'first':row['first_message_at'],'last':row['last_message_at'],
                    'snapshot':row['snapshot_at'],'timezone':archive.get('timezone') or 'UTC'})
            ids=[object_id(r['account'],r['kind'],r['external_id']) for r in records]
            before=dict(source.execute(text('SELECT id,current_hash FROM objects WHERE id=ANY(:ids)'),{'ids':ids}).all())
            states=observe_many(source,records)
            changed+=sum(before.get(r['object_id'])!=r['hash'] for r in states)
        db.execute(text("""INSERT INTO source_chat_months(account_id,month,sent,received,total,first_message_at,last_message_at,snapshot_at,timezone)
            VALUES(:account,CAST(:month AS date),:sent,:received,:total,to_timestamp(:first),to_timestamp(:last),CAST(:snapshot AS timestamptz),:timezone)
            ON CONFLICT(account_id,month) DO UPDATE SET sent=EXCLUDED.sent,received=EXCLUDED.received,total=EXCLUDED.total,
            first_message_at=EXCLUDED.first_message_at,last_message_at=EXCLUDED.last_message_at,snapshot_at=EXCLUDED.snapshot_at,timezone=EXCLUDED.timezone
            WHERE (source_chat_months.sent,source_chat_months.received,source_chat_months.total,source_chat_months.first_message_at,
                   source_chat_months.last_message_at,source_chat_months.snapshot_at,source_chat_months.timezone)
              IS DISTINCT FROM (EXCLUDED.sent,EXCLUDED.received,EXCLUDED.total,EXCLUDED.first_message_at,
                                EXCLUDED.last_message_at,EXCLUDED.snapshot_at,EXCLUDED.timezone)"""),updates)
        db.commit();count+=len(updates)
    return {'source_rows':len(rows),'projected_rows':count,'changed_source_objects':changed}


def profile_months(db,profile_id):
    rows=[dict(r) for r in db.execute(text("""SELECT to_char(m.month,'YYYY-MM') AS month,sum(sent) sent,sum(received) received,
        sum(total) total,min(first_message_at) first,max(last_message_at) last,min(snapshot_at) snapshot_at,min(timezone) timezone
        FROM source_chat_months m JOIN source_account_links l ON l.account_id=m.account_id
        WHERE l.profile_id=:profile GROUP BY m.month ORDER BY m.month"""),{'profile':profile_id}).mappings()]
    total=sum(r['total'] for r in rows)
    return {'months':rows,'total':total,'sent':sum(r['sent'] for r in rows),'received':sum(r['received'] for r in rows),
        'first':min((r['first'] for r in rows if r['first']),default=None),'last':max((r['last'] for r in rows if r['last']),default=None),
        'snapshot_at':min((r['snapshot_at'] for r in rows),default=None),'peak':max(rows,key=lambda r:r['total'],default=None),
        'timezone':rows[0]['timezone'] if rows else 'UTC','active_months':sum(r['total']>0 for r in rows)}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--archive',type=Path,required=True);args=parser.parse_args()
    with SessionLocal() as db:print(json.dumps(import_archive(db,args.archive)))


if __name__=='__main__':main()
