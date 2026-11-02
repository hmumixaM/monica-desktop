"""Structured profile collection through LinkedIn's in-browser Voyager API."""

import json
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from linkedin_cli.api.voyager import parse_linkedin_voyager_response
from linkedin_scraper import Experience

from .browser import AccessBlocked, AccessSignal
from .models import ExtendedEducation, ExtendedPerson
from .profile_media import profile_media

_PROFILE_DECORATION = (
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-91"
)


def _public_identifier(profile_url: str) -> str:
    segments = [part for part in urlsplit(profile_url).path.split("/") if part]
    if len(segments) < 2 or segments[0] != "in":
        raise ValueError("profile_url must contain /in/<public-identifier>")
    return segments[1]


def _linkedin_date(value: dict[str, int] | None) -> str | None:
    if not value or not value.get("year"):
        return None
    month = value.get("month")
    return f"{value['year']}-{month:02d}" if month else str(value["year"])


def _entity_url(urn: str | None, entity_type: str) -> str | None:
    if not urn:
        return None
    identifier = urn.rsplit(":", 1)[-1]
    if not identifier.isdigit():
        return None
    return f"https://www.linkedin.com/{entity_type}/{identifier}/"


def _resolved_entity(
    entity: dict[str, Any],
    field_name: str,
    urn_map: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    reference = entity.get(field_name)
    if isinstance(reference, list):
        reference = next(iter(reference), None)
    return urn_map.get(reference) if isinstance(reference, str) else None


def _school_url(
    school: dict[str, Any] | None,
    school_urn: str | None,
) -> str | None:
    if school:
        for field in ("url", "companyPageUrl", "navigationUrl"):
            value = school.get(field)
            if isinstance(value, str) and value.strip():
                return urljoin("https://www.linkedin.com", value.strip())
    return _entity_url(school_urn, "school")


def enrich_voyager_educations(
    parsed: dict[str, Any],
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Restore education fields dropped by linkedin-cli's stable parser."""

    urn_map = {
        entity["entityUrn"]: entity
        for entity in raw.get("included", [])
        if isinstance(entity, dict) and entity.get("entityUrn")
    }
    educations = []
    for education in parsed.get("educations", []):
        row = dict(education)
        education_urn = row.get("urn")
        raw_education = urn_map.get(education_urn, {})
        school = _resolved_entity(raw_education, "*school", urn_map)
        school_reference = raw_education.get("*school")
        if isinstance(school_reference, list):
            school_reference = next(iter(school_reference), None)
        school_urn = (
            school.get("entityUrn")
            if school
            else raw_education.get("schoolUrn") or school_reference
        )
        row.update(
            {
                "degree_name": raw_education.get(
                    "degreeName", row.get("degree_name")
                ),
                "field_of_study": raw_education.get(
                    "fieldOfStudy", row.get("field_of_study")
                ),
                "description": raw_education.get("description"),
                "activities": raw_education.get("activities"),
                "grade": raw_education.get("grade"),
                "school_urn": school_urn,
                "school_url": _school_url(school, school_urn),
                "education_urn": education_urn or raw_education.get("entityUrn"),
            }
        )
        educations.append(row)
    return {**parsed, "educations": educations}


def parse_voyager_profile(
    raw: dict[str, Any],
    *,
    public_identifier: str,
) -> dict[str, Any]:
    """Parse the stable profile contract and restore raw education detail."""

    parsed = parse_linkedin_voyager_response(
        raw,
        public_identifier=public_identifier,
    )
    return enrich_voyager_educations(parsed, raw)


def person_from_voyager(profile_url: str, data: dict[str, Any]) -> ExtendedPerson:
    """Translate linkedin-cli's stable profile contract to the pinned models."""

    experiences = []
    for row in data.get("positions", []):
        date_range = row.get("date_range") or {}
        experiences.append(
            Experience(
                position_title=row.get("title"),
                institution_name=row.get("company_name"),
                linkedin_url=_entity_url(row.get("company_urn"), "company"),
                from_date=_linkedin_date(date_range.get("start")),
                to_date=_linkedin_date(date_range.get("end")),
                location=row.get("location"),
                description=row.get("description"),
            )
        )

    educations = []
    for row in data.get("educations", []):
        date_range = row.get("date_range") or {}
        educations.append(
            ExtendedEducation(
                institution_name=row.get("school_name"),
                degree_name=row.get("degree_name"),
                field_of_study=row.get("field_of_study"),
                description=row.get("description"),
                activities=row.get("activities"),
                grade=row.get("grade"),
                school_urn=row.get("school_urn"),
                school_url=row.get("school_url"),
                education_urn=row.get("education_urn") or row.get("urn"),
                linkedin_url=row.get("school_url"),
                from_date=_linkedin_date(date_range.get("start")),
                to_date=_linkedin_date(date_range.get("end")),
            )
        )

    return ExtendedPerson(
        linkedin_url=profile_url,
        name=data.get("full_name"),
        location=data.get("location_name"),
        about=data.get("summary"),
        open_to_work=False,
        experiences=experiences,
        educations=educations,
    )


async def scrape_voyager_profile(page: Any, profile_url: str) -> ExtendedPerson:
    """Fetch and parse one profile without navigating away from its profile page."""

    public_identifier = _public_identifier(profile_url)
    cookies = {
        cookie["name"]: cookie["value"]
        for cookie in await page.context.cookies("https://www.linkedin.com")
    }
    csrf_token = cookies.get("JSESSIONID", "").strip('"')
    if not csrf_token:
        raise AccessBlocked(AccessSignal.AUTH, "LinkedIn JSESSIONID is unavailable")

    query = urlencode(
        {
            "decorationId": _PROFILE_DECORATION,
            "memberIdentity": public_identifier,
            "q": "memberIdentity",
        }
    )
    result = await page.evaluate(
        """async ({url, csrfToken}) => {
            const response = await fetch(url, {
                credentials: "include",
                headers: {
                    accept: "application/vnd.linkedin.normalized+json+2.1",
                    "csrf-token": csrfToken,
                    "x-li-lang": "en_US",
                    "x-restli-protocol-version": "2.0.0"
                }
            });
            return {
                status: response.status,
                body: await response.text()
            };
        }""",
        {
            "url": (
                "https://www.linkedin.com/voyager/api/identity/dash/profiles?"
                f"{query}"
            ),
            "csrfToken": csrf_token,
        },
    )
    status = result["status"]
    if status == 401:
        raise AccessBlocked(
            AccessSignal.AUTH,
            f"LinkedIn Voyager profile request returned HTTP {status}",
        )
    if status == 403:
        raise RuntimeError(
            "LinkedIn Voyager denied this profile with HTTP 403"
        )
    if status == 429:
        raise AccessBlocked(
            AccessSignal.RATE_LIMIT,
            "LinkedIn Voyager profile request returned HTTP 429",
        )
    if status != 200:
        raise RuntimeError(f"LinkedIn Voyager profile request returned HTTP {status}")

    raw = json.loads(result["body"])
    parsed = parse_voyager_profile(
        raw,
        public_identifier=public_identifier,
    )
    person = person_from_voyager(profile_url, parsed)
    avatar_url, background_url = profile_media(raw, public_identifier)
    return person.model_copy(
        update={
            "avatar_url": avatar_url or person.avatar_url,
            "background_url": background_url,
        }
    )
