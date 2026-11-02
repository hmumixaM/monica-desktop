"""Browse saved face annotations. This module never runs detection or matching."""

import base64
import json
import math
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..aggregate_cache import cached_aggregate
from ..auth import current_user
from ..db import get_db
from .review import REVIEWABLE

router = APIRouter(prefix="/api/v1/faces", dependencies=[Depends(current_user)])
DOMAINS = ('faces', 'media', 'posts', 'messages', 'links', 'profiles')

# Resolve only links belonging to the requested person's media. Unknown/missing
# sources remain browseable; known official-account sources are excluded.
SOURCES = """
  SELECT l.media_id, l.entity_type, l.entity_id, l.role,
         COALESCE(sp.provider, ss.provider, sa.provider, ca.provider) AS provider,
         COALESCE(sp.occurred_at, ss.occurred_at, me.occurred_at) AS occurred_at,
         COALESCE(author.display_name, sa.display_name, cp.display_name, ca.display_name) AS author,
         c.id AS conversation_id, c.conversation_type,
         COALESCE(cp.display_name, ca.display_name) AS conversation_name,
         COALESCE(sp.id, ss.id, me.id, p.id, sa.id) IS NOT NULL AS resolved,
         NOT (COALESCE(sa.provider = 'wechat' AND left(sa.external_id, 3) = 'gh_', false)
              OR COALESCE(ca.provider = 'wechat' AND left(ca.external_id, 3) = 'gh_', false)) AS allowed
  FROM (SELECT * FROM media_links WHERE media_id IN (SELECT media_id FROM matched) OFFSET 0) l
  LEFT JOIN LATERAL (SELECT * FROM social_posts WHERE l.entity_type = 'social_post' AND id = l.entity_id LIMIT 1) sp ON true
  LEFT JOIN LATERAL (SELECT * FROM social_stories WHERE l.entity_type = 'social_story' AND id = l.entity_id LIMIT 1) ss ON true
  LEFT JOIN LATERAL (SELECT * FROM message_events WHERE l.entity_type = 'message_event' AND id = l.entity_id LIMIT 1) me ON true
  LEFT JOIN conversations c ON c.id = me.conversation_id
  LEFT JOIN profiles cp ON cp.id = c.profile_id
  LEFT JOIN profiles p ON l.entity_type = 'profile' AND p.id = l.entity_id
  LEFT JOIN source_accounts sa ON sa.id = COALESCE(sp.source_account_id, ss.source_account_id,
       me.sender_account_id, CASE WHEN l.entity_type = 'source_account' THEN l.entity_id END, l.source_account_id)
  LEFT JOIN source_accounts ca ON ca.id = c.source_account_id
  LEFT JOIN profiles author ON author.id = COALESCE(sp.profile_id, ss.profile_id, me.sender_profile_id, p.id)

"""

BASE = """WITH matched AS MATERIALIZED (
  SELECT f.* FROM faces f
  WHERE ((f.status = 'confirmed' AND f.profile_id = :pid)
    OR (f.status = 'pending' AND f.suggested_profile_id = :pid
        AND f.rejected_profile_id IS DISTINCT FROM :pid AND f.quality >= :quality))
), sources AS MATERIALIZED (""" + SOURCES + """), source_stats AS (
  SELECT media_id, bool_or(allowed) AS allowed, max(occurred_at) FILTER (WHERE allowed) AS occurred_at
  FROM sources GROUP BY media_id
), photos AS (
  SELECT f.media_id, bool_or(f.status = 'pending') AS pending,
         bool_or(f.status = 'confirmed') AS confirmed, max(f.suggestion_score) AS score,
         COALESCE(s.occurred_at, a.capture_at, a.created_at) AS occurred_at,
         a.width, a.height
  FROM matched f JOIN media_assets a ON a.id = f.media_id
  LEFT JOIN source_stats s ON s.media_id = f.media_id
  WHERE a.media_type LIKE 'image/%' AND COALESCE(s.allowed, true)
  GROUP BY f.media_id, a.id, s.occurred_at
)
"""


def _params(profile_id):
    return {'pid': profile_id, 'quality': REVIEWABLE}


def _counts(db, profile_id):
    return dict(db.execute(text(BASE + """SELECT count(*) AS all,
        count(*) FILTER (WHERE pending) AS pending, count(*) FILTER (WHERE confirmed) AS confirmed,
        md5(COALESCE(string_agg(media_id::text || occurred_at::text || pending::text || confirmed::text
            || COALESCE(score::text, ''), ',' ORDER BY media_id), '')) AS revision
        FROM photos"""), _params(profile_id)).mappings().one())


def snapshot(db, profile_id):
    return cached_aggregate(db, 'profile-face-photo-counts', DOMAINS, str(profile_id),
                            lambda session: _counts(session, profile_id), source=_counts)


def counts(db, profile_id):
    return {k: v for k, v in snapshot(db, profile_id).items() if k != 'revision'}


@router.get('/people/{profile_id}/photos/status')
def status(profile_id: UUID, db: Session = Depends(get_db)):
    saved = snapshot(db, profile_id)
    return {'revision': saved['revision'], 'counts': {k: saved[k] for k in ('all', 'pending', 'confirmed')}}


