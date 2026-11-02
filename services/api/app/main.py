import hmac
import json
from uuid import UUID

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import query_services
from . import search as global_search
from .aggregate_cache import cached_endpoint
from .ai.router import router as ai_router
from .auth import current_user, token_hash
from .community_names import router as community_names_router
from .config import get_settings
from .contact_calendar import router as calendar_router
from .data_versions import DOMAINS as DATA_DOMAINS
from .db import get_db
from .directory import router as directory_router
from .faces.device import router as faces_device_router
from .faces.photos import router as face_photos_router
from .faces.review import router as faces_review_router
from .home import router as home_router
from .insights import router as insights_router
from .linkedin_collection import router as linkedin_collection_router
from .manual_media import attachments, set_activity_media, validate_media
from .orbit import router as orbit_router
from .overview import router as overview_router
from .photo_preview import router as photo_preview_router
from .profile_retirement import router as retirement_router
from .profile_summary import router as summary_router
from .repositories import (
    append_field_revision,
    create_profile,
    get_profile,
    list_field_history,
    list_profiles,
)
from .request_timing import request_timing
from .ribbon import router as ribbon_router
from .routers.auth import router as auth_router
from .routers.cdc import router as cdc_router
from .routers.chatlog_proxy import router as chatlog_proxy_router
from .routers.imports_api import router as imports_api_router
from .routers.locations import router as locations_router
from .routers.media import router as media_router
from .routers.messages import router as messages_router
from .routers.profile_dates import router as profile_dates_router
from .routers.profile_details import router as profile_details_router
from .routers.timeline import router as timeline_router
from .schemas import (
    ActivityCreate,
    ActivityState,
    FieldEdit,
    FieldRevisionOut,
    GroupMembershipCreate,
    IdentityCandidateCreate,
    IdentityCreate,
    IdentityImportCreate,
    PlatformLinkBatch,
    PlatformLinkDecision,
    ProfileCreate,
    ProfileOut,
    RelationshipCreate,
    SourceAccountLinkUpdate,
    TagCreate,
)
from .significant_dates import birthday_of, list_dates

settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0")
app.include_router(linkedin_collection_router)
app.include_router(cdc_router)
app.middleware('http')(request_timing)
for feature_router in (profile_dates_router, profile_details_router, directory_router, home_router, insights_router, orbit_router, calendar_router, ribbon_router, overview_router, auth_router, media_router, timeline_router, messages_router, locations_router, imports_api_router, chatlog_proxy_router, ai_router, community_names_router, faces_device_router, faces_review_router, face_photos_router, photo_preview_router):
    app.include_router(feature_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_CSRF_EXEMPT_PATHS = {
    "/api/v1/auth/bootstrap",
    "/api/v1/auth/login",
    "/api/v1/imports/batches",
    "/api/v1/faces/device/batch",
    "/api/v1/faces/device/media",
}


@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    """Protect browser mutations with a double-submit token.

    Import and face batches are authenticated with their source HMAC and nonce
    instead of a browser session, so those endpoints remain intentionally exempt.
    """
    if request.method not in _SAFE_METHODS and request.url.path not in _CSRF_EXEMPT_PATHS:
        cookie_token = request.cookies.get("crm_csrf")
        header_token = request.headers.get("x-csrf-token")
        if (
            not cookie_token
            or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
        ):
            return JSONResponse(status_code=403, content={"detail": "CSRF token required"})
    return await call_next(request)


if settings.app_env == "desktop":
    from .desktop_http import DesktopBoundaryMiddleware
    app.add_middleware(DesktopBoundaryMiddleware, origin=settings.desktop_origin)


_token_hash = token_hash


@app.get("/health")
@app.get("/api/v1/health")
async def health() -> dict[str, str]:
    # Runs on the event loop: a thread pool saturated by disk-bound requests
    # must not make the liveness probe time out.
    return {"status": "ok", "service": "api"}


@app.get("/api/v1/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SET LOCAL statement_timeout = '2s'"))
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.get("/api/v1/profiles", response_model=list[ProfileOut])
def profiles(
    q: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    profile_type: str | None = None,
    company: str | None = None,
    school: str | None = None,
    tag: UUID | None = None,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    return list_profiles(db, q, min(max(limit, 1), 100), cursor, profile_type, company, school, tag)


@app.post("/api/v1/profiles", response_model=ProfileOut, status_code=201)
def create_profile_endpoint(
    payload: ProfileCreate, db: Session = Depends(get_db), user=Depends(current_user)
):
    row = create_profile(db, payload.profile_type, payload.display_name, payload.summary)
    db.execute(
        text(
            "INSERT INTO audit_events(user_id, action, entity_type, entity_id) VALUES (:user, 'profile.created', 'profile', :id)"
        ),
        {"user": user["id"], "id": row["id"]},
    )
    db.commit()
    return row


@app.get("/api/v1/profiles/{profile_id}")
@cached_endpoint("profile-detail", DATA_DOMAINS)
def profile(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    from .profile_retirement import resolve_profile
    profile_id = resolve_profile(db, profile_id)
    row = get_profile(db, profile_id)
    if not row:
        raise HTTPException(status_code=404, detail="profile not found")
    fields = list(
        db.execute(
            text("""
      SELECT c.field_key, c.current_content_hash, c.current_source_type, c.updated_at,
             co.payload_json AS value
      FROM profile_field_current c
      JOIN profile_field_revisions r ON r.id = c.current_revision_id
      JOIN content_objects co ON co.id = r.content_object_id
      WHERE c.profile_id = :profile_id
        AND c.field_key !~ '(snapshot|raw_payload|direct_stats|group_stats|moments_summary|location_history)$'
      ORDER BY c.field_key
    """),
            {"profile_id": profile_id},
        ).mappings()
    )
    field_values = {str(field["field_key"]): field.get("value") for field in fields}
    avatar_url = next(
        (
            str(field_values[key])
            for key in (
                "wechat.avatar_url",
                "linkedin.avatar_url",
                "instagram.profile_pic_url",
            )
            if field_values.get(key) not in (None, "", [])
        ),
        None,
    )
    # Fetch the newest media once per account/role. Default profile reads used
    # to repeat this same media join for cover, LinkedIn, and platform avatars.
    profile_media=list(db.execute(text("""SELECT DISTINCT ON (l.role,l.source_account_id)
        m.id,m.source_record_id,m.created_at,l.source_account_id,l.role
        FROM media_links l JOIN media_assets m ON m.id=l.media_id
        WHERE l.entity_type='profile' AND l.entity_id=:id
          AND l.role IN ('cover','linkedin_cover','wechat_avatar','linkedin_avatar','instagram_avatar')
        ORDER BY l.role,l.source_account_id,m.created_at DESC"""),{'id':profile_id}).mappings())
    def latest_media(role):
        return max((m for m in profile_media if m['role']==role),key=lambda m:m['created_at'],default=None)
    cover_media=latest_media('cover')
    linkedin_avatar=latest_media('linkedin_avatar')
    linkedin_avatar_media_id=linkedin_avatar['id'] if linkedin_avatar else None
    platform_avatars=[{'media_id':m['id'],'source_account_id':m['source_account_id'],
        'provider':m['role'].removesuffix('_avatar')} for m in profile_media if m['role'].endswith('_avatar')]
    identities = list(
        db.execute(
            text("""
              SELECT id, provider, external_id, username, profile_url, display_name, status,
                     first_seen_at, last_seen_at
              FROM identities WHERE profile_id = :profile_id AND status NOT IN ('invalid','merged') ORDER BY provider, username NULLS LAST
            """),
            {"profile_id": profile_id},
        ).mappings()
    )
    structured_facts = list(
        db.execute(
            text("""
              SELECT c.fact_type, c.current_content_hash, c.current_source_type, c.updated_at,
                     co.payload_json AS value, companies.name AS company_name,
                     schools.name AS school_name
              FROM profile_structured_current c
              LEFT JOIN LATERAL (
                SELECT ar.content_object_id, NULL::uuid AS company_id, NULL::uuid AS school_id
                FROM profile_address_revisions ar
                WHERE c.fact_type = 'address' AND ar.id = c.current_revision_id
                UNION ALL
                SELECT er.content_object_id, er.company_id, NULL::uuid
                FROM profile_employment_revisions er
                WHERE c.fact_type = 'employment' AND er.id = c.current_revision_id
                UNION ALL
                SELECT ed.content_object_id, NULL::uuid, ed.school_id
                FROM profile_education_revisions ed
                WHERE c.fact_type = 'education' AND ed.id = c.current_revision_id
              ) fact ON TRUE
              LEFT JOIN content_objects co ON co.id = fact.content_object_id
              LEFT JOIN companies ON companies.id = fact.company_id
              LEFT JOIN schools ON schools.id = fact.school_id
              WHERE c.profile_id = :profile_id ORDER BY c.fact_type
            """),
            {"profile_id": profile_id},
        ).mappings()
    )
    group_members = list(
        db.execute(
            text("""
              SELECT gm.group_profile_id, gm.person_profile_id, gm.role,
                     gm.joined_at, gm.left_at, gm.observed_at, gm.source_record_id,
                     p.display_name, p.profile_type, profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id
              FROM group_memberships gm
              JOIN profiles group_profile ON group_profile.id = gm.group_profile_id
                AND group_profile.profile_type = 'group'
              JOIN profiles p ON p.id = gm.person_profile_id AND p.profile_type = 'person'
              WHERE gm.group_profile_id = :profile_id
                AND gm.person_profile_id <> gm.group_profile_id AND gm.left_at IS NULL
                AND p.archived_at IS NULL
              ORDER BY CASE WHEN gm.role IN ('owner', '群主') THEN 0 ELSE 1 END,
                       p.display_name, p.id LIMIT 16
            """),
            {"profile_id": profile_id},
        ).mappings()
    ) if row['profile_type']=='group' else []
    member_of_groups = list(
        db.execute(
            text("""
              SELECT gm.group_profile_id, gm.person_profile_id, gm.role,
                     gm.joined_at, gm.left_at, gm.observed_at, gm.source_record_id,
                     p.display_name, p.profile_type, profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id
              FROM group_memberships gm
              JOIN profiles p ON p.id = gm.group_profile_id AND p.profile_type = 'group'
              JOIN profiles person_profile ON person_profile.id = gm.person_profile_id
                AND person_profile.profile_type = 'person'
              WHERE gm.person_profile_id = :profile_id
                AND gm.person_profile_id <> gm.group_profile_id AND gm.left_at IS NULL
                AND p.archived_at IS NULL
              ORDER BY p.display_name
            """),
            {"profile_id": profile_id},
        ).mappings()
    ) if row['profile_type']=='person' else []
    group_owners = [member for member in group_members if member['role'] in ('owner', '群主')]
    group_member_count = db.execute(text("""SELECT count(*) FROM group_memberships m
        JOIN profiles p ON p.id=m.person_profile_id AND p.profile_type='person' AND p.archived_at IS NULL
        JOIN profiles g ON g.id=m.group_profile_id AND g.profile_type='group'
        WHERE m.group_profile_id=:id AND m.left_at IS NULL AND m.person_profile_id<>m.group_profile_id"""),{'id':profile_id}).scalar_one() if row['profile_type']=='group' else 0
    tags = list(db.execute(text("""SELECT t.id,t.name,m.source_type FROM tag_memberships m
        JOIN tags t ON t.id=m.tag_id WHERE m.profile_id=:id ORDER BY t.name"""),
        {'id':profile_id}).mappings())
    group_summary = None
    if row['profile_type']=='group':
        stats=db.execute(text("""SELECT o.payload_json FROM profile_field_current c
            JOIN profile_field_revisions r ON r.id=c.current_revision_id JOIN content_objects o ON o.id=r.content_object_id
            WHERE c.profile_id=:id AND c.field_key='wechat.group_stats'"""),{'id':profile_id}).scalar_one_or_none()
        if isinstance(stats,dict):
            from .source_time import iso
            group_summary={'messages':int(stats.get('n') or 0),'active_senders':int(stats.get('active_senders') or 0),
                           'first':iso(stats.get('first')),'last':iso(stats.get('last'))}
    source_observations = []
    from .profile_presentation import ALIASES, present_identities, presentation
    independent=set(db.execute(text('SELECT provider FROM platform_migration_state WHERE enabled')).scalars())
    if independent:
        # Monica's original mixed notebook is retained in history/imports.
        # Source bios now come exclusively from the linked account projection.
        fields=[dict(field) for field in fields]
        for field in fields:
            if field['field_key']=='monica.custom_fields' and isinstance(field['value'],dict):
                field['value']={label:value for label,value in field['value'].items()
                    if not any((ALIASES.get(label,'').startswith(provider+'.') or label.lower().startswith({'wechat':'微信'}.get(provider,provider)))
                               for provider in independent)}
    return {
        "presentation": presentation(fields, structured_facts),
        "tags": tags,
        "group_summary": group_summary,
        **dict(row),
        "avatar_url": avatar_url,
        "linkedin_avatar_media_id": linkedin_avatar_media_id,
        "linkedin_cover_media_id": str(latest_media("linkedin_cover")["id"]) if latest_media("linkedin_cover") else None,
        "platform_avatars": platform_avatars,
        "cover_media_id": str(cover_media["id"]) if cover_media else None,
        "cover_url": (
            str(cover_media["source_record_id"])
            if cover_media and str(cover_media["source_record_id"] or "").startswith("http")
            else None
        ),
        "fields": fields,
        "identities": present_identities(identities),
        "structured_facts": structured_facts,
        "group_members": group_members,
        "group_owners": group_owners,
        "member_of_groups": member_of_groups,
        "group_member_count": group_member_count,
        "linked_group_count": len(member_of_groups),
        "source_observations": source_observations,
        **_dates(db, profile_id, row["profile_type"]),
    }


@app.post("/api/v1/identities", status_code=201)
def import_identity(
    payload: IdentityImportCreate,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    if payload.profile_id and not get_profile(db, payload.profile_id, for_update=True):
        raise HTTPException(status_code=404, detail="profile not found")
    status_value = "active" if payload.profile_id else "pending"
    row = (
        db.execute(
            text("""
              INSERT INTO identities(profile_id, provider, external_id, username, profile_url, display_name, status)
              VALUES (:profile_id, :provider, :external_id, :username, :profile_url, :display_name, :status)
              ON CONFLICT(provider, external_id) DO UPDATE SET
                profile_id = COALESCE(EXCLUDED.profile_id, identities.profile_id),
                username = EXCLUDED.username,
                profile_url = EXCLUDED.profile_url,
                display_name = EXCLUDED.display_name,
                status = CASE WHEN EXCLUDED.profile_id IS NULL THEN identities.status ELSE 'active' END,
                last_seen_at = now()
              RETURNING id, profile_id, provider, external_id, username, profile_url, display_name, status
            """),
            {**payload.model_dump(), "status": status_value},
        )
        .mappings()
        .one()
    )
    db.commit()
    return dict(row)


@app.patch("/api/v1/profiles/{profile_id}/fields", response_model=FieldRevisionOut)
def edit_field(
    profile_id: UUID, payload: FieldEdit, db: Session = Depends(get_db), user=Depends(current_user)
):
    if not get_profile(db, profile_id, for_update=True):
        raise HTTPException(status_code=404, detail="profile not found")
    if payload.field_key in {'display_name','summary'} and (
        not isinstance(payload.value,str) or (payload.field_key=='display_name' and not payload.value.strip())
    ):
        raise HTTPException(status_code=422,detail='Name and summary must be text, and name cannot be empty')
    revision, _ = append_field_revision(
        db,
        profile_id=profile_id,
        field_key=payload.field_key,
        value=payload.value,
        source_type="manual",
        operation="manual_edit",
        actor_user_id=user["id"],
    )
    db.execute(
        text(
            """
            INSERT INTO audit_events(user_id, action, entity_type, entity_id, metadata)
            VALUES (:user, 'profile.field.edited', 'profile', :id, CAST(:metadata AS jsonb))
            """
        ),
        {
            "user": user["id"],
            "id": profile_id,
            "metadata": json.dumps({"field_key": payload.field_key}),
        },
    )
    db.commit()
    return revision


@app.get("/api/v1/profiles/{profile_id}/history", response_model=list[FieldRevisionOut])
def field_history(
    profile_id: UUID,
    field_key: str | None = None,
    limit: int = 100,
    before: int | None = None,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    return list_field_history(db, profile_id, field_key, limit, before)


@app.get("/api/v1/profiles/{profile_id}/structured-history")
def structured_history(
    profile_id: UUID,
    fact_type: str | None = None,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    if fact_type and fact_type not in {"address", "employment", "education"}:
        raise HTTPException(status_code=422, detail="unsupported structured fact type")
    rows = list(
        db.execute(
            text("""
              SELECT * FROM (
                SELECT ar.id, ar.profile_id, ar.source_account_id, 'address' AS fact_type, ar.content_hash,
                       ar.source_type, ar.source_record_id, ar.observed_at, ar.is_conflict,
                       co.payload_json AS value, NULL::text AS entity_name
                FROM profile_address_revisions ar
                JOIN content_objects co ON co.id = ar.content_object_id
                UNION ALL
                SELECT er.id, er.profile_id, er.source_account_id, 'employment' AS fact_type, er.content_hash,
                       er.source_type, er.source_record_id, er.observed_at, er.is_conflict,
                       co.payload_json AS value, c.name AS entity_name
                FROM profile_employment_revisions er
                JOIN content_objects co ON co.id = er.content_object_id
                LEFT JOIN companies c ON c.id = er.company_id
                UNION ALL
                SELECT ed.id, ed.profile_id, ed.source_account_id, 'education' AS fact_type, ed.content_hash,
                       ed.source_type, ed.source_record_id, ed.observed_at, ed.is_conflict,
                       co.payload_json AS value, s.name AS entity_name
                FROM profile_education_revisions ed
                JOIN content_objects co ON co.id = ed.content_object_id
                LEFT JOIN schools s ON s.id = ed.school_id
              ) facts
              WHERE ((source_account_id IS NULL AND profile_id = :profile_id) OR EXISTS (
                SELECT 1 FROM source_account_links l WHERE l.account_id=facts.source_account_id AND l.profile_id=:profile_id))
                AND (CAST(:fact_type AS text) IS NULL OR fact_type = :fact_type)
              ORDER BY observed_at DESC, id DESC
            """),
            {"profile_id": profile_id, "fact_type": fact_type},
        ).mappings()
    )
    return rows


@app.get("/api/v1/profiles/{profile_id}/metrics")
@cached_endpoint("profile-metrics", ("profiles", "messages", "posts", "links"))
def profile_metrics(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    if not get_profile(db, profile_id):
        raise HTTPException(status_code=404, detail="profile not found")
    return list(
        db.execute(
            text("""
              SELECT metric_key, value_json AS value, source_watermark,
                     computed_at, calculation_version, freshness_state
              FROM profile_metrics_current WHERE profile_id = :profile_id
              ORDER BY metric_key
            """),
            {"profile_id": profile_id},
        ).mappings()
    )


@app.post(
    "/api/v1/profiles/{profile_id}/history/{revision_id}/restore",
    response_model=FieldRevisionOut,
)
def restore_field_revision(
    profile_id: UUID,
    revision_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    source = (
        db.execute(
            text("""
              SELECT r.field_key, co.payload_json
              FROM profile_field_revisions r
              JOIN content_objects co ON co.id = r.content_object_id
              WHERE r.id = :revision_id AND r.profile_id = :profile_id
            """),
            {"revision_id": revision_id, "profile_id": profile_id},
        )
        .mappings()
        .one_or_none()
    )
    if not source or source["payload_json"] is None:
        raise HTTPException(status_code=404, detail="restorable revision not found")
    revision, _ = append_field_revision(
        db,
        profile_id=profile_id,
        field_key=source["field_key"],
        value=source["payload_json"],
        source_type="manual",
        operation="restore",
        actor_user_id=user["id"],
    )
    db.commit()
    return revision


@app.post("/api/v1/profiles/{profile_id}/identities", status_code=201)
def add_identity(
    profile_id: UUID,
    payload: IdentityCreate,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    if not get_profile(db, profile_id, for_update=True):
        raise HTTPException(status_code=404, detail="profile not found")
    existing=db.execute(text('SELECT profile_id,source_account_id FROM identities WHERE provider=:provider AND external_id=:external'),
                        {'provider':payload.provider,'external':payload.external_id}).mappings().one_or_none()
    if existing and existing['profile_id']!=profile_id:
        raise HTTPException(status_code=409,detail='Confirm the source account link through account management')
    if payload.provider in ('wechat','wechat_group','instagram','linkedin'):
        raise HTTPException(status_code=409,detail='Platform accounts are managed through source account links')
    row = (
        db.execute(
            text("""
          INSERT INTO identities(profile_id, provider, external_id, username, profile_url, display_name)
          VALUES (:profile_id, :provider, :external_id, :username, :profile_url, :display_name)
          ON CONFLICT(provider, external_id) DO UPDATE SET
            profile_id = EXCLUDED.profile_id,
            username = EXCLUDED.username,
            profile_url = EXCLUDED.profile_url,
            display_name = EXCLUDED.display_name,
            last_seen_at = now()
          RETURNING id, profile_id, provider, external_id, username, profile_url, display_name, status
        """),
            {"profile_id": profile_id, **payload.model_dump()},
        )
        .mappings()
        .one()
    )
    db.commit()
    return dict(row)


@app.get('/api/v1/source-accounts')
def source_accounts(q: str = '', provider: str | None = None, profile_id: UUID | None = None,
                    cursor: UUID | None = None, db: Session = Depends(get_db), _user=Depends(current_user)):
    from .link_ownership import link_blockers
    rows=[dict(row) for row in db.execute(text('''SELECT a.id,a.provider,a.external_id,a.object_kind,a.display_name,a.username,a.profile_url,
        l.profile_id,p.display_name AS linked_profile_name
        FROM source_accounts a LEFT JOIN source_account_links l ON l.account_id=a.id
        LEFT JOIN profiles p ON p.id=l.profile_id
        WHERE (CAST(:provider AS text) IS NULL OR a.provider=:provider)
          AND (CAST(:profile AS uuid) IS NULL OR l.profile_id=:profile)
          AND (CAST(:cursor AS uuid) IS NULL OR a.id>:cursor)
          AND (:q='' OR a.display_name ILIKE :query OR a.external_id ILIKE :query OR a.username ILIKE :query)
          AND NOT EXISTS(SELECT 1 FROM identities i WHERE i.source_account_id=a.id AND i.status='invalid'
            AND NOT EXISTS(SELECT 1 FROM identities v WHERE v.source_account_id=a.id AND v.status NOT IN ('invalid','merged')))
        ORDER BY a.id LIMIT 50'''),{'provider':provider,'profile':profile_id,'cursor':cursor,'q':q,'query':'%'+q+'%'}).mappings()]
    blockers=link_blockers(db,[row['id'] for row in rows]) if rows else {}
    return [{**row,'ready':str(row['id']) not in blockers,'blocked_reason':blockers.get(str(row['id']))} for row in rows]


@app.put('/api/v1/source-accounts/{account_id}/link')
def link_source_account(account_id: UUID,payload: SourceAccountLinkUpdate,db: Session=Depends(get_db),user=Depends(current_user)):
    from .account_links import set_link
    result=set_link(db,account_id,payload.profile_id,expected_profile_id=payload.expected_profile_id,actor=user['id'])
    db.commit()
    return result


@app.get('/api/v1/link-review')
@cached_endpoint("link-review", ("links", "profiles"))
def link_review(pair: str = '', status_filter: str = 'unresolved', q: str = '', sort: str = 'confidence',
                page: int = Query(1, ge=1), page_size: int = Query(50, ge=10, le=200),
                db: Session = Depends(get_db), _user=Depends(current_user)):
    from .link_review import review
    return review(db, pair=pair, status_filter=status_filter, query=q, sort=sort, page=page, page_size=page_size)


@app.get('/api/v1/link-review/linked')
@cached_endpoint("link-review-linked", ("links", "profiles"))
def link_review_linked(q: str = '', providers: str = '', db: Session = Depends(get_db), _user=Depends(current_user)):
    from .link_review import linked_people
    return linked_people(db, query=q, providers=providers)


@app.post('/api/v1/link-review/batch')
def apply_platform_link_batch(payload: PlatformLinkBatch, db: Session = Depends(get_db), user=Depends(current_user)):
    from .link_review import apply_batch
    result = apply_batch(db, payload, user)
    db.commit()
    return result


@app.get('/api/v1/link-review/contacts')
def search_link_contacts(q: str = '', provider: str = '', db: Session = Depends(get_db), _user=Depends(current_user)):
    from .link_review import search_contacts
    return search_contacts(db, query=q, provider=provider)


@app.post('/api/v1/link-review/confirm')
def confirm_platform_link(payload: PlatformLinkDecision, db: Session = Depends(get_db), user=Depends(current_user)):
    from .link_review import confirm
    result = confirm(db, payload, user)
    db.commit()
    return result


@app.post('/api/v1/link-review/reject')
def reject_platform_link(payload: PlatformLinkDecision, db: Session = Depends(get_db), user=Depends(current_user)):
    from .link_review import decide
    result = decide(db, payload, user, 'rejected')
    db.commit()
    return result


@app.post('/api/v1/link-review/skip')
def skip_platform_link(payload: PlatformLinkDecision, db: Session = Depends(get_db), user=Depends(current_user)):
    from .link_review import decide
    result = decide(db, payload, user, 'skipped')
    db.commit()
    return result


@app.get("/api/v1/identity-candidates")
@cached_endpoint("identity-candidates", ("links", "profiles"))
def identity_candidates(
    status_filter: str = "pending",
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    return list(
        db.execute(
            text("""
              SELECT c.id, c.score, c.evidence, c.status, i.profile_id AS source_profile_id,
                     i.provider, i.external_id, i.username,
                     p.id AS candidate_profile_id, p.display_name AS candidate_display_name
              FROM identity_candidates c
              JOIN identities i ON i.id = c.identity_id
              JOIN profiles p ON p.id = c.candidate_profile_id
              WHERE c.status = :status
              ORDER BY c.score DESC, c.id LIMIT 100
            """),
            {"status": status_filter},
        ).mappings()
    )


@app.post("/api/v1/identity-candidates", status_code=201)
def create_identity_candidate(
    payload: IdentityCandidateCreate,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    identity_exists = db.execute(
        text("SELECT id FROM identities WHERE id = :id"), {"id": payload.identity_id}
    ).scalar_one_or_none()
    profile_exists = db.execute(
        text("SELECT id FROM profiles WHERE id = :id"), {"id": payload.candidate_profile_id}
    ).scalar_one_or_none()
    if not identity_exists or not profile_exists:
        raise HTTPException(status_code=404, detail="identity or candidate profile not found")
    row = (
        db.execute(
            text("""
              INSERT INTO identity_candidates(identity_id, candidate_profile_id, score, evidence)
              VALUES (:identity_id, :profile_id, :score, CAST(:evidence AS jsonb))
              RETURNING id, identity_id, candidate_profile_id, score, evidence, status
            """),
            {
                "identity_id": payload.identity_id,
                "profile_id": payload.candidate_profile_id,
                "score": payload.score,
                "evidence": json.dumps(payload.evidence, ensure_ascii=False),
            },
        )
        .mappings()
        .one()
    )
    db.commit()
    return dict(row)


@app.post("/api/v1/identity-candidates/{candidate_id}/accept")
def accept_identity_candidate(
    candidate_id: UUID, db: Session = Depends(get_db), user=Depends(current_user)
):
    candidate = (
        db.execute(
            text("""
          SELECT identity_id, candidate_profile_id FROM identity_candidates
          WHERE id = :id AND status = 'pending'
        """),
            {"id": candidate_id},
        )
        .mappings()
        .one_or_none()
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="candidate not found")
    identity = db.execute(text("SELECT profile_id,source_account_id FROM identities WHERE id=:id FOR UPDATE"),
                          {"id": candidate["identity_id"]}).mappings().one()
    original=identity['profile_id']
    target = candidate["candidate_profile_id"]
    if not identity['source_account_id']:
        raise HTTPException(status_code=409,detail='Account migration must finish before linking')
    from .account_links import set_link
    set_link(db,identity['source_account_id'],target,actor=user['id'],expected_profile_id=original,
             evidence={'kind':'accepted-candidate','candidate_id':str(candidate_id)})
    db.execute(
        text("""
          UPDATE identities SET profile_id = :profile_id, status = 'active', last_seen_at = now()
          WHERE id = :identity_id
        """),
        {"profile_id": candidate["candidate_profile_id"], "identity_id": candidate["identity_id"]},
    )
    db.execute(
        text("""
          UPDATE identity_candidates SET status = 'accepted', reviewed_by = :user, reviewed_at = now()
          WHERE id = :id
        """),
        {"id": candidate_id, "user": user["id"]},
    )
    db.commit()
    return {"status": "accepted", "profile_id": str(candidate["candidate_profile_id"])}


@app.post("/api/v1/identity-candidates/{candidate_id}/reject")
def reject_identity_candidate(
    candidate_id: UUID, db: Session = Depends(get_db), user=Depends(current_user)
):
    result = db.execute(
        text("""
          UPDATE identity_candidates SET status = 'rejected', reviewed_by = :user, reviewed_at = now()
          WHERE id = :id AND status = 'pending' RETURNING id
        """),
        {"id": candidate_id, "user": user["id"]},
    ).scalar_one_or_none()
    if not result:
        raise HTTPException(status_code=404, detail="candidate not found")
    db.commit()
    return {"status": "rejected"}


@app.get("/api/v1/profiles/{profile_id}/relationships")
@cached_endpoint("profile-relationships", ("profiles", "links", "media"), source=query_services.profile_relationships)
def profile_relationships(
    profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)
):
    return query_services.profile_relationships(profile_id=profile_id, db=db)


@app.post("/api/v1/profiles/{profile_id}/relationships", status_code=201)
def create_relationship(
    profile_id: UUID,
    payload: RelationshipCreate,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    origin=get_profile(db, profile_id, for_update=True); target=get_profile(db, payload.to_profile_id, for_update=True)
    if not origin or not target:
        raise HTTPException(status_code=404, detail="profile not found")
    if profile_id==payload.to_profile_id or origin['profile_type']!='person' or target['profile_type']!='person':
        raise HTTPException(status_code=422,detail='Relationships require two different people; use group membership for groups')
    if any(word in payload.relationship_type.lower() for word in ('group','member','owner','群')):
        raise HTTPException(status_code=422,detail='Use group membership for group roles')
    rel_type = db.execute(
        text("""
          INSERT INTO relationship_types(name) VALUES (:name)
          ON CONFLICT(name) DO UPDATE SET name = EXCLUDED.name RETURNING id
        """),
        {"name": payload.relationship_type},
    ).scalar_one()
    edge = db.execute(
        text("""
          INSERT INTO relationship_edges(from_profile_id, to_profile_id, relationship_type_id, note,
                                         valid_from, valid_to, source_type)
          VALUES (:from_id, :to_id, :type_id, :note, :valid_from, :valid_to, 'manual')
          ON CONFLICT(from_profile_id, to_profile_id, relationship_type_id) DO UPDATE SET
            note = EXCLUDED.note, valid_from = EXCLUDED.valid_from, valid_to = EXCLUDED.valid_to
          RETURNING id
        """),
        {
            "from_id": profile_id,
            "to_id": payload.to_profile_id,
            "type_id": rel_type,
            "note": payload.note,
            "valid_from": payload.valid_from,
            "valid_to": payload.valid_to,
        },
    ).scalar_one()
    db.execute(
        text("""
          INSERT INTO relationship_edge_revisions(edge_id, operation, payload, actor_user_id)
          VALUES (:edge_id, 'created', CAST(:payload AS jsonb), :user)
        """),
        {
            "edge_id": edge,
            "payload": json.dumps(payload.model_dump(), default=str),
            "user": user["id"],
        },
    )
    db.commit()
    return {"id": str(edge), "status": "created"}


@app.post("/api/v1/tags", status_code=201)
def create_tag(payload: TagCreate, db: Session = Depends(get_db), _user=Depends(current_user)):
    row = (
        db.execute(
            text("""
          INSERT INTO tags(name) VALUES (:name)
          ON CONFLICT(name) DO UPDATE SET name = EXCLUDED.name RETURNING id, name
        """),
            {"name": payload.name},
        )
        .mappings()
        .one()
    )
    db.commit()
    return dict(row)


@app.get('/api/v1/tags')
@cached_endpoint("tags", ("tags", "profiles"), source=query_services.list_tags)
def list_tags(profile_type: str | None=None, db: Session=Depends(get_db), _user=Depends(current_user)):
    return query_services.list_tags(profile_type=profile_type, db=db)


@app.post("/api/v1/profiles/{profile_id}/tags/{tag_id}")
def attach_tag(
    profile_id: UUID, tag_id: UUID, db: Session = Depends(get_db), user=Depends(current_user)
):
    db.execute(text("DELETE FROM tag_membership_removals WHERE tag_id = :tag AND profile_id = :profile"),
               {"tag": tag_id, "profile": profile_id})
    db.execute(
        text("""
          INSERT INTO tag_memberships(tag_id, profile_id, source_type) VALUES (:tag, :profile, 'manual')
          ON CONFLICT(tag_id, profile_id) DO NOTHING
        """),
        {"tag": tag_id, "profile": profile_id},
    )
    db.execute(
        text("""
          INSERT INTO tag_membership_revisions(tag_id, profile_id, operation, actor_user_id)
          VALUES (:tag, :profile, 'added', :user)
        """),
        {"tag": tag_id, "profile": profile_id, "user": user["id"]},
    )
    db.commit()
    return {"status": "attached"}


@app.delete("/api/v1/profiles/{profile_id}/tags/{tag_id}", status_code=204)
def detach_tag(profile_id: UUID, tag_id: UUID, db: Session = Depends(get_db), user=Depends(current_user)):
    """Remove a tag, including an imported one; later imports will not add it back."""
    removed = db.execute(text("DELETE FROM tag_memberships WHERE tag_id = :tag AND profile_id = :profile RETURNING source_type"),
                         {"tag": tag_id, "profile": profile_id}).scalar_one_or_none()
    if removed is None:
        raise HTTPException(status_code=404, detail="tag not on profile")
    db.execute(text("""INSERT INTO tag_membership_removals(tag_id, profile_id, removed_by) VALUES (:tag, :profile, :user)
        ON CONFLICT (tag_id, profile_id) DO UPDATE SET removed_by = EXCLUDED.removed_by, removed_at = now()"""),
        {"tag": tag_id, "profile": profile_id, "user": user["id"]})
    db.execute(text("""INSERT INTO tag_membership_revisions(tag_id, profile_id, operation, source_type, actor_user_id)
        VALUES (:tag, :profile, 'removed', :source, :user)"""), {"tag": tag_id, "profile": profile_id, "source": removed, "user": user["id"]})
    db.commit()
    return Response(status_code=204)


@app.post("/api/v1/activities", status_code=201)
def create_activity(
    payload: ActivityCreate,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    validate_media(db, payload.media_ids or [])
    for participant in payload.participant_profile_ids:
        if not get_profile(db, participant, for_update=True):
            raise HTTPException(status_code=404, detail=f"profile not found: {participant}")
    activity_id = db.execute(
        text("""
          INSERT INTO activities(activity_type, occurred_at, title, body, source_type, created_by, due_at)
          VALUES (:activity_type, :occurred_at, :title, :body, 'manual', :created_by, :due_at)
          RETURNING id
        """),
        {
            "activity_type": payload.activity_type,
            "occurred_at": payload.occurred_at,
            "title": payload.title,
            "body": payload.body,
            "created_by": user["id"],
            "due_at": payload.due_at,
        },
    ).scalar_one()
    for participant in payload.participant_profile_ids:
        db.execute(
            text("""
              INSERT INTO activity_participants(activity_id, profile_id)
              VALUES (:activity_id, :profile_id) ON CONFLICT DO NOTHING
            """),
            {"activity_id": activity_id, "profile_id": participant},
        )
    set_activity_media(db, activity_id, payload.media_ids or [], user["id"])
    db.commit()
    return {"id": str(activity_id), "status": "created"}


@app.get("/api/v1/activities")
def list_activities(
    profile_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    params: dict[str, object] = {"limit": min(max(limit, 1), 100), "offset": max(offset, 0)}
    clause = "a.activity_type NOT IN ('wechat_direct_summary','wechat_group_summary')"
    if profile_id:
        params["profile_id"] = profile_id
        clause += " AND EXISTS (SELECT 1 FROM activity_participants ap WHERE ap.activity_id = a.id AND ap.profile_id = :profile_id)"
    rows = list(
        db.execute(
            text(f"""
              SELECT a.id, a.activity_type, a.occurred_at, a.title, a.body,
                     a.source_type, a.created_at, a.state, a.due_at,
                     (SELECT jsonb_agg(jsonb_build_object('id',p.id,'display_name',p.display_name) ORDER BY p.display_name)
                      FROM activity_participants ap JOIN profiles p ON p.id=ap.profile_id WHERE ap.activity_id=a.id) AS participants
              FROM activities a WHERE {clause}
              ORDER BY a.occurred_at DESC, a.id DESC LIMIT :limit OFFSET :offset
            """),
            params,
        ).mappings()
    )


    media = attachments(db, "activity", [row["id"] for row in rows])
    return [{**dict(row), "media": media.get(str(row["id"]), [])} for row in rows]


@app.patch('/api/v1/activities/{activity_id}/state')
def set_activity_state(activity_id: UUID, payload: ActivityState, db: Session = Depends(get_db), _user=Depends(current_user)):
    row=db.execute(text("UPDATE activities SET state=:state WHERE id=:id AND activity_type IN ('task','reminder') RETURNING id,state"),
                   {'id':activity_id,'state':payload.state}).mappings().one_or_none()
    if not row:raise HTTPException(status_code=404,detail='task or reminder not found')
    db.commit();return dict(row)


@app.put('/api/v1/activities/{activity_id}')
def edit_activity(activity_id: UUID, payload: ActivityCreate, db: Session = Depends(get_db), _user=Depends(current_user)):
    # Imported facts keep their source evidence. Manual notes and records can be edited.
    row=db.execute(text("SELECT id FROM activities WHERE id=:id AND source_type='manual' FOR UPDATE"),{'id':activity_id}).first()
    if not row:raise HTTPException(status_code=404,detail='manual record not found')
    for pid in payload.participant_profile_ids:
        if not get_profile(db,pid, for_update=True):raise HTTPException(status_code=404,detail='participant not found')
    db.execute(text("UPDATE activities SET activity_type=:kind,occurred_at=:at,title=:title,body=:body,due_at=:due WHERE id=:id"),
               {'id':activity_id,'kind':payload.activity_type,'at':payload.occurred_at,'title':payload.title,'body':payload.body,'due':payload.due_at})
    db.execute(text('DELETE FROM activity_participants WHERE activity_id=:id'),{'id':activity_id})
    for pid in set(payload.participant_profile_ids):
        db.execute(text('INSERT INTO activity_participants(activity_id,profile_id) VALUES(:id,:pid)'),{'id':activity_id,'pid':pid})
    if payload.media_ids is not None:
        set_activity_media(db, activity_id, payload.media_ids, _user['id'])
    db.commit();return {'id':str(activity_id),'status':'updated'}


@app.post("/api/v1/groups/{group_id}/members", status_code=201)
def add_group_member(
    group_id: UUID,
    payload: GroupMembershipCreate,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    group = get_profile(db, group_id, for_update=True)
    person = get_profile(db, payload.person_profile_id, for_update=True)
    if not group or group["profile_type"] != "group" or not person or person["profile_type"] != "person":
        raise HTTPException(status_code=422, detail="membership requires a group and a person")
    row = (
        db.execute(
            text("""
              INSERT INTO group_memberships(group_profile_id, person_profile_id, role, joined_at, left_at)
              VALUES (:group_id, :person_id, :role, :joined_at, :left_at)
              ON CONFLICT (group_profile_id, person_profile_id) DO UPDATE SET
                role = EXCLUDED.role, joined_at = EXCLUDED.joined_at, left_at = EXCLUDED.left_at,
                observed_at = now()
              RETURNING group_profile_id, person_profile_id, role, joined_at, left_at, observed_at
            """),
            {
                "group_id": group_id,
                "person_id": payload.person_profile_id,
                "role": payload.role,
                "joined_at": payload.joined_at,
                "left_at": payload.left_at,
            },
        )
        .mappings()
        .one()
    )
    db.commit()
    return dict(row)


@app.get("/api/v1/groups/{group_id}/members")
def group_members(group_id: UUID, limit: int = 50, offset: int = 0, db: Session = Depends(get_db), _user=Depends(current_user)):
    return list(
        db.execute(
            text("""
              SELECT gm.group_profile_id, gm.person_profile_id, p.display_name,
                     gm.role, gm.joined_at, gm.left_at, gm.observed_at,p.profile_type,p.avatar_media_id
              FROM group_memberships gm JOIN profiles p ON p.id = gm.person_profile_id AND p.profile_type='person'
              JOIN profiles g ON g.id=gm.group_profile_id AND g.profile_type='group'
              WHERE gm.group_profile_id = :group_id
                AND gm.person_profile_id <> gm.group_profile_id AND gm.left_at IS NULL AND p.archived_at IS NULL
              ORDER BY CASE WHEN gm.role IN ('owner','群主') THEN 0 ELSE 1 END,p.display_name,p.id LIMIT :limit OFFSET :offset
            """),
            {"group_id": group_id,'limit':min(max(limit,1),100),'offset':max(offset,0)},
        ).mappings()
    )


@app.get("/api/v1/search")
def search(q: str, limit: int = 30, db: Session = Depends(get_db), _user=Depends(current_user)):
    return global_search.search(db, q, limit)


@app.get("/api/v1/metrics/summary")
@cached_endpoint("metrics-summary", DATA_DOMAINS, stale_on_change=True, source=query_services.metrics_summary)
def metrics_summary(db: Session = Depends(get_db), _user=Depends(current_user)):
    return query_services.metrics_summary(db=db)


@app.get("/api/v1/relationships")
def relationships(limit: int = 10, db: Session = Depends(get_db), _user=Depends(current_user)):
    """Return a small, name-resolved relationship slice for the dashboard.

    The full graph remains on-demand. This endpoint only reads the indexed edge
    projection and keeps the overview request bounded for large archives.
    """
    return list(
        db.execute(
            text("""
              SELECT e.id, e.from_profile_id, e.to_profile_id,
                     source.display_name AS from_display_name,
                     target.display_name AS to_display_name,
                     rt.name AS relationship_type, e.source_type,
                     e.confidence, e.created_at
              FROM relationship_edges e
              JOIN profiles source ON source.id = e.from_profile_id
              JOIN profiles target ON target.id = e.to_profile_id
              JOIN relationship_types rt ON rt.id = e.relationship_type_id
              ORDER BY e.created_at DESC, e.id DESC
              LIMIT :limit
            """),
            {"limit": min(max(limit, 1), 50)},
        ).mappings()
    )


@app.get("/api/v1/relationships/graph")
@cached_endpoint("relationship-graph", ("profiles", "links", "posts", "media", "ai", "faces"), stale_on_change=True, source=query_services.relationship_graph)
def relationship_graph(
    limit: int = 900,
    source_type: str | None = None,
    relationship_type: str | None = None,
    min_confidence: float = 0,
    format: str | None = None,
    focus: UUID | None = None,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    return query_services.relationship_graph(limit=limit, source_type=source_type, relationship_type=relationship_type, min_confidence=min_confidence, format=format, focus=focus, db=db)


@app.get("/api/v1/metrics/snapshots")
@cached_endpoint("metric-snapshots", ("profiles",), ttl=300, stale_on_change=True)
def metric_snapshots(limit: int = 100, db: Session = Depends(get_db), _user=Depends(current_user)):
    return list(
        db.execute(
            text("""
              SELECT DISTINCT ON (metric_key) metric_key, value_json AS value,
                     source_watermark, computed_at, calculation_version, freshness_state
              FROM global_metric_snapshots
              ORDER BY metric_key, computed_at DESC
              LIMIT :limit
            """),
            {"limit": min(max(limit, 1), 200)},
        ).mappings()
    )


@app.get("/api/v1/metrics/people-overview")
@cached_endpoint("people-overview", DATA_DOMAINS, stale_on_change=True, source=query_services.people_overview)
def people_overview(db: Session = Depends(get_db), _user=Depends(current_user)):
    return query_services.people_overview(db=db)


def _dates(db, profile_id, profile_type) -> dict:
    if profile_type != "person":
        return {"birthday": None, "significant_dates": []}
    dates = list_dates(db, profile_id)
    return {"birthday": birthday_of(dates), "significant_dates": dates}


@app.get('/api/v1/profiles/{profile_id}/moments-circle')
@cached_endpoint("moments-circle", ("posts", "links", "profiles", "media", "faces"))
def profile_moments_circle(profile_id: UUID, db: Session=Depends(get_db), _user=Depends(current_user)):
    owner=get_profile(db,profile_id)
    if not owner:raise HTTPException(status_code=404,detail='profile not found')
    if owner['profile_type']!='person':raise HTTPException(status_code=422,detail='Moments belong to people, not groups')
    from .graph import circle_projection
    return circle_projection(db,settings.raw_root,profile_id)


@app.get('/api/v1/metrics/social')
def social_statistics(profile_id: UUID | None=None, db: Session=Depends(get_db), _user=Depends(current_user)):
    from .social_analytics import social_analytics
    return social_analytics(db,profile_id)

app.include_router(summary_router)
app.include_router(retirement_router)
