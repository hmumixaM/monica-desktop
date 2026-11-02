"""Compact, presentation-ready profile facts shared by directory, overview and home."""

import re
from collections import defaultdict
from datetime import date

from sqlalchemy import text

NAME_FIELDS = ("wechat.remark", "wechat.nickname", "wechat.alias", "monica.nickname")
CITY_FIELDS = ("monica.residence", "wechat.city", "linkedin.location", "wechat.province")
HEADLINE_FIELDS = ("linkedin.headline", "monica.company", "monica.job")
GROUP_NAMES_FIELD = "wechat.group_nicknames"
TEXT_FIELDS = (*NAME_FIELDS, *CITY_FIELDS, *HEADLINE_FIELDS, GROUP_NAMES_FIELD)
GROUP_NAME_SEPARATOR = re.compile(r"\s*[｜|]\s*")
PRESENT = re.compile(r"present|current|now|至今|现在", re.IGNORECASE)
EXPERIENCE_KEYS = ("title", "company", "company_name", "school_name", "degree", "field_of_study", "location",
                   "start_date", "end_date", "is_current", "description", "linkedin_url")


def _text(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _content_payloads(db, ids):
    ids = sorted({str(value) for value in ids})
    payloads = {}
    # Bound each cold read without raising the interactive statement budget
    # when background imports evict thousands of directory values from cache.
    for offset in range(0, len(ids), 512):
        payloads.update(db.execute(text("""SELECT id, payload_json FROM content_objects
            WHERE id = ANY(CAST(:ids AS uuid[]))"""), {"ids": ids[offset:offset + 512]}).all())
    return payloads


def field_texts(db, keys=TEXT_FIELDS, profile_id=None, *, active_only=False) -> dict[str, dict[str, str]]:
    rows = db.execute(text("""SELECT c.profile_id, c.field_key, c.current_revision_id
        FROM profile_field_current c
        WHERE c.field_key = ANY(:keys) AND (CAST(:pid AS uuid) IS NULL OR c.profile_id = CAST(:pid AS uuid))
          AND (NOT :active_only OR EXISTS (SELECT 1 FROM profiles p WHERE p.id=c.profile_id AND p.archived_at IS NULL))"""),
        {"keys": list(keys), "pid": profile_id, "active_only": active_only}).all()
    if not rows:
        return {}
    revisions = dict(db.execute(text("""SELECT id, content_object_id FROM profile_field_revisions
        WHERE id = ANY(CAST(:ids AS uuid[]))"""),
        {"ids": list({str(row.current_revision_id) for row in rows})}).all())
    payloads = _content_payloads(db, revisions.values())
    values: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        value = _text(payloads.get(revisions.get(row.current_revision_id)))
        if value:
            values[str(row.profile_id)][row.field_key] = value
    return values


def _detail_text(value):
    if isinstance(value, list):
        return "\n".join(dict.fromkeys(part for item in value if (part := _text(item)))) or None
    return _text(value)


def custom_fields(db, profile_id) -> dict[str, str]:
    """User-named fields from Monica, which are stored together as one object."""
    row = db.execute(text("""SELECT o.payload_json FROM profile_field_current c
        JOIN profile_field_revisions r ON r.id = c.current_revision_id
        JOIN content_objects o ON o.id = r.content_object_id
        WHERE c.profile_id = CAST(:pid AS uuid) AND c.field_key = 'monica.custom_fields'"""), {"pid": profile_id}).scalar_one_or_none()
    return {key: value for key, item in (row or {}).items() if (value := _detail_text(item))} if isinstance(row, dict) else {}


def instagram_bios(db, profile_id) -> list[str]:
    """One biography per linked Instagram account."""
    rows = db.execute(text("""SELECT o.payload_json FROM profile_field_current c
        JOIN profile_field_revisions r ON r.id = c.current_revision_id
        JOIN content_objects o ON o.id = r.content_object_id
        WHERE c.profile_id = CAST(:pid AS uuid) AND c.field_key LIKE 'instagram.%biography'"""), {"pid": profile_id}).scalars()
    return list(dict.fromkeys(value for item in rows if (value := _text(item))))


def experiences(db, profile_id=None) -> dict[str, list[dict]]:
    """Current employment and education facts, most recently observed first."""
    # Resolve current revisions first, then fetch their payloads in one batch.
    # A large directory join otherwise performs thousands of scattered heap
    # reads against the shared content store on the NAS.
    rows = db.execute(text("""SELECT c.profile_id, c.fact_type, c.fact_key, c.current_source_type AS source,
          er.content_object_id, companies.id AS company_id, companies.name AS company_name,
          NULL::uuid AS school_id, NULL::text AS school_name, er.observed_at
        FROM profile_structured_current c
        JOIN profile_employment_revisions er ON er.id = c.current_revision_id
        LEFT JOIN companies ON companies.id = er.company_id
        WHERE c.fact_type = 'employment'
          AND (CAST(:pid AS uuid) IS NULL OR c.profile_id = CAST(:pid AS uuid))
        UNION ALL
        SELECT c.profile_id, c.fact_type, c.fact_key, c.current_source_type AS source,
          ed.content_object_id, NULL::uuid AS company_id, NULL::text AS company_name,
          schools.id AS school_id, schools.name AS school_name, ed.observed_at
        FROM profile_structured_current c
        JOIN profile_education_revisions ed ON ed.id = c.current_revision_id
        LEFT JOIN schools ON schools.id = ed.school_id
        WHERE c.fact_type = 'education'
          AND (CAST(:pid AS uuid) IS NULL OR c.profile_id = CAST(:pid AS uuid))
        ORDER BY profile_id, fact_type, fact_key"""), {"pid": profile_id}).mappings().all()
    if not rows:
        return {}
    payloads = _content_payloads(db, (row["content_object_id"] for row in rows))
    facts: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["content_object_id"] not in payloads:
            continue
        value = payloads[row["content_object_id"]]
        payload = value if isinstance(value, dict) else {}
        fact = {"type": row["fact_type"], "source": row["source"]}
        fact.update({key: payload[key] for key in EXPERIENCE_KEYS if payload.get(key) not in (None, "", [])})
        payload_company, payload_school = fact.pop("company_name", None), fact.pop("school_name", None)
        company = row["company_name"] or payload_company or fact.get("company")
        school = row["school_name"] or payload_school
        if company:
            fact["company"] = company
        if school:
            fact["school"] = school
        if row["company_id"]:
            fact["company_id"] = str(row["company_id"])
        if row["school_id"]:
            fact["school_id"] = str(row["school_id"])
        facts[str(row["profile_id"])].append(fact)
    return {profile: ordered_experience(rows) for profile, rows in facts.items()}


def ordered_experience(facts: list[dict]) -> list[dict]:
    """Deduplicate repeated imports of one role and list current roles, then newest first."""
    unique = {}
    for fact in facts:
        key = (fact["type"], fact.get("company") or fact.get("school"), fact.get("title") or fact.get("degree"),
               fact.get("start_date"), fact.get("end_date"))
        unique.setdefault(key, fact)
    return sorted(unique.values(), key=lambda fact: (
        fact["type"] != "employment", not is_current(fact), _negated(str(fact.get("start_date") or ""))))


def _negated(value: str) -> tuple:
    return tuple(-ord(char) for char in value)


def is_current(fact: dict) -> bool:
    if isinstance(fact.get("is_current"), bool):
        return fact["is_current"]
    ended = fact.get("end_date")
    return ended in (None, "") or bool(PRESENT.search(str(ended)))


def current_job(facts: list[dict]) -> dict | None:
    jobs = [fact for fact in facts if fact["type"] == "employment" and (fact.get("company") or fact.get("title"))]
    return next((job for job in jobs if is_current(job)), jobs[0] if jobs else None)


def headline(fields: dict[str, str], facts: list[dict]) -> str | None:
    job = current_job(facts)
    if job:
        return " · ".join(part for part in (job.get("company"), job.get("title")) if part) or None
    company, title = fields.get("monica.company"), fields.get("monica.job")
    if company or title:
        return " · ".join(part for part in (company, title) if part)
    return fields.get("linkedin.headline")


def city(fields: dict[str, str]) -> str | None:
    if fields.get("monica.residence"):
        return fields["monica.residence"]
    if fields.get("wechat.city"):
        return fields["wechat.city"]
    if fields.get("linkedin.location"):
        return fields["linkedin.location"].split(",")[0].strip()
    return fields.get("wechat.province")


def aliases(name: str, fields: dict[str, str], identity_names) -> list[str]:
    seen = {name.casefold()} if name else set()
    result = []
    for value in [*(fields.get(key) for key in NAME_FIELDS), *identity_names]:
        value = _text(value)
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result[:8]


def group_names(value: str | None, known: list[str]) -> list[str]:
    """Names someone uses inside WeChat groups ("group｜name" per line), minus names already listed."""
    seen = {name.casefold() for name in known}
    result = []
    for line in (value or "").splitlines():
        name = GROUP_NAME_SEPARATOR.split(line.strip(), maxsplit=1)[-1].strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
    return result


def avatar_map(db, profile_ids) -> dict[str, str]:
    """Displayed avatar per profile in one pass (see profile_avatar_media)."""
    ids = list({str(pid) for pid in profile_ids if pid})
    if not ids:
        return {}
    return {str(pid): str(media) for pid, media in db.execute(text("""SELECT p.id, COALESCE(p.avatar_media_id, a.media_id)
        FROM profiles p LEFT JOIN (SELECT DISTINCT ON (l.entity_id) l.entity_id, l.media_id
          FROM media_links l JOIN media_assets m ON m.id = l.media_id
          WHERE l.entity_type = 'profile' AND l.entity_id = ANY(CAST(:ids AS uuid[]))
            AND l.role IN ('wechat_avatar', 'linkedin_avatar', 'instagram_avatar')
          ORDER BY l.entity_id, m.created_at DESC) a ON a.entity_id = p.id
        WHERE p.id = ANY(CAST(:ids AS uuid[]))"""), {"ids": ids}) if media}


def birthday_md(row) -> str | None:
    if not row or not row.get("month") or not row.get("day"):
        return None
    return f"{int(row['month']):02}-{int(row['day']):02}"


def next_occurrence(month: int, day: int, today: date) -> date:
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:  # 29 February outside a leap year
            candidate = date(year, 3, 1)
        if candidate >= today:
            return candidate
    raise AssertionError("a yearly date recurs within two calendar years")


def month_series(rows, months: int, today: date) -> list[dict]:
    """A dense series ending in the current month; absent months are zeros."""
    counts = {row["month"].strftime("%Y-%m"): row for row in rows}
    series = []
    year, month = today.year, today.month
    for _ in range(months):
        key = f"{year:04}-{month:02}"
        row = counts.get(key)
        series.append({"month": key, "sent": int(row["sent"]) if row else 0,
                       "received": int(row["received"]) if row else 0})
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return list(reversed(series))


def compact(value: dict) -> dict:
    """Drop empty blocks: None, 0-only summaries, empty lists and dicts."""
    return {key: item for key, item in value.items() if item not in (None, "", [], {}, 0)}


AI_NAME_FIELDS = {"wechat.remark": "微信备注", "wechat.nickname": "微信昵称", "wechat.alias": "微信号",
                  "monica.nickname": "备注名", "wechat.group_nicknames": "群内显示名"}


def contact_contexts(db, profile_ids, *, compact=False, full=False) -> dict[str, dict]:
    """Small profile details for AI, resolved through current revisions rather than stale summaries."""
    from .post_text import excerpt

    ids = list(dict.fromkeys(str(pid) for pid in profile_ids))
    if not ids:
        return {}
    length, details_limit = (300, 4) if compact else (None, None) if full else (1200, 12)
    contexts = {str(row.id): {"name": row.display_name, "summary": row.summary if full else excerpt(row.summary, length),
                             "names": [], "group_names": [], "details": [], "notes": []}
                for row in db.execute(text("SELECT id,display_name,summary FROM profiles WHERE id=ANY(CAST(:ids AS uuid[]))"), {"ids": ids})}
    rows = db.execute(text("""SELECT c.profile_id,c.field_key,c.current_source_type AS source,r.observed_at,o.payload_json AS value
        FROM profile_field_current c JOIN profile_field_revisions r ON r.id=c.current_revision_id
        JOIN content_objects o ON o.id=r.content_object_id
        WHERE c.profile_id=ANY(CAST(:ids AS uuid[]))
          AND (c.field_key=ANY(:names) OR c.current_source_type IN ('manual','monica'))
        ORDER BY c.profile_id,(c.current_source_type='manual') DESC,(c.field_key='monica.custom_fields') DESC,c.field_key"""),
                      {"ids": ids, "names": list(AI_NAME_FIELDS)}).mappings()
    for row in rows:
        context = contexts.get(str(row["profile_id"]))
        if context is None:
            continue
        key, value = row["field_key"], row["value"]
        if key == "wechat.group_nicknames":
            for line in (_text(value) or "").splitlines():
                parts = GROUP_NAME_SEPARATOR.split(line.strip(), maxsplit=1)
                if parts[-1].strip():
                    context["group_names"].append({"group": (parts[0] if full else excerpt(parts[0], 120)) if len(parts) == 2 else None,
                                                   "name": parts[-1] if full else excerpt(parts[-1], 240)})
            if not full:
                context["group_names"] = context["group_names"][:4 if compact else 12]
        elif key in AI_NAME_FIELDS:
            if name := _text(value):
                context["names"].append({"source": AI_NAME_FIELDS[key], "value": name if full else name[:240]})
        elif key not in ("display_name", "summary"):
            entries = value.items() if key == "monica.custom_fields" and isinstance(value, dict) else [(key, value)]
            for label, raw in entries:
                if value_text := _detail_text(raw):
                    shortened = value_text if full or len(value_text) <= length else value_text[:length - 1].rstrip() + "…"
                    context["details"].append({"label": str(label)[:100], "value": shortened,
                                               "source": row["source"], "date": row["observed_at"].date().isoformat()})
    for context in contexts.values():
        # Keep deliberate notes and education/work details ahead of imported URLs and counters.
        context["details"].sort(key=lambda item: (item["source"] != "manual", not any(
            word in item["label"].casefold() for word in ("备注", "教育", "经历", "职业", "工作", "简介", "note", "education", "about"))))
        if details_limit is not None:
            context["details"] = context["details"][:details_limit]
    rows = db.execute(text("""WITH ranked AS (
        SELECT ap.profile_id,a.id,a.title,a.body,a.occurred_at,a.source_type,
          row_number() OVER(PARTITION BY ap.profile_id ORDER BY a.occurred_at DESC,a.id) AS n
        FROM activities a JOIN activity_participants ap ON ap.activity_id=a.id
        WHERE ap.profile_id=ANY(CAST(:ids AS uuid[])) AND a.activity_type='note' AND a.source_type IN ('manual','monica'))
        SELECT * FROM ranked WHERE CAST(:limit AS bigint) IS NULL OR n<=:limit ORDER BY profile_id,n"""),
                      {"ids": ids, "limit": None if full else 1 if compact else 6}).mappings()
    for row in rows:
        if context := contexts.get(str(row["profile_id"])):
            context["notes"].append({"title": row["title"] if full else excerpt(row["title"], 200),
                                     "body": row["body"] if full else excerpt(row["body"], length),
                                     "date": row["occurred_at"].date().isoformat(), "source": row["source_type"]})
    return contexts
