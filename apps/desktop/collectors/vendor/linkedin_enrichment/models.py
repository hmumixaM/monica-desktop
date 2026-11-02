"""Collector models layered on the pinned upstream models."""

from typing import Any, Literal

from linkedin_scraper import Education, Person, Post
from pydantic import BaseModel, Field, HttpUrl


class ExtendedEducation(Education):
    """Education fields retained from the raw Voyager entity."""

    degree_name: str | None = None
    field_of_study: str | None = None
    activities: str | None = None
    grade: str | None = None
    school_urn: str | None = None
    school_url: str | None = None
    education_urn: str | None = None


class MutualConnection(BaseModel):
    display_name: str
    profile_url: str


class ExtendedPerson(Person):
    """Upstream ``Person`` plus richer education and mutual metadata."""

    educations: list[ExtendedEducation] = Field(default_factory=list)
    avatar_url: str | None = None
    background_url: str | None = None
    mutual_count: int | None = None
    mutual_preview_names: list[str] = Field(default_factory=list)
    mutual_browse_url: str | None = None
    mutual_connections: list[MutualConnection] = Field(default_factory=list)
    mutual_is_complete: bool = False


class MutualPreview(BaseModel):
    count: int | None = None
    names: list[str] = Field(default_factory=list)
    browse_url: str | None = None
    connections: list[MutualConnection] = Field(default_factory=list)
    is_complete: bool = False


class ExtendedPost(Post):
    """Post plus best-effort DOM authorship evidence."""

    actor_profile_url: str | None = None
    repost_header: str | None = None
    is_authored: bool | None = None
    activity_type: Literal["post", "repost", "other_actor", "unknown"] = "unknown"


class Target(BaseModel):
    profile_url: HttpUrl
    external_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def url(self) -> str:
        return str(self.profile_url)


class SnapshotEnvelope(BaseModel):
    kind: Literal["profile", "posts", "mutuals"]
    data: dict[str, Any] | list[dict[str, Any]]
    complete: bool = True


def dump_post(post: Post) -> dict[str, Any]:
    """Serialize an upstream post without coupling callers to Pydantic details."""

    return post.model_dump(mode="json")
