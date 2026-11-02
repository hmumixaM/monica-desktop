"""Operational-only pipeline metrics, shared by API and desktop settings."""

CONSUMERS = """SELECT provider,state,retained_wal_bytes,
    greatest(0,extract(epoch FROM now()-heartbeat_at))::int AS heartbeat_age_seconds
    FROM cdc_consumers ORDER BY provider"""
PENDING = """SELECT greatest(
    (SELECT count(*) FROM projection_inbox WHERE projected_at IS NULL),
    (SELECT count(*) FROM import_batches WHERE status='queued')) AS count,
    greatest(coalesce((SELECT max(extract(epoch FROM now()-received_at)) FROM projection_inbox WHERE projected_at IS NULL),0),
             coalesce((SELECT max(extract(epoch FROM now()-received_at)) FROM import_batches WHERE status='queued'),0))::int AS oldest_seconds"""
MEDIA = 'SELECT reason,count(*)::int AS count FROM media_dependencies GROUP BY reason ORDER BY reason'
LANES = """SELECT coalesce(resource_class,'db') AS lane,status,count(*)::int AS count,
    greatest(0,max(extract(epoch FROM now()-coalesce(enqueued_at,available_at))))::int AS oldest_seconds,
    count(*) FILTER(WHERE status='queued' AND available_at>now())::int AS retry_waiting
    FROM jobs WHERE status IN ('queued','running') OR
      (status='failed' AND available_at>now()-interval '7 days')
    GROUP BY resource_class,status ORDER BY resource_class,status"""


def read(execute):
    return {'consumers': [dict(row) for row in execute(CONSUMERS)],
            'projection': dict(next(iter(execute(PENDING)))),
            'media_waiting': [dict(row) for row in execute(MEDIA)],
            'lanes': [dict(row) for row in execute(LANES)]}
