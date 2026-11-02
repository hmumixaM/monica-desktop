"""Read-only audit of historical message keys; never rewrites or merges IDs."""
import json
from itertools import groupby

from sqlalchemy import text

from .platform_store import platform_engine
from .source_time import message_identity, message_key


def audit(source):
    report = {'timezone': source.execute(text('SHOW TimeZone')).scalar_one(), 'checked': 0,
              'canonical': 0, 'legacy': 0, 'unrecognized': 0, 'ambiguous': 0, 'examples': []}
    rows = source.execute(text("""SELECT id,account_id,external_id,occurred_at FROM objects
        WHERE object_kind='message' ORDER BY account_id,occurred_at,id"""),
        execution_options={'yield_per': 1000})
    for _group, events in groupby(rows, lambda r: (r.account_id, r.occurred_at)):
        seen = set()
        for row in events:
            report['checked'] += 1
            identity = message_identity(row.external_id)
            if identity is None or identity[1] != row.occurred_at:
                report['unrecognized'] += 1
                continue
            report['canonical' if row.external_id == message_key(*identity) else 'legacy'] += 1
            if identity in seen or len(seen) >= 10000:
                report['ambiguous'] += 1
                if len(report['examples']) < 20:
                    report['examples'].append({'id': str(row.id), 'key': row.external_id})
            else:
                seen.add(identity)
    return report


if __name__ == '__main__':
    with platform_engine('wechat').connect() as source:
        # Every row is consumed. The default cursor fraction otherwise favors
        # random index reads on the NAS, unlike EXPLAIN of a plain SELECT.
        source.execute(text('SET TRANSACTION READ ONLY'))
        source.execute(text('SET LOCAL cursor_tuple_fraction=1.0'))
        source.execute(text("SET LOCAL work_mem='64MB'"))
        source.execute(text("SET LOCAL statement_timeout='15min'"))
        print(json.dumps(audit(source), ensure_ascii=False, indent=2))
