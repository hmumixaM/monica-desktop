"""Record the upright width and height of stored images.

The feed reserves layout space from these dimensions. Pages load originals
directly; no resized copies are made. New images are queued by a database
trigger (migrations 049/052) and measured by the worker; historical media
without dimensions is backfilled at idle disk priority.
"""

import argparse
import json
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import text

from .config import get_settings

ORIENTATION = 0x0112


def upright_size(source: Path) -> tuple[int, int] | None:
    """Read only the image header; None if the file is not an image."""
    try:
        with Image.open(source) as image:
            width, height = image.size
            rotated = image.getexif().get(ORIENTATION) in (5, 6, 7, 8)
    except UnidentifiedImageError:
        return None
    return (height, width) if rotated else (width, height)


def record_dimensions(db, media_id) -> bool:
    asset = db.execute(text("SELECT media_type, object_path, width, height FROM media_assets WHERE id = :id"),
                       {"id": media_id}).mappings().one_or_none()
    if not asset or not str(asset["media_type"]).startswith("image/") or not Path(asset["object_path"]).is_file():
        return False
    size = upright_size(Path(asset["object_path"]))
    if size and (asset["width"], asset["height"]) != size:
        db.execute(text("UPDATE media_assets SET width = :w, height = :h WHERE id = :id"),
                   {"w": size[0], "h": size[1], "id": media_id})
    return size is not None


def backfill(db, *, batch_size=500) -> dict:
    """Measure images that have no recorded dimensions, newest first."""
    from .jobs import heavy_io, yield_heavy_io

    report = {"checked": 0, "measured": 0, "skipped": 0}
    after = {"after_at": None, "after_id": None}
    while True:
        with heavy_io("media_dimensions backfill"):
            rows = db.execute(text("""SELECT id, created_at FROM media_assets
                WHERE media_type LIKE 'image/%' AND width IS NULL
                  AND (CAST(:after_at AS timestamptz) IS NULL
                    OR (created_at, id) < (CAST(:after_at AS timestamptz), CAST(:after_id AS uuid)))
                ORDER BY created_at DESC, id DESC LIMIT :limit"""), {**after, "limit": batch_size}).mappings().all()
            if not rows:
                db.commit()
                return report
            for row in rows:
                report["checked"] += 1
                report["measured" if record_dimensions(db, row["id"]) else "skipped"] += 1
            db.commit()
        after = {"after_at": rows[-1]["created_at"], "after_id": rows[-1]["id"]}
        print(json.dumps({"media_dimensions": report}), flush=True)
        yield_heavy_io()


def remove_thumbnails(media_root: Path) -> int:
    """Delete the resized copies earlier releases wrote under media/thumbnails."""
    removed = 0
    for path in sorted((media_root / "thumbnails").rglob("*.webp")):
        path.unlink()
        removed += 1
    return removed


def main():
    from .db import SessionLocal
    from .jobs import lower_priority

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove-thumbnails", action="store_true", help="delete media/thumbnails from earlier releases")
    args = parser.parse_args()
    lower_priority()
    with SessionLocal() as db:
        report = backfill(db)
    if args.remove_thumbnails:
        report["thumbnails_removed"] = remove_thumbnails(get_settings().media_root)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
