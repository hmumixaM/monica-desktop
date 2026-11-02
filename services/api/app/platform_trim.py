"""Trim duplicated message storage from a platform database.

Message bodies already live in CRM content_objects under the same canonical
hash, and messages never change. For each batch of message objects this
replaces confirmed duplicate payload values with a CRM reference and removes
the synthetic single-revision history. `--vacuum` then returns the space to the
filesystem (VACUUM FULL takes exclusive locks: run it in a maintenance window).
"""

import argparse
import json

from sqlalchemy import text

from . import platform_store
from .db import SessionLocal
from .platform_store import PROVIDERS


def trim(crm, provider="wechat", batch_size=5000) -> dict:
    report = {"message_objects": 0, "payloads_referenced": 0, "revisions_removed": 0, "observations_removed": 0}
    cursor = None
    platform_store.apply_schema(provider)
    engine = platform_store.platform_engine(provider)
    from .jobs import heavy_io, yield_heavy_io

    while True:
        with heavy_io("platform_trim"), engine.begin() as source:
            rows = source.execute(text("""SELECT id, current_hash FROM objects WHERE object_kind = 'message'
                AND (CAST(:cursor AS uuid) IS NULL OR id > CAST(:cursor AS uuid)) ORDER BY id LIMIT :limit"""),
                {"cursor": cursor, "limit": batch_size}).all()
            if not rows:
                return report
            ids = [row.id for row in rows]
            hashes = sorted({row.current_hash for row in rows if row.current_hash})
            stored = source.execute(text("SELECT sha256 FROM payloads WHERE sha256 = ANY(:hashes) AND value IS NOT NULL"),
                                    {"hashes": hashes}).scalars().all()
            confirmed = crm.execute(text("""SELECT sha256 FROM content_objects
                WHERE content_kind = 'message' AND sha256 = ANY(:hashes)"""), {"hashes": list(stored)}).scalars().all()
            report["payloads_referenced"] += source.execute(text("""UPDATE payloads SET value = NULL, crm_content_kind = 'message'
                WHERE sha256 = ANY(:hashes) AND value IS NOT NULL"""), {"hashes": list(confirmed)}).rowcount
            report["observations_removed"] += source.execute(
                text("DELETE FROM observations WHERE object_id = ANY(:ids)"), {"ids": ids}).rowcount
            report["revisions_removed"] += source.execute(
                text("DELETE FROM revisions WHERE object_id = ANY(:ids)"), {"ids": ids}).rowcount
            source.execute(text("UPDATE objects SET revision_number = 0 WHERE id = ANY(:ids) AND revision_number <> 0"),
                           {"ids": ids})
        # The CRM side is read-only here; do not keep its snapshot open between batches.
        crm.commit()
        report["message_objects"] += len(rows)
        cursor = rows[-1].id
        print(json.dumps({"platform_trim": report}), flush=True)
        yield_heavy_io()


def vacuum(provider="wechat") -> None:
    engine = platform_store.platform_engine(provider).execution_options(isolation_level="AUTOCOMMIT")
    with engine.connect() as source:
        for table in ("payloads", "revisions", "observations", "objects"):
            source.exec_driver_sql(f"VACUUM (FULL, ANALYZE) {table}")


def main():
    from .jobs import heavy_io, lower_priority

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=PROVIDERS, default="wechat")
    parser.add_argument("--vacuum", action="store_true", help="VACUUM FULL the trimmed tables afterwards")
    args = parser.parse_args()
    lower_priority()
    with SessionLocal() as crm:
        print(json.dumps({"platform_trim": trim(crm, args.provider)}), flush=True)
    if args.vacuum:
        with heavy_io("platform_trim vacuum"):
            vacuum(args.provider)


if __name__ == "__main__":
    main()