def _cursor(value, profile_id, state, sort):
    try:
        data = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        if data['profile'] != str(profile_id) or data['state'] != state or data['sort'] != sort:
            raise ValueError()
        stamp = datetime.fromisoformat(data['time'])
        if stamp.tzinfo is None:
            raise ValueError()
        score = data['score']
        if score is not None and (not isinstance(score, (int, float)) or not math.isfinite(score)):
            raise ValueError()
        return {'after_id': UUID(data['id']), 'after_time': stamp, 'after_score': score}
    except (ValueError, TypeError, KeyError):
        raise HTTPException(422, 'Invalid photo cursor') from None


def _encode(row, profile_id, state, sort):
    return base64.urlsafe_b64encode(json.dumps({
        'profile': str(profile_id), 'state': state, 'sort': sort,
        'id': str(row['media_id']), 'time': row['occurred_at'].isoformat(), 'score': row['score'],
    }).encode()).decode().rstrip('=')


def _source(row):
    source = dict(row)
    kind, entity = row['entity_type'], row['entity_id']
    href = None
    if row['resolved']:
        if kind in ('social_post', 'social_story'):
            href = f'/timeline?event={entity}'
        elif kind == 'message_event' and row['conversation_id']:
            month = row['occurred_at'].strftime('%Y-%m')
            href = f'/messages?conversation={row["conversation_id"]}&month={month}&timezone=UTC&message={entity}'
        elif kind == 'profile':
            href = f'/profiles/{entity}'
    source['href'] = href
    return source


@router.get('/people/{profile_id}/photos')
def photos(profile_id: UUID, state: Literal['all', 'pending', 'confirmed'] = 'all',
           sort: Literal['time', 'score'] = 'time', cursor: Annotated[str | None, Query(max_length=1024)] = None,
           limit: Annotated[int, Query(ge=1, le=60)] = 24, media_id: UUID | None = None, db: Session = Depends(get_db)):
    params = {**_params(profile_id), 'limit': limit + 1}
    clauses = ['true' if state == 'all' else state]
    if media_id:
        params['media_id'] = media_id
        clauses.append('media_id = :media_id')
    if cursor:
        params.update(_cursor(cursor, profile_id, state, sort))
        if sort == 'time':
            clauses.append('(occurred_at, media_id) < (:after_time, CAST(:after_id AS uuid))')
        elif params['after_score'] is None:
            clauses.append('score IS NULL AND media_id < CAST(:after_id AS uuid)')
        else:
            clauses.append('(score IS NULL OR score < :after_score OR (score = :after_score AND media_id < CAST(:after_id AS uuid)))')
    order = 'occurred_at DESC, media_id DESC' if sort == 'time' else 'score DESC NULLS LAST, media_id DESC'
    rows = [dict(r) for r in db.execute(text(BASE + f"""SELECT * FROM photos
        WHERE {' AND '.join(clauses)} ORDER BY {order} LIMIT :limit"""), params).mappings()]
    page = rows[:limit]
    if page:
        ids = [r['media_id'] for r in page]
        # Page before fetching annotation lists and provenance.
        faces = list(db.execute(text("""SELECT f.id, f.media_id, f.box, f.status, f.suggestion_score AS score,
            CASE WHEN f.status = 'confirmed' THEN f.profile_id ELSE f.suggested_profile_id END AS profile_id,
            p.display_name AS name FROM faces f LEFT JOIN profiles p ON p.id =
                CASE WHEN f.status = 'confirmed' THEN f.profile_id ELSE f.suggested_profile_id END
            WHERE f.media_id = ANY(CAST(:ids AS uuid[])) AND f.status <> 'ignored'
              AND (f.status = 'confirmed' OR (f.quality >= :quality
                AND (f.suggested_profile_id IS NULL OR f.suggested_profile_id IS DISTINCT FROM f.rejected_profile_id)))
            ORDER BY f.id"""), {'ids': ids, 'quality': REVIEWABLE}).mappings())
        sources = list(db.execute(text("WITH matched AS (SELECT unnest(CAST(:ids AS uuid[])) AS media_id) "
            + 'SELECT * FROM (' + SOURCES + ') s WHERE allowed ORDER BY occurred_at DESC NULLS LAST, entity_id'),
            {'ids': ids}).mappings())
        for row in page:
            row['faces'] = [dict(f) for f in faces if f['media_id'] == row['media_id']]
            row['sources'] = [_source(s) for s in sources if s['media_id'] == row['media_id']]
    saved = snapshot(db, profile_id)
    return {'items': page, 'counts': {k: saved[k] for k in ('all', 'pending', 'confirmed')}, 'revision': saved['revision'],
            'next_cursor': _encode(page[-1], profile_id, state, sort) if len(rows) > limit else None}


@router.get('/media/{media_id}/reading')
def reading(media_id: UUID, db: Session = Depends(get_db)):
    # Only saved reading results for linked social sources; never invokes a model.
    rows = db.execute(text("""SELECT 'social_post' AS kind, r.post_id AS entity_id, r.summary, r.items
        FROM social_post_media_readings r JOIN media_links l ON l.entity_type = 'social_post'
          AND l.entity_id = r.post_id WHERE l.media_id = :id
        UNION ALL SELECT 'social_story', r.story_id, r.summary, r.items
        FROM social_story_media_readings r JOIN media_links l ON l.entity_type = 'social_story'
          AND l.entity_id = r.story_id WHERE l.media_id = :id"""), {'id': media_id}).mappings()
    return {'readings': [dict(r) for r in rows]}
