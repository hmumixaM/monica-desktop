"""Build current dashboard metadata without replaying source imports."""
import json

from sqlalchemy import text

from .db import SessionLocal


def rebuild(db,batch_size=500):
    count=0
    while True:
        added=db.execute(text("""WITH selected AS MATERIALIZED (
            SELECT c.profile_id,c.field_key,c.current_revision_id FROM profile_field_current c
            LEFT JOIN profile_metric_values m ON m.profile_id=c.profile_id AND m.field_key=c.field_key
            WHERE is_profile_metric_field(c.field_key) AND (m.revision_id IS NULL OR m.revision_id<>c.current_revision_id)
            ORDER BY c.profile_id,c.field_key LIMIT :limit FOR UPDATE OF c
          ) INSERT INTO profile_metric_values(profile_id,field_key,revision_id,value)
            SELECT c.profile_id,c.field_key,c.current_revision_id,o.payload_json FROM selected c
            JOIN profile_field_revisions r ON r.id=c.current_revision_id JOIN content_objects o ON o.id=r.content_object_id
            ON CONFLICT(profile_id,field_key) DO UPDATE SET revision_id=EXCLUDED.revision_id,value=EXCLUDED.value
            WHERE profile_metric_values.revision_id IS DISTINCT FROM EXCLUDED.revision_id"""),{'limit':batch_size}).rowcount
        db.commit();count+=added
        if not added:return count
        print(json.dumps({'dashboard_metadata_indexed':count}),flush=True)


if __name__=='__main__':
    with SessionLocal() as db:print(json.dumps({'changed_dashboard_indexes':rebuild(db)}),flush=True)
