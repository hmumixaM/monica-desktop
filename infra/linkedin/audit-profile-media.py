"""Audit local LinkedIn profile images from source snapshot through profile projection."""
import argparse
import json
from pathlib import Path

from sqlalchemy import text

from app.db import SessionLocal


def audit(snapshot_path: Path) -> dict:
    snapshot = json.loads(snapshot_path.read_text())
    assets = {row["source_url"] for row in snapshot.get("local_assets", [])}
    expected = []
    for row in snapshot.get("profiles", []):
        external_id = str(row.get("provider_user_id") or row.get("profile_url") or "")
        profile = row.get("profile") or {}
        for field, role in (
            ("avatar_url", "linkedin_avatar"),
            ("background_url", "linkedin_cover"),
        ):
            url = profile.get(field)
            if url:
                expected.append(
                    {
                        "external_id": external_id,
                        "role": role,
                        "url": url,
                        "local_file_listed": url in assets,
                    }
                )

    report = {
        "expected": len(expected),
        "expected_posts": sum(
            len(row.get("posts") or []) for row in snapshot.get("profiles", [])
        ),
        "expected_by_role": {},
        "local_file_listed": sum(row["local_file_listed"] for row in expected),
        "source_media": 0,
        "profile_media": 0,
        "files_present": 0,
        "missing_accounts": [],
        "missing_source_media": [],
        "missing_profile_links": [],
        "missing_files": [],
        "missing_posts": [],
        "samples": [],
    }
    for role in ("linkedin_avatar", "linkedin_cover"):
        report["expected_by_role"][role] = sum(row["role"] == role for row in expected)

    with SessionLocal() as db:
        for row in expected:
            account = db.execute(
                text(
                    "SELECT id FROM source_accounts "
                    "WHERE provider='linkedin' AND external_id=:external"
                ),
                {"external": row["external_id"]},
            ).scalar_one_or_none()
            if account is None:
                report["missing_accounts"].append(row)
                continue
            media = db.execute(
                text(
                    """
                    SELECT m.media_id, a.object_path
                    FROM source_account_media m
                    JOIN media_assets a ON a.id=m.media_id
                    WHERE m.account_id=:account AND m.role=:role
                    """
                ),
                {"account": account, "role": row["role"]},
            ).mappings().one_or_none()
            if media is None:
                report["missing_source_media"].append(row)
                continue
            report["source_media"] += 1
            if Path(media["object_path"]).is_file():
                report["files_present"] += 1
            else:
                report["missing_files"].append(
                    {**row, "media_id": str(media["media_id"])}
                )
            profile_id = db.execute(
                text(
                    "SELECT profile_id FROM source_account_links WHERE account_id=:account"
                ),
                {"account": account},
            ).scalar_one_or_none()
            projected = (
                db.execute(
                    text(
                        """
                        SELECT 1 FROM media_links
                        WHERE entity_type='profile' AND entity_id=:profile
                          AND media_id=:media AND role=:role
                        """
                    ),
                    {
                        "profile": profile_id,
                        "media": media["media_id"],
                        "role": row["role"],
                    },
                ).first()
                if profile_id
                else None
            )
            if projected:
                report["profile_media"] += 1
                if len(report["samples"]) < 12:
                    report["samples"].append(
                        {
                            "profile_id": str(profile_id),
                            "media_id": str(media["media_id"]),
                            "role": row["role"],
                        }
                    )
            else:
                report["missing_profile_links"].append(
                    {
                        **row,
                        "profile_id": str(profile_id) if profile_id else None,
                        "media_id": str(media["media_id"]),
                    }
                )
        for owner in snapshot.get("profiles", []):
            external_id = str(
                owner.get("provider_user_id") or owner.get("profile_url") or ""
            )
            account = db.execute(
                text(
                    "SELECT id FROM source_accounts "
                    "WHERE provider='linkedin' AND external_id=:external"
                ),
                {"external": external_id},
            ).scalar_one_or_none()
            for post in owner.get("posts") or []:
                post_id = next(
                    (
                        str(post[key])
                        for key in (
                            "provider_post_id",
                            "urn",
                            "external_id",
                            "id",
                            "source_key",
                        )
                        if post.get(key)
                    ),
                    None,
                )
                found = (
                    db.execute(
                        text(
                            "SELECT 1 FROM social_posts "
                            "WHERE provider='linkedin' AND external_id=:post "
                            "AND source_account_id=:account"
                        ),
                        {"post": post_id, "account": account},
                    ).first()
                    if account and post_id
                    else None
                )
                if not found:
                    report["missing_posts"].append(
                        {
                            "external_id": external_id,
                            "post_id": post_id,
                            "is_authored": post.get("is_authored"),
                        }
                    )
        db.rollback()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.snapshot), ensure_ascii=False))
