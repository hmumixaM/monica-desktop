"""Initialize a new desktop source store without touching an existing deployment."""
import argparse
import json
from uuid import UUID, uuid5

from sqlalchemy import text

from .config import get_settings


def initialize(db):
    if get_settings().app_env != "desktop":
        raise ValueError("Desktop initialization requires APP_ENV=desktop")
    from .platform_store import PROVIDERS, platform_engine
    enabled = dict(db.execute(text("SELECT provider,enabled FROM platform_migration_state")).all())
    missing = [provider for provider in PROVIDERS if not enabled.get(provider)]
    if not missing:
        return {"initialized": True, "providers": list(PROVIDERS), "changed": False}
    # Existing servers must use the audited source migration, never an implicit
    # desktop switch that could orphan old CRM projections.
    for table in ("profiles", "identities", "source_accounts", "social_posts", "social_stories"):
        if db.execute(text(f"SELECT EXISTS(SELECT 1 FROM {table})")).scalar_one():
            raise ValueError("Existing data needs its normal source migration before desktop startup")
    for provider in missing:
        with platform_engine(provider).connect() as source:
            if source.execute(text("SELECT EXISTS(SELECT 1 FROM accounts)")).scalar_one():
                raise ValueError("Existing source data must be migrated explicitly")
    for provider in missing:
        db.execute(text("""INSERT INTO platform_migration_state(provider,enabled) VALUES(:provider,true)
            ON CONFLICT(provider) DO UPDATE SET enabled=true,updated_at=now()"""), {"provider": provider})
    db.commit()
    return {"initialized": True, "providers": list(PROVIDERS), "changed": True}


def account_profile(db, account, provider, external, kind, content):
    """One first-time desktop profile per collected account, never a name match.

    A recorded unlink or merge is permanent until the user links it again.
    PostgreSQL's account lock also serializes two first imports of this account.
    """
    if get_settings().app_env != "desktop" or provider not in ("instagram", "linkedin"):
        return None
    linked = db.execute(text("SELECT profile_id FROM source_account_links WHERE account_id=:id"), {"id": account}).scalar_one_or_none()
    if linked:
        return linked
    if db.execute(text("SELECT 1 FROM source_account_link_revisions WHERE account_id=:id LIMIT 1"), {"id": account}).first():
        return None
    db.execute(text("SELECT id FROM source_accounts WHERE id=:id FOR UPDATE"), {"id": account})
    linked = db.execute(text('SELECT profile_id FROM source_account_links WHERE account_id=:id'), {'id': account}).scalar_one_or_none()
    if linked:
        return linked
    if db.execute(text('SELECT 1 FROM source_account_link_revisions WHERE account_id=:id LIMIT 1'), {'id': account}).first():
        return None
    owner = content.get("owner") if isinstance(content.get("owner"), dict) else {}
    name = content.get("display_name") or content.get("full_name") or owner.get("full_name") or content.get("username") or owner.get("username") or external
    profile = uuid5(UUID(str(account)), "desktop-independent-profile-v1")
    db.execute(text("""INSERT INTO profiles(id,profile_type,display_name)
        VALUES(:id,:kind,:name) ON CONFLICT(id) DO NOTHING"""), {"id": profile, "kind": kind, "name": str(name)})
    evidence = json.dumps({"kind": "desktop-collected-account", "provider": provider, "external_id": external})
    args = {"account": account, "profile": profile, "evidence": evidence}
    db.execute(text("""INSERT INTO source_account_links(account_id,profile_id,evidence)
        VALUES(:account,:profile,CAST(:evidence AS jsonb))"""), args)
    db.execute(text("""INSERT INTO source_account_link_revisions(account_id,new_profile_id,evidence)
        VALUES(:account,:profile,CAST(:evidence AS jsonb))"""), args)
    db.execute(text("UPDATE source_accounts SET display_name=:name WHERE id=:id"), {"name": str(name), "id": account})
    return profile


def refresh_profile_name(db, account, previous, name):
    """Fill a newly collected name without overwriting a user-renamed profile."""
    profile = uuid5(UUID(str(account)), "desktop-independent-profile-v1")
    db.execute(text("""UPDATE profiles SET display_name=:name WHERE id=:profile AND display_name=:previous
        AND EXISTS(SELECT 1 FROM source_account_links WHERE account_id=:account AND profile_id=:profile)"""),
        {"profile": profile, "account": account, "previous": previous, "name": name})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["initialize"])
    parser.parse_args()
    from .db import SessionLocal
    with SessionLocal() as db:
        print(json.dumps(initialize(db)))


if __name__ == "__main__":
    main()
