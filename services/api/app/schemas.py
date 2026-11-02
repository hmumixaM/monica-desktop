from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProfileCreate(BaseModel):
    profile_type: Literal["person", "group"] = "person"
    display_name: str = Field(min_length=1, max_length=255)
    summary: str | None = None


class BootstrapRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=512)


class LoginRequest(BootstrapRequest):
    recovery_code: str | None = None
    totp_code: str | None = Field(default=None, min_length=6, max_length=8)


class TotpCode(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class ImportServiceTokenCreate(BaseModel):
    source: Literal["wechat", "instagram", "linkedin", "monica", "faces"]
    label: str = Field(min_length=1, max_length=120)


class ProfileOut(ProfileCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    avatar_media_id: UUID | None = None
    created_at: datetime
    archived_at: datetime | None = None


class FieldEdit(BaseModel):
    field_key: str = Field(min_length=1, max_length=120)
    value: Any
    source_type: Literal["manual"] = "manual"


class DetailRef(BaseModel):
    """One displayed detail: a whole field, or one named entry inside a bundled object field."""
    field_key: str = Field(min_length=1, max_length=120)
    item: str | None = Field(default=None, min_length=1, max_length=120)


class DetailSave(BaseModel):
    field_key: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=20000)
    replaces: list[DetailRef] = Field(default_factory=list, max_length=20)


class DetailRemove(BaseModel):
    refs: list[DetailRef] = Field(min_length=1, max_length=20)


class SignificantDateWrite(BaseModel):
    kind: Literal["birthday", "anniversary", "other"]
    label: str | None = Field(default=None, max_length=60)
    precision: Literal["month_day", "approximate", "exact"]
    year: int | None = Field(default=None, ge=1800, le=2200)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)


class FieldRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    profile_id: UUID
    field_key: str
    content_hash: str
    source_type: str
    operation: str
    observed_at: datetime
    is_conflict: bool
    value: Any | None = None


class IdentityCreate(BaseModel):
    provider: Literal["wechat", "instagram", "linkedin"]
    external_id: str = Field(min_length=1, max_length=255)
    username: str | None = None
    profile_url: str | None = None
    display_name: str | None = None


class IdentityImportCreate(IdentityCreate):
    profile_id: UUID | None = None


class RelationshipCreate(BaseModel):
    to_profile_id: UUID
    relationship_type: str = Field(min_length=1, max_length=120)
    note: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MediaSelection(BaseModel):
    media_ids: list[UUID] = Field(max_length=50)


class ActivityCreate(BaseModel):
    media_ids: list[UUID] | None = Field(default=None, max_length=50)
    activity_type: str = Field(min_length=1, max_length=120)
    occurred_at: datetime
    title: str = Field(min_length=1, max_length=255)
    body: str | None = None
    participant_profile_ids: list[UUID] = Field(default_factory=list)
    due_at: datetime | None = None


class ActivityState(BaseModel):
    state: Literal['open', 'done']


class GroupMembershipCreate(BaseModel):
    person_profile_id: UUID
    role: str | None = None
    joined_at: datetime | None = None
    left_at: datetime | None = None


class MediaInitiate(BaseModel):
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    media_type: str = Field(default="application/octet-stream", max_length=255)
    expected_byte_length: int | None = Field(default=None, ge=0)


class BatchRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    external_id: str = Field(min_length=1, max_length=512)
    content: Any
    profile_id: UUID | None = None
    sender_profile_id: UUID | None = None
    source_account_external_id: str | None = None
    entity_type: str | None = None
    provider: str | None = None
    conversation_external_id: str | None = None
    conversation_type: str | None = None
    message_type: str | None = None
    occurred_at: datetime | None = None
    source_updated_at: datetime | None = None
    media: list[dict[str, Any]] = Field(default_factory=list)


class ImportBatchRequest(BaseModel):
    source: Literal["wechat", "instagram", "linkedin", "monica"]
    stream: str = Field(min_length=1, max_length=120)
    schema_version: int = Field(default=1, ge=1)
    batch_id: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    cursor_before: str | None = None
    cursor_after: str | None = None
    observed_at: datetime
    records: list[BatchRecord] = Field(default_factory=list)
    raw_objects: list[dict[str, Any]] = Field(default_factory=list)
    media_manifest: list[dict[str, Any]] = Field(default_factory=list)


class ImportBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    stream: str
    batch_id: str
    status: str
    received_at: datetime
    completed_at: datetime | None
    inserted_count: int
    changed_count: int
    unchanged_count: int
    error_summary: str | None


class IdentityCandidateCreate(BaseModel):
    identity_id: UUID
    candidate_profile_id: UUID
    score: float = Field(ge=0, le=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class SourceAccountLinkUpdate(BaseModel):
    profile_id: UUID | None
    expected_profile_id: UUID | None


class PlatformLinkDecision(BaseModel):
    account_id: UUID
    profile_id: UUID
    expected_profile_id: UUID | None = None
    score: int | None = None
    reasons: list[str] = Field(default_factory=list)


class PlatformLinkBatchItem(PlatformLinkDecision):
    action: Literal["confirm", "reject", "skip"]


class PlatformLinkBatch(BaseModel):
    items: list[PlatformLinkBatchItem] = Field(min_length=1, max_length=25)


class TimelineItemOut(BaseModel):
    id: UUID
    provider: str
    external_id: str
    occurred_at: datetime | None
    title: str
    summary: str | None
    content_hash: str


class MetricOut(BaseModel):
    metric_key: str
    value: Any
    computed_at: datetime | None
    freshness_state: str
