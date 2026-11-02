"""Import exact LinkedIn manifest posts missing from the CRM projection."""
import argparse
import json
from pathlib import Path

from sqlalchemy import text

from app.db import SessionLocal
from app.full_migration import _linkedin_media, iso, record_digest, run_batch


def import_missing(snapshot_path: Path) -> dict:
    snapshot = json.loads(snapshot_path.read_text())
    with SessionLocal() as db:
        if not db.execute(text("SELECT pg_try_advisory_lock(11803291)")).scalar_one():
            raise RuntimeError("LinkedIn importer is busy")
        try:
            accounts = dict(
                db.execute(
                    text(
                        """
                        SELECT a.external_id,l.profile_id
                        FROM source_accounts a
                        LEFT JOIN source_account_links l ON l.account_id=a.id
                        WHERE a.provider='linkedin'
                        """
                    )
                ).all()
            )
            existing = set(
                db.execute(
                    text(
                        "SELECT external_id FROM social_posts WHERE provider='linkedin'"
                    )
                ).scalars()
            )
            records = []
            seen = {}
            for item in snapshot.get("profiles") or []:
                account = str(
                    item.get("provider_user_id") or item.get("profile_url") or ""
                )
                if account not in accounts:
                    raise RuntimeError(f"LinkedIn source account is missing: {account}")
                for original in item.get("posts") or []:
                    external_id = next(
                        (
                            str(original[key])
                            for key in (
                                "provider_post_id",
                                "urn",
                                "external_id",
                                "id",
                                "source_key",
                            )
                            if original.get(key)
                        ),
                        None,
                    )
                    if not external_id or external_id in existing:
                        continue
                    if external_id in seen:
                        if seen[external_id] != account:
                            raise RuntimeError(
                                f"Ambiguous LinkedIn post owner: {external_id}"
                            )
                        continue
                    seen[external_id] = account
                    profile_id = accounts[account]
                    post = {
                        **original,
                        "profile_id": str(profile_id) if profile_id else None,
                        "provider": "linkedin",
                    }
                    media = _linkedin_media(post)
                    records.append(
                        {
                            "external_id": external_id,
                            "profile_id": post["profile_id"],
                            "source_account_external_id": account,
                            "provider": "linkedin",
                            "entity_type": "post",
                            "occurred_at": iso(
                                post.get("occurred_at")
                                or post.get("posted_at")
                                or post.get("created_at")
                            ),
                            "content": {
                                **post,
                                "media": media,
                                "like_count": post.get("reaction_count"),
                                "comment_count": post.get("comment_count"),
                            },
                            "media": media,
                        }
                    )
            if records:
                run_batch(
                    db,
                    source="linkedin",
                    stream="posts",
                    records=records,
                    batch_name=(
                        "linkedin-manifest-missing-posts-"
                        + record_digest(records)[:20]
                    ),
                )
            return {"missing_posts_imported": len(records)}
        finally:
            db.rollback()
            db.execute(text("SELECT pg_advisory_unlock(11803291)"))
            db.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(import_missing(args.snapshot)))
