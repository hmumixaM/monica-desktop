"""Report exact Instagram archive owners that do not have active CRM profiles."""
import argparse
import json
from pathlib import Path

from sqlalchemy import text

from app.db import SessionLocal
from app.reconcile_sources import identities


def audit(root: Path, *, apply: bool = False) -> dict:
    from app.account_links import set_link

    owners = {}
    for path in sorted(root.glob("*/*_UTC_*.json")):
        node = json.loads(path.read_text()).get("node", {})
        owner = node.get("owner") or {}
        external_id = str(owner.get("id") or owner.get("username") or "")
        post_id = str(node.get("id") or node.get("shortcode") or "")
        if not external_id or not post_id:
            continue
        row = owners.setdefault(
            external_id,
            {
                "external_id": external_id,
                "username": owner.get("username"),
                "full_name": owner.get("full_name"),
                "profile_pic_url": owner.get("profile_pic_url"),
                "post_ids": [],
            },
        )
        row["post_ids"].append(post_id)

    with SessionLocal() as db:
        mapping = identities(db, "instagram")
        accounts = {
            row.external_id: str(row.id)
            for row in db.execute(
                text("SELECT id,external_id FROM source_accounts WHERE provider='instagram'")
            )
        }
        links = {
            str(row.account_id): str(row.profile_id)
            for row in db.execute(text("SELECT account_id,profile_id FROM source_account_links"))
        }
        missing = []
        for external_id, owner in owners.items():
            profile_id = mapping.get(external_id)
            account_id = accounts.get(external_id)
            if profile_id is None and account_id:
                profile_id = links.get(account_id)
            if profile_id is None:
                missing.append(
                    {
                        **owner,
                        "source_account_id": account_id,
                        "post_count": len(owner["post_ids"]),
                    }
                )
        created = []
        if apply:
            for row in missing:
                if not row["source_account_id"]:
                    raise RuntimeError(
                        f"Missing source account for Instagram owner {row['external_id']}"
                    )
                concurrent = db.execute(
                    text(
                        "SELECT profile_id FROM source_account_links WHERE account_id=:account"
                    ),
                    {"account": row["source_account_id"]},
                ).scalar_one_or_none()
                if concurrent:
                    continue
                profile_id = db.execute(
                    text(
                        "INSERT INTO profiles(profile_type,display_name) "
                        "VALUES('person',:name) RETURNING id"
                    ),
                    {
                        "name": row["full_name"]
                        or row["username"]
                        or row["external_id"]
                    },
                ).scalar_one()
                set_link(
                    db,
                    row["source_account_id"],
                    profile_id,
                    expected_profile_id=None,
                    evidence={
                        "kind": "user-requested-separate-instagram-profile",
                        "source_userid": row["external_id"],
                        "source_username": row["username"],
                        "archive_post_ids": row["post_ids"],
                    },
                )
                created.append(
                    {
                        "external_id": row["external_id"],
                        "profile_id": str(profile_id),
                    }
                )
                db.commit()
        else:
            db.rollback()
    return {
        "owners": len(owners),
        "missing_owners": len(missing),
        "missing_posts": sum(row["post_count"] for row in missing),
        "missing": missing,
        "created": created,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(audit(args.root, apply=args.apply), ensure_ascii=False))
