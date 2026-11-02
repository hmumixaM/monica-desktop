"""Apply the media export upgrade to the existing, separately installed collector."""
import argparse
from pathlib import Path


def replace_once(path, old, new):
    content = path.read_text()
    if new in content:
        return
    if old not in content:
        raise SystemExit(f"Collector layout changed: {path.name}")
    path.with_suffix(path.suffix + ".before-crm-media").write_text(content)
    path.write_text(content.replace(old, new, 1))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    src = args.root / "src/linkedin_enrichment"
    replace_once(src / "models.py",
                 "    mutual_count: int | None = None",
                 "    avatar_url: str | None = None\n    mutual_count: int | None = None")
    replace_once(src / "voyager.py",
                 "from .models import ExtendedEducation, ExtendedPerson",
                 "from .models import ExtendedEducation, ExtendedPerson\nfrom .profile_media import profile_avatar")
    replace_once(src / "voyager.py",
                 "    return enrich_voyager_educations(parsed, raw)",
                 '    parsed["avatar_url"] = profile_avatar(raw, public_identifier)\n    return enrich_voyager_educations(parsed, raw)')
    replace_once(src / "voyager.py", '        name=data.get("full_name"),',
                 '        name=data.get("full_name"),\n        avatar_url=data.get("avatar_url"),')
    replace_once(src / "storage.py", '        "about": data.get("about"),',
                 '        "about": data.get("about"),\n        "avatar_url": data.get("avatar_url"),')
    replace_once(src / "storage.py", '        snapshot_hash: str | None = None,\n    ) -> dict[str, Any]:',
                 '        snapshot_hash: str | None = None,\n        include_unlinked: bool = False,\n    ) -> dict[str, Any]:')
    replace_once(src / "storage.py", '                if not isinstance(contact_id, int) or not provider_user_id:',
                 '                if not provider_user_id or (not include_unlinked and not isinstance(contact_id, int)):')
    replace_once(src / "cli.py", '    export.add_argument("--snapshot-hash")',
                 '    export.add_argument("--snapshot-hash")\n    export.add_argument("--include-unlinked", action="store_true")')
    replace_once(src / "cli.py", '                snapshot_hash=args.snapshot_hash,',
                 '                snapshot_hash=args.snapshot_hash,\n                include_unlinked=args.include_unlinked,')
    script = args.root / "scripts/sync-host.sh"
    publish = ('# CRM sources also retain accounts with no human-confirmed Profile link.\n'
               'crm_payload="${PAYLOAD%.json}.crm.json"\n'
               '"$UV" run --no-sync linkedin-enrichment --db "$DB" export "$crm_payload" --include-unlinked\n'
               'chmod 600 "$crm_payload"\n'
               '"$ROOT/scripts/publish-crm.sh" "$crm_payload"')
    if publish not in script.read_text():
        legacy = '"$ROOT/scripts/publish-crm.sh" "$PAYLOAD"'
        if legacy in script.read_text():
            replace_once(script, legacy, publish)
        else:
            replace_once(script, 'chmod 600 "$PAYLOAD"', 'chmod 600 "$PAYLOAD"\n' + publish)


if __name__ == "__main__":
    main()
