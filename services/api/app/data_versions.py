"""Change counters that invalidate cached aggregates by domain.

Versions are advanced by database triggers (migration 046) whenever a
statement changes rows in a domain's tables, so no writer can forget them.
"""

from sqlalchemy import text

DOMAINS = ('profiles', 'links', 'tags', 'messages', 'posts', 'media', 'contact_events')
# The contact directory summarizes names, links, tags and message statistics.
DIRECTORY = ('profiles', 'links', 'tags', 'messages')
# Model outputs and the choices made about them (migration 058).
AI = 'ai'
# Detected faces and their review decisions (migration 059).
FACES = 'faces'
# Text and descriptions read off post images (migration 062).
POST_MEDIA = 'post_media'


def current(db, domains):
    unknown = set(domains) - {*DOMAINS, AI, FACES, POST_MEDIA}
    if unknown:
        raise ValueError(f'Unknown data domain: {sorted(unknown)}')
    ordered = sorted(set(domains))
    # One MVCC snapshot covers both the compacted count and pending events.
    values = db.execute(text("""SELECT t.domain, t.version + count(e.id) AS version
        FROM data_version_totals t LEFT JOIN data_version_events e ON e.domain=t.domain
        WHERE t.domain = ANY(CAST(:domains AS text[])) GROUP BY t.domain, t.version"""),
                        {"domains": ordered}).all()
    return {domain: int(version) for domain, version in values}


def compact(db, limit=10000):
    """Move visible events into totals atomically; never lose late commits.

    Counting consumed events rather than assigning max(sequence) makes the
    total independent of commit order. Version is invariant under compaction.
    """
    if not db.execute(text("SELECT pg_try_advisory_xact_lock(11803350)")).scalar_one():
        return 0
    return db.execute(text("""WITH consumed AS (
        DELETE FROM data_version_events WHERE id IN (
            SELECT id FROM data_version_events ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED)
        RETURNING domain), counts AS (SELECT domain, count(*) AS n FROM consumed GROUP BY domain)
        UPDATE data_version_totals t SET version=t.version+counts.n FROM counts
        WHERE t.domain=counts.domain"""), {"limit": limit}).rowcount
