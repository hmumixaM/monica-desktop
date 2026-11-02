"""Repair only XML-derived search rows; complete immutable message bytes remain."""
import json

from sqlalchemy import text

from .db import SessionLocal
from .import_messages import search_text


def main():
    changed=0;cursor=None
    with SessionLocal() as db:
        while True:
            rows=db.execute(text("""SELECT message_id,occurred_at,body FROM message_search
                WHERE (body LIKE '<%' OR body ~ '^[^<\n]{1,100}:\n<')
                AND (CAST(:cursor AS uuid) IS NULL OR (message_id,occurred_at)>(CAST(:cursor AS uuid),CAST(:at AS timestamptz)))
                ORDER BY message_id,occurred_at LIMIT 2000"""),
                {'cursor':cursor[0] if cursor else None,'at':cursor[1] if cursor else None}).all()
            if not rows:break
            values=[{'id':str(r.message_id),'at':r.occurred_at.isoformat(),'body':search_text(r.body)} for r in rows if search_text(r.body)!=r.body]
            if values:
                db.execute(text("""UPDATE message_search s SET body=i.body
                    FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS i(id uuid,at timestamptz,body text)
                    WHERE s.message_id=i.id AND s.occurred_at=i.at"""),{'rows':json.dumps(values,ensure_ascii=False)})
            changed+=len(values);cursor=(rows[-1].message_id,rows[-1].occurred_at);db.commit()
            if changed and changed%10000==0:print(json.dumps({'compacted':changed}),flush=True)
        print(json.dumps({'xml_search_rows_compacted':changed}),flush=True)


if __name__=='__main__':main()
