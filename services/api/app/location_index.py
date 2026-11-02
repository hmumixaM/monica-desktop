"""Build missing map metadata in bounded batches without modifying sources."""
import json

from sqlalchemy import text

from .db import SessionLocal


def rebuild(db,batch_size=1000):
    count=0
    while True:
        added=db.execute(text("""WITH selected AS MATERIALIZED (
            SELECT p.id,p.content_object_id,p.provider FROM social_posts p
            LEFT JOIN social_post_locations l ON l.post_id=p.id
            WHERE l.post_id IS NULL OR l.content_object_id<>p.content_object_id
            ORDER BY p.id LIMIT :limit FOR UPDATE OF p
          ) INSERT INTO social_post_locations(post_id,content_object_id,location,coordinate_format)
            SELECT p.id,p.content_object_id,c.payload_json->'location',
              CASE WHEN p.provider='wechat' AND c.payload_json ? 'raw_xml_sha256' THEN 'chatlog_sns_xml' END
            FROM selected p JOIN content_objects c ON c.id=p.content_object_id
            ON CONFLICT(post_id) DO UPDATE SET content_object_id=EXCLUDED.content_object_id,
              location=EXCLUDED.location,coordinate_format=EXCLUDED.coordinate_format
            WHERE social_post_locations.content_object_id IS DISTINCT FROM EXCLUDED.content_object_id"""),{'limit':batch_size}).rowcount
        db.commit();count+=added
        if not added:return count
        print(json.dumps({'location_metadata_indexed':count}),flush=True)


if __name__=='__main__':
    with SessionLocal() as db:print(json.dumps({'changed_location_indexes':rebuild(db)}),flush=True)
