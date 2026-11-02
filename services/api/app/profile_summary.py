"""Bounded first-paint data; full dossiers and expensive sections load separately."""
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from .aggregate_cache import cached_endpoint
from .auth import current_user
from .config import get_settings
from .data_versions import DOMAINS
from .db import get_db
from .overview import _contact_events, _messages
from .profile_presentation import present_identities
from .repositories import get_profile
from .significant_dates import birthday_of, list_dates

router = APIRouter()


def build_summary(db, profile_id):
    from .profile_retirement import resolve_profile
    profile_id = resolve_profile(db, profile_id)
    row = get_profile(db, profile_id)
    if not row:
        raise HTTPException(404, 'Profile not found')
    profile = dict(row)
    profile['avatar_media_id'] = db.execute(text('SELECT profile_avatar_media(:id,:chosen)'),
        {'id': profile_id, 'chosen': row['avatar_media_id']}).scalar_one()
    profile['identities'] = present_identities(db.execute(text("""SELECT id,provider,external_id,
        username,profile_url,display_name,status FROM identities
        WHERE profile_id=:id AND status NOT IN ('invalid','merged') ORDER BY provider,id"""),
        {'id': profile_id}).mappings())
    profile['linked_group_count'] = db.execute(text("""SELECT count(*) FROM group_memberships m
        JOIN profiles p ON p.id=m.group_profile_id AND p.archived_at IS NULL
        WHERE m.person_profile_id=:id AND m.left_at IS NULL"""), {'id': profile_id}).scalar_one()
    dates = list_dates(db, profile_id)
    profile['significant_dates'] = dates
    profile['birthday'] = birthday_of(dates)
    today = datetime.now(ZoneInfo(get_settings().crm_timezone)).date()
    messages = _messages(db, profile_id, today) if row['profile_type'] == 'person' else None
    has_posts = db.execute(text("""SELECT EXISTS(SELECT 1 FROM social_posts WHERE profile_id=:id)
        OR EXISTS(SELECT 1 FROM social_stories WHERE profile_id=:id)"""), {'id': profile_id}).scalar_one()
    return {'profile': profile, 'has_posts': has_posts,
        'overview': {'messages': messages, 'contact_events': _contact_events(db, profile_id)}}


@router.get('/api/v1/profiles/{profile_id}/summary')
@cached_endpoint('profile-summary', DOMAINS)
def summary(profile_id: UUID, db=Depends(get_db), _user=Depends(current_user)):
    return build_summary(db, profile_id)
