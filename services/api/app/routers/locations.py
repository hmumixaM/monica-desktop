"""Profile and global location maps."""
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..aggregate_cache import cached_endpoint
from ..auth import current_user
from ..config import get_settings
from ..db import get_db
from ..location_points import _build_location_summary, _location_point
from ..repositories import (
    get_profile,
)

router = APIRouter()
settings = get_settings()




@router.get("/api/v1/profiles/{profile_id}/locations")
@cached_endpoint("profile-locations", ("posts", "profiles", "links"))
def profile_locations(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    owner = get_profile(db, profile_id)
    if not owner:
        raise HTTPException(status_code=404, detail="profile not found")
    if owner['profile_type'] == 'group':
        return {"profile_id": str(profile_id), "total_posts_with_location": 0,
                "place_count": 0, "points": [], "field_sources": []}
    rows = list(
        db.execute(
            text("""
              SELECT p.id,p.profile_id,p.provider,p.occurred_at,p.location,
                p.summary,p.coordinate_format
              FROM social_location_events p
              WHERE p.profile_id = :profile_id
                AND p.location IS NOT NULL AND p.location<>'null'::jsonb
                AND NOT p.location @> '{"latitude":"0","longitude":"0"}'::jsonb
              ORDER BY p.occurred_at DESC NULLS LAST
              LIMIT 50000
            """),
            {"profile_id": profile_id},
        ).mappings()
    )
    field_rows = list(
        db.execute(
            text("""
              SELECT c.field_key, co.payload_json AS value, c.current_source_type, c.updated_at
              FROM profile_field_current c
              JOIN profile_field_revisions r ON r.id = c.current_revision_id
              JOIN content_objects co ON co.id = r.content_object_id
              WHERE c.profile_id = :profile_id
                AND (c.field_key ILIKE '%address%' OR c.field_key ILIKE '%location%'
                     OR c.field_key = 'wechat.location_history')
              ORDER BY c.updated_at DESC
            """),
            {"profile_id": profile_id},
        ).mappings()
    )
    for field in (field_rows if not any(_location_point(row.get("location"),row.get("coordinate_format")) for row in rows) else []):
        value = field["value"]
        candidates = value if isinstance(value, list) else (value.get("places", [value]) if isinstance(value,dict) else [])
        for candidate in candidates:
            if isinstance(candidate, dict):
                rows.append(
                    {
                        "provider": field["current_source_type"],
                        "occurred_at": None,
                        "location": candidate,
                        "count": int(candidate.get("visits", 0)),
                        "first_seen": candidate.get("first"),
                        "last_seen": candidate.get("last"),
                        "source_kind": "archived_visits" if "visits" in candidate else "profile_address",
                        "events": [{"at": m.get("at"), "text": m.get("text", ""),
                                    "source": field["current_source_type"], "url": None}
                                   for m in candidate.get("moments", []) if m.get("at")],
                    }
                )
    points = _build_location_summary([dict(row) for row in rows])
    return {
        "profile_id": str(profile_id),
        "total_posts_with_location": sum(int(item["count"]) for item in points),
        "place_count": len(points),
        "points": points[:500],
        "field_sources": [
            {"field_key": row["field_key"], "source_type": row["current_source_type"], "updated_at": row["updated_at"]}
            for row in field_rows
        ],
    }


@router.get("/api/v1/metrics/locations")
def location_metrics(provider: str | None=None, year: int | None=None, kind: str='all',
                     db: Session=Depends(get_db), _user=Depends(current_user)):
    if provider not in (None,'wechat','instagram','linkedin') or kind not in ('all','profile','post') or (year is not None and not 1900<=year<=2100):
        raise HTTPException(status_code=422,detail='Invalid location filter')
    from ..location_atlas import atlas
    # The atlas contract already contains JSON primitives; avoid recursively
    # re-encoding every point and person on the event loop.
    return JSONResponse(atlas(db,provider,year,kind))
