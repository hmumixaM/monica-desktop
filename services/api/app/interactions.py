"""Shared batch projection for likes/comments: identical for single and bulk posts."""
import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import text

from .content import canonical_json


def project_interactions(db,records,provider):
    rows={}
    for record in records:
        for index,interaction in enumerate(record.content.get('interactions') or []):
            if not isinstance(interaction,dict):continue
            kind=str(interaction.get('interaction_type') or 'comment')
            if kind not in {'like','comment','reply','mention'}:kind='comment'
            author=str(interaction.get('author_external_id') or interaction.get('username') or '') or None
            occurred=interaction.get('occurred_at')
            try:
                if isinstance(occurred,(int,float)) or (isinstance(occurred,str) and occurred.isdigit()):occurred=datetime.fromtimestamp(float(occurred),UTC)
                elif isinstance(occurred,str):occurred=datetime.fromisoformat(occurred)
            except (ValueError,OverflowError,OSError):occurred=None
            stable=str(interaction.get('external_id') or f"{record.external_id}:{kind}:{author or 'unknown'}:{interaction.get('create_time') or occurred or index}")
            body=interaction.get('text') or interaction.get('content')
            body=body if isinstance(body,dict) else {'text':body} if body else None
            encoded=canonical_json(body) if body is not None else None
            rows[(kind,stable)]={'post':record.external_id,'kind':kind,'external':stable,'author':author,
                'name':interaction.get('author_name') or interaction.get('nickname'),
                'occurred':occurred.isoformat() if occurred else None,'metadata':interaction,
                'digest':hashlib.sha256(canonical_json(interaction)).hexdigest(),'body':body,
                'body_hash':hashlib.sha256(encoded).hexdigest() if encoded else None,'size':len(encoded) if encoded else 0}
    if not rows:return
    db.execute(text("""CREATE TEMP TABLE interaction_input ON COMMIT DROP AS
        SELECT * FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(post text,kind text,external text,author text,
        name text,occurred timestamptz,metadata jsonb,digest char(64),body jsonb,body_hash char(64),size bigint)"""),{'rows':json.dumps(list(rows.values()),ensure_ascii=False)})
    db.execute(text("""INSERT INTO content_objects(sha256,content_kind,byte_length,payload_json)
        SELECT DISTINCT ON(body_hash) body_hash,'social_interaction',size,body FROM interaction_input WHERE body_hash IS NOT NULL
        ON CONFLICT(content_kind,sha256) DO NOTHING"""))
    # The corrected WeChat parser can identify a previously plain comment as
    # a reply. Keep that source comment's row and do not count it twice. Its
    # previous payload remains in the parent post's immutable revision.
    if provider == 'wechat':
        db.execute(text("""UPDATE social_interactions s SET interaction_type=r.kind
            FROM interaction_input r WHERE s.provider=:provider AND s.external_id=r.external
            AND s.interaction_type IN ('comment','reply') AND r.kind IN ('comment','reply')
            AND s.interaction_type<>r.kind"""), {'provider':provider})
    db.execute(text("""INSERT INTO social_interactions(provider,post_id,interaction_type,external_id,author_profile_id,
        author_external_id,author_name,content_object_id,occurred_at,metadata,current_content_hash)
        SELECT :provider,p.id,r.kind,r.external,i.profile_id,r.author,r.name,c.id,r.occurred,r.metadata,r.digest
        FROM interaction_input r JOIN social_posts p ON p.provider=:provider AND p.external_id=r.post
        LEFT JOIN identities i ON i.provider=:provider AND i.external_id=r.author AND i.status NOT IN ('invalid','merged')
        LEFT JOIN content_objects c ON c.content_kind='social_interaction' AND c.sha256=r.body_hash
        ON CONFLICT(provider,interaction_type,external_id) DO UPDATE SET post_id=EXCLUDED.post_id,
          author_profile_id=COALESCE(EXCLUDED.author_profile_id,social_interactions.author_profile_id),
          author_name=COALESCE(EXCLUDED.author_name,social_interactions.author_name),
          content_object_id=COALESCE(EXCLUDED.content_object_id,social_interactions.content_object_id),
          occurred_at=COALESCE(EXCLUDED.occurred_at,social_interactions.occurred_at),
          metadata=EXCLUDED.metadata,current_content_hash=EXCLUDED.current_content_hash"""),{'provider':provider})
    db.execute(text('DROP TABLE interaction_input'))
