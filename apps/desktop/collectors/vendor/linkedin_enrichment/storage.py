"""SQLite WAL queue, immutable archive, budget, and circuit breaker."""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .browser import AccessSignal
from .models import Target

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY,
  profile_url TEXT NOT NULL UNIQUE,
  external_id TEXT,
  metadata_json TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  profile_due_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  target_id INTEGER NOT NULL UNIQUE REFERENCES targets(id),
  state TEXT NOT NULL CHECK (state IN ('queued','leased','done')),
  available_at TEXT NOT NULL,
  lease_until TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(state, available_at, lease_until);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY,
  target_id INTEGER NOT NULL REFERENCES targets(id),
  kind TEXT NOT NULL CHECK (kind IN ('profile','posts','mutuals')),
  content_hash TEXT NOT NULL,
  canonical_json TEXT NOT NULL,
  complete INTEGER NOT NULL CHECK (complete IN (0,1)),
  captured_at TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  UNIQUE(target_id, kind, content_hash)
);
CREATE INDEX IF NOT EXISTS snapshots_latest
  ON snapshots(target_id, kind, captured_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS field_changes (
  id INTEGER PRIMARY KEY,
  target_id INTEGER NOT NULL REFERENCES targets(id),
  kind TEXT NOT NULL,
  snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
  field_path TEXT NOT NULL,
  change_type TEXT NOT NULL CHECK (change_type IN ('add','update','delete')),
  old_json TEXT,
  new_json TEXT,
  changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_budget (
  budget_date TEXT PRIMARY KEY,
  consumed INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_events (
  id INTEGER PRIMARY KEY,
  target_id INTEGER NOT NULL REFERENCES targets(id),
  claimed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS claim_events_window ON claim_events(claimed_at);
CREATE TABLE IF NOT EXISTS circuit_breaker (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  paused_until TEXT,
  reason TEXT,
  failures INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
  source_url TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  media_type TEXT NOT NULL,
  byte_count INTEGER NOT NULL,
  local_path TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_hash ON assets(content_hash);
CREATE TABLE IF NOT EXISTS conversations (
  conversation_urn TEXT PRIMARY KEY,
  backend_urn TEXT,
  title TEXT,
  participants_json TEXT NOT NULL,
  created_at TEXT,
  last_activity_at TEXT,
  last_read_at TEXT,
  unread_count INTEGER NOT NULL DEFAULT 0,
  is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0,1)),
  checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_activity
  ON conversations(last_activity_at DESC);
CREATE TABLE IF NOT EXISTS messages (
  message_urn TEXT PRIMARY KEY,
  conversation_urn TEXT NOT NULL REFERENCES conversations(conversation_urn),
  sender_urn TEXT,
  delivered_at TEXT,
  subject TEXT,
  body TEXT,
  incoming INTEGER NOT NULL CHECK (incoming IN (0,1)),
  observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation
  ON messages(conversation_urn, delivered_at);
"""

_MISSING = object()
COLLECTOR_VERSION = "0.1.0+joeyism-b1cdc1c0"
_MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ),
        1,
    )
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def canonical_json(data: Any) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


def linkedin_month(value: Any) -> str | None:
    """Normalize LinkedIn's display date without inventing day precision."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    exact = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-\d{1,2})?", text)
    if exact:
        month = exact.group(2)
        return f"{exact.group(1)}-{int(month):02d}" if month else exact.group(1)
    named = re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b",
        text,
        re.I,
    )
    if named:
        return f"{named.group(2)}-{_MONTHS[named.group(1)[:3].lower()]:02d}"
    year = re.search(r"\b(19|20)\d{2}\b", text)
    return year.group(0) if year else None


def _education_description(row: Mapping[str, Any]) -> str | None:
    parts = []
    if row.get("description"):
        parts.append(str(row["description"]).strip())
    if row.get("activities"):
        parts.append(f"Activities: {str(row['activities']).strip()}")
    if row.get("grade"):
        parts.append(f"Grade: {str(row['grade']).strip()}")
    return "\n\n".join(part for part in parts if part) or None


def activity_urn_posted_at(urn: Any) -> str | None:
    """Decode LinkedIn's activity Snowflake timestamp as UTC ISO-8601."""

    if not isinstance(urn, str):
        return None
    match = re.fullmatch(r"urn:li:(?:activity|share|ugcPost):(\d+)", urn)
    if not match:
        return None
    milliseconds = int(match.group(1)) >> 22
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat()


def projector_profile(data: Mapping[str, Any]) -> dict[str, Any]:
    """Translate pinned-upstream field names into the Monica payload contract."""

    experiences = []
    experience_indexes: dict[tuple[Any, ...], int] = {}
    for row in data.get("experiences", []):
        to_date = row.get("to_date")
        is_current = not to_date or (
            isinstance(to_date, str)
            and to_date.strip().lower() in {"present", "current", "至今", "现在"}
        )
        experience = {
            "title": row.get("position_title"),
            "company_name": row.get("institution_name"),
            "location": row.get("location"),
            "description": row.get("description"),
            "start_date": linkedin_month(row.get("from_date")),
            "end_date": linkedin_month(to_date),
            "is_current": is_current,
            "linkedin_url": row.get("linkedin_url"),
        }
        identity = tuple(
            experience[field]
            for field in (
                "title",
                "company_name",
                "location",
                "start_date",
                "end_date",
            )
        )
        if identity in experience_indexes:
            existing = experiences[experience_indexes[identity]]
            existing["is_current"] = existing["is_current"] or is_current
            for field in ("description", "linkedin_url"):
                if len(str(experience[field] or "")) > len(str(existing[field] or "")):
                    existing[field] = experience[field]
            continue
        experience_indexes[identity] = len(experiences)
        experiences.append(experience)
    educations = []
    for row in data.get("educations", []):
        to_date = row.get("to_date")
        is_current = not to_date or (
            isinstance(to_date, str)
            and to_date.strip().lower() in {"present", "current", "至今", "现在"}
        )
        education = {
            "school_name": row.get("institution_name"),
            "degree": row.get("degree_name") or row.get("degree"),
            "field_of_study": row.get("field_of_study"),
            "description": _education_description(row),
            "start_date": linkedin_month(row.get("from_date")),
            "end_date": linkedin_month(to_date),
            "is_current": is_current,
            "linkedin_url": row.get("school_url") or row.get("linkedin_url"),
        }
        if row.get("education_urn"):
            education["source_key"] = row["education_urn"]
        educations.append(education)
    return {
        "name": data.get("name"),
        "location": data.get("location"),
        "about": data.get("about"),
        "avatar_url": data.get("avatar_url"),
        "avatar_asset": data.get("avatar_asset"),
        "avatar_local_url": (
            data.get("avatar_asset", {}).get("local_url")
            if isinstance(data.get("avatar_asset"), Mapping)
            else None
        ),
        "background_url": data.get("background_url"),
        "background_asset": data.get("background_asset"),
        "background_local_url": (
            data.get("background_asset", {}).get("local_url")
            if isinstance(data.get("background_asset"), Mapping)
            else None
        ),
        "open_to_work": data.get("open_to_work"),
        "experiences": experiences,
        "educations": educations,
    }


def projector_posts(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if row.get("is_authored") is False:
            continue
        urn = row.get("urn")
        image_urls = [
            url
            for url in row.get("image_urls", [])
            if isinstance(url, str)
            and not any(
                marker in url.casefold()
                for marker in (
                    "profile-displayphoto",
                    "company-logo",
                    "school-logo",
                )
            )
        ]
        image_assets = [
            asset
            for asset in row.get("image_assets", [])
            if isinstance(asset, Mapping)
            and asset.get("source_url") in image_urls
        ]
        projected = {
            "provider_post_id": urn,
            "url": row.get("linkedin_url"),
            "text": row.get("text"),
            "posted_at": activity_urn_posted_at(urn),
            "posted_label": row.get("posted_date"),
            "reaction_count": row.get("reactions_count"),
            "comment_count": row.get("comments_count"),
            "repost_count": row.get("reposts_count"),
            "image_urls": image_urls,
            "image_assets": image_assets,
            "video_url": row.get("video_url"),
            "video_asset": row.get("video_asset"),
            "is_authored": row.get("is_authored"),
            "activity_type": row.get("activity_type", "unknown"),
        }
        if urn:
            projected["source_key"] = urn
        result.append(projected)
    return result


def iter_changes(
    old: Any,
    new: Any,
    *,
    complete: bool,
    path: str = "",
) -> Iterator[tuple[str, str, Any, Any]]:
    """Yield recursive changes, suppressing deletion from partial snapshots."""

    if isinstance(old, Mapping) and isinstance(new, Mapping):
        for key in sorted(old.keys() | new.keys()):
            child = f"{path}.{key}" if path else str(key)
            before = old.get(key, _MISSING)
            after = new.get(key, _MISSING)
            if before is _MISSING:
                yield child, "add", None, after
            elif after is _MISSING:
                if complete:
                    yield child, "delete", before, None
            else:
                yield from iter_changes(before, after, complete=complete, path=child)
        return
    if old != new:
        yield path or "$", "update", old, new


class Store:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            target_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(targets)")
            }
            if "active" not in target_columns:
                connection.execute(
                    "ALTER TABLE targets ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            if "profile_due_at" not in target_columns:
                connection.execute("ALTER TABLE targets ADD COLUMN profile_due_at TEXT")
            now = iso(utc_now())
            connection.execute(
                """INSERT OR IGNORE INTO circuit_breaker
                   (id, paused_until, reason, failures, updated_at)
                   VALUES (1, NULL, NULL, 0, ?)""",
                (now,),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self._secure_database_files()
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()
            self._secure_database_files()

    def _secure_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.path}{suffix}")
            if path.exists():
                os.chmod(path, 0o600)

    def enqueue(
        self,
        targets: list[Target],
        *,
        now: datetime | None = None,
        reconcile: bool = False,
        reconcile_metadata_key: str | None = None,
    ) -> int:
        timestamp = iso(now or utc_now())
        if reconcile and reconcile_metadata_key:
            raise ValueError(
                "reconcile and reconcile_metadata_key are mutually exclusive"
            )
        with self.connect() as connection:
            if reconcile:
                connection.execute(
                    "UPDATE targets SET active=0, updated_at=?",
                    (timestamp,),
                )
            elif reconcile_metadata_key:
                from .target_sources import reconcile_source
                reconcile_source(connection, targets, reconcile_metadata_key, timestamp)
            for target in targets:
                existing = connection.execute(
                    """SELECT external_id, metadata_json FROM targets
                       WHERE profile_url=?""",
                    (target.url,),
                ).fetchone()
                metadata = (
                    {
                        **json.loads(existing["metadata_json"]),
                        **target.metadata,
                    }
                    if existing
                    else target.metadata
                )
                external_id = target.external_id or (
                    existing["external_id"] if existing else None
                )
                connection.execute(
                    """INSERT INTO targets
                       (profile_url, external_id, metadata_json, active,
                        created_at, updated_at)
                       VALUES (?, ?, ?, 1, ?, ?)
                       ON CONFLICT(profile_url) DO UPDATE SET
                         external_id=excluded.external_id,
                         metadata_json=excluded.metadata_json,
                         active=1,
                         updated_at=excluded.updated_at""",
                    (
                        target.url,
                        external_id,
                        canonical_json(metadata),
                        timestamp,
                        timestamp,
                    ),
                )
                target_id = connection.execute(
                    "SELECT id FROM targets WHERE profile_url=?", (target.url,)
                ).fetchone()["id"]
                connection.execute(
                    """INSERT INTO jobs
                       (target_id, state, available_at, lease_until, attempts,
                        last_error, created_at, updated_at)
                       VALUES (?, 'queued', ?, NULL, 0, NULL, ?, ?)
                       ON CONFLICT(target_id) DO NOTHING""",
                    (target_id, timestamp, timestamp, timestamp),
                )
        return len(targets)

    def claim(
        self,
        *,
        window_cap: int = 3,
        window_hours: int = 2,
        rolling_cap: int = 36,
        rolling_hours: int = 24,
        lease_minutes: int = 30,
        profile_url: str | None = None,
        now: datetime | None = None,
    ) -> sqlite3.Row | None:
        current = now or utc_now()
        timestamp = iso(current)
        window_start = iso(current - timedelta(hours=window_hours))
        rolling_start = iso(current - timedelta(hours=rolling_hours))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            circuit = connection.execute(
                "SELECT paused_until FROM circuit_breaker WHERE id=1"
            ).fetchone()
            if circuit["paused_until"] and circuit["paused_until"] > timestamp:
                return None
            connection.execute(
                """UPDATE jobs SET state='queued', lease_until=NULL, updated_at=?
                   WHERE state='leased' AND lease_until <= ?""",
                (timestamp, timestamp),
            )
            consumed = connection.execute(
                "SELECT COUNT(*) consumed FROM claim_events WHERE claimed_at>?",
                (rolling_start,),
            ).fetchone()["consumed"]
            if consumed >= rolling_cap:
                return None
            window_consumed = connection.execute(
                "SELECT COUNT(*) consumed FROM claim_events WHERE claimed_at>?",
                (window_start,),
            ).fetchone()["consumed"]
            if window_consumed >= window_cap:
                return None
            profile_clause = " AND t.profile_url=?" if profile_url else ""
            parameters = (timestamp, profile_url) if profile_url else (timestamp,)
            job = connection.execute(
                f"""SELECT j.id AS job_id, j.target_id, t.profile_url,
                           t.external_id, t.metadata_json, t.profile_due_at
                    FROM jobs j JOIN targets t ON t.id=j.target_id
                    WHERE j.state='queued' AND t.active=1 AND j.available_at <= ?
                    {profile_clause}
                    ORDER BY CASE
                      WHEN NOT EXISTS (
                        SELECT 1 FROM snapshots s WHERE s.target_id=t.id
                          AND s.kind='profile' AND s.complete=1
                      ) THEN 0
                      WHEN j.last_error IS NOT NULL THEN 1
                      WHEN (t.profile_due_at IS NULL OR t.profile_due_at<=?)
                        AND COALESCE(json_extract((
                          SELECT s.canonical_json FROM snapshots s
                          WHERE s.target_id=t.id AND s.kind='profile'
                          ORDER BY s.checked_at DESC,s.id DESC LIMIT 1
                        ), '$.avatar_url'),'')='' THEN 2
                      ELSE 3 END,
                      j.available_at, j.created_at, j.id LIMIT 1""",
                (*parameters, timestamp),
            ).fetchone()
            if job is None:
                return None
            lease_until = iso(current + timedelta(minutes=lease_minutes))
            connection.execute(
                """UPDATE jobs SET state='leased', lease_until=?, attempts=attempts+1,
                   updated_at=? WHERE id=?""",
                (lease_until, timestamp, job["job_id"]),
            )
            connection.execute(
                "INSERT INTO claim_events(target_id, claimed_at) VALUES (?,?)",
                (job["target_id"], timestamp),
            )
            return job

    def complete_job(
        self,
        job_id: int,
        *,
        now: datetime | None = None,
        next_run_at: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        timestamp = iso(current)
        due = next_run_at or current + timedelta(hours=20 + secrets.randbelow(9))
        with self.connect() as connection:
            connection.execute(
                """UPDATE jobs SET state='queued', available_at=?, lease_until=NULL,
                   last_error=NULL, updated_at=? WHERE id=?""",
                (iso(due), timestamp, job_id),
            )

    def mark_profile_collected(
        self,
        target_id: int,
        *,
        now: datetime | None = None,
        next_run_at: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        due = next_run_at or current + timedelta(days=14 + secrets.randbelow(15))
        with self.connect() as connection:
            connection.execute(
                """UPDATE targets SET profile_due_at=?, updated_at=?
                   WHERE id=?""",
                (iso(due), iso(current), target_id),
            )

    def fail_job(
        self,
        job_id: int,
        error: str,
        *,
        retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        with self.connect() as connection:
            connection.execute(
                """UPDATE jobs SET state='queued', available_at=?, lease_until=NULL,
                   last_error=?, updated_at=? WHERE id=?""",
                (
                    iso(retry_at or current + timedelta(hours=24)),
                    error,
                    iso(current),
                    job_id,
                ),
            )

    def trip_circuit(
        self,
        signal: AccessSignal,
        *,
        now: datetime | None = None,
    ) -> datetime:
        current = now or utc_now()
        hours = {
            AccessSignal.AUTH: 24,
            AccessSignal.CHECKPOINT: 48,
            AccessSignal.RATE_LIMIT: 72,
            AccessSignal.SELECTOR_DRIFT: 24,
        }[signal]
        paused_until = current + timedelta(hours=hours)
        with self.connect() as connection:
            connection.execute(
                """UPDATE circuit_breaker SET paused_until=?, reason=?,
                   failures=failures+1, updated_at=? WHERE id=1""",
                (iso(paused_until), signal.value, iso(current)),
            )
        return paused_until

    def archive_snapshot(
        self,
        target_id: int,
        kind: str,
        data: Any,
        *,
        complete: bool = True,
        now: datetime | None = None,
    ) -> tuple[int, bool]:
        current = now or utc_now()
        timestamp = iso(current)
        encoded = canonical_json(data)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as connection:
            duplicate = connection.execute(
                """SELECT id FROM snapshots
                   WHERE target_id=? AND kind=? AND content_hash=?""",
                (target_id, kind, digest),
            ).fetchone()
            if duplicate:
                connection.execute(
                    "UPDATE snapshots SET checked_at=? WHERE id=?",
                    (timestamp, duplicate["id"]),
                )
                return duplicate["id"], False
            previous = connection.execute(
                """SELECT canonical_json FROM snapshots
                   WHERE target_id=? AND kind=?
                   ORDER BY captured_at DESC, id DESC LIMIT 1""",
                (target_id, kind),
            ).fetchone()
            cursor = connection.execute(
                """INSERT INTO snapshots
                   (target_id, kind, content_hash, canonical_json, complete,
                    captured_at, checked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (target_id, kind, digest, encoded, int(complete), timestamp, timestamp),
            )
            snapshot_id = cursor.lastrowid
            if previous:
                old = json.loads(previous["canonical_json"])
                for path, change_type, before, after in iter_changes(
                    old, data, complete=complete
                ):
                    connection.execute(
                        """INSERT INTO field_changes
                           (target_id, kind, snapshot_id, field_path, change_type,
                            old_json, new_json, changed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            target_id,
                            kind,
                            snapshot_id,
                            path,
                            change_type,
                            None if before is None else canonical_json(before),
                            None if after is None else canonical_json(after),
                            timestamp,
                        ),
                    )
            return snapshot_id, True

    def known_post_urns(self, target_id: int) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT canonical_json FROM snapshots
                   WHERE target_id=? AND kind='posts'
                   ORDER BY captured_at DESC, id DESC""",
                (target_id,),
            ).fetchall()
        return {
            post["urn"]
            for row in rows
            for post in json.loads(row["canonical_json"])
            if post.get("urn")
        }

    def asset_for_url(self, source_url: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """SELECT source_url, content_hash, media_type, byte_count,
                          local_path, captured_at, checked_at
                   FROM assets WHERE source_url=?""",
                (source_url,),
            ).fetchone()

    def record_asset(
        self,
        *,
        source_url: str,
        content_hash: str,
        media_type: str,
        byte_count: int,
        local_path: str,
        captured_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO assets
                   (source_url, content_hash, media_type, byte_count, local_path,
                    captured_at, checked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_url) DO UPDATE SET
                     content_hash=excluded.content_hash,
                     media_type=excluded.media_type,
                     byte_count=excluded.byte_count,
                     local_path=excluded.local_path,
                     checked_at=excluded.checked_at""",
                (
                    source_url,
                    content_hash,
                    media_type,
                    byte_count,
                    local_path,
                    captured_at,
                    captured_at,
                ),
            )

    def asset_urls(self) -> list[str]:
        urls: set[str] = set()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT canonical_json FROM snapshots ORDER BY id"
            ).fetchall()
        for row in rows:
            payload = json.loads(row["canonical_json"])
            values = payload if isinstance(payload, list) else [payload]
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                for key in ("avatar_url", "background_url", "video_url"):
                    asset_url = value.get(key)
                    if isinstance(asset_url, str):
                        urls.add(asset_url)
                image_urls = value.get("image_urls")
                if isinstance(image_urls, list):
                    urls.update(url for url in image_urls if isinstance(url, str))
        return sorted(urls)

    def store_conversations(self, conversations: list[Mapping[str, Any]]) -> None:
        observed_at = iso(utc_now())
        with self.connect() as connection:
            for conversation in conversations:
                connection.execute(
                    """INSERT INTO conversations
                       (conversation_urn, backend_urn, title, participants_json,
                        created_at, last_activity_at, last_read_at, unread_count,
                        is_read, checked_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(conversation_urn) DO UPDATE SET
                         backend_urn=excluded.backend_urn,
                         title=excluded.title,
                         participants_json=excluded.participants_json,
                         last_activity_at=excluded.last_activity_at,
                         last_read_at=excluded.last_read_at,
                         unread_count=excluded.unread_count,
                         is_read=excluded.is_read,
                         checked_at=excluded.checked_at""",
                    (
                        conversation["conversation_urn"],
                        conversation.get("backend_urn"),
                        conversation.get("title"),
                        canonical_json(conversation.get("participants", [])),
                        conversation.get("created_at"),
                        conversation.get("last_activity_at"),
                        conversation.get("last_read_at"),
                        conversation.get("unread_count", 0),
                        int(bool(conversation.get("read"))),
                        observed_at,
                    ),
                )
                messages = conversation.get("messages")
                if not isinstance(messages, list):
                    message = conversation.get("message")
                    messages = [message] if isinstance(message, Mapping) else []
                for message in messages:
                    if not isinstance(message, Mapping):
                        continue
                    connection.execute(
                        """INSERT INTO messages
                           (message_urn, conversation_urn, sender_urn,
                            delivered_at, subject, body, incoming, observed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(message_urn) DO UPDATE SET
                             subject=excluded.subject,
                             body=excluded.body,
                             observed_at=excluded.observed_at""",
                        (
                            message["message_urn"],
                            conversation["conversation_urn"],
                            message.get("sender_urn"),
                            message.get("delivered_at"),
                            message.get("subject"),
                            message.get("body"),
                            int(bool(message.get("incoming"))),
                            observed_at,
                        ),
                    )

    def export_conversations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            conversations = connection.execute(
                """SELECT * FROM conversations
                   ORDER BY last_activity_at DESC, conversation_urn"""
            ).fetchall()
            result = []
            for conversation in conversations:
                messages = connection.execute(
                    """SELECT message_urn, sender_urn, delivered_at, subject,
                              body, incoming, observed_at
                       FROM messages WHERE conversation_urn=?
                       ORDER BY delivered_at, message_urn""",
                    (conversation["conversation_urn"],),
                ).fetchall()
                result.append(
                    {
                        "conversation_urn": conversation["conversation_urn"],
                        "backend_urn": conversation["backend_urn"],
                        "title": conversation["title"],
                        "participants": json.loads(
                            conversation["participants_json"]
                        ),
                        "created_at": conversation["created_at"],
                        "last_activity_at": conversation["last_activity_at"],
                        "last_read_at": conversation["last_read_at"],
                        "unread_count": conversation["unread_count"],
                        "read": bool(conversation["is_read"]),
                        "messages": [
                            {
                                **dict(message),
                                "incoming": bool(message["incoming"]),
                            }
                            for message in messages
                        ],
                    }
                )
        return result

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or utc_now()
        with self.connect() as connection:
            jobs = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) count FROM jobs GROUP BY state"
                )
            }
            budget = connection.execute(
                """SELECT COUNT(*) consumed FROM claim_events
                   WHERE claimed_at>?""",
                (iso(current - timedelta(hours=24)),),
            ).fetchone()
            window_budget = connection.execute(
                """SELECT COUNT(*) consumed FROM claim_events
                   WHERE claimed_at>?""",
                (iso(current - timedelta(hours=2)),),
            ).fetchone()
            circuit = dict(
                connection.execute(
                    """SELECT paused_until, reason, failures
                       FROM circuit_breaker WHERE id=1"""
                ).fetchone()
            )
            snapshots = connection.execute(
                "SELECT COUNT(*) count FROM snapshots"
            ).fetchone()["count"]
            active_targets = connection.execute(
                "SELECT COUNT(*) count FROM targets WHERE active=1"
            ).fetchone()["count"]
            profile_collected = connection.execute(
                """SELECT COUNT(*) count FROM targets t
                   WHERE t.active=1
                     AND EXISTS (
                       SELECT 1 FROM snapshots s
                       WHERE s.target_id=t.id AND s.kind='profile'
                     )"""
            ).fetchone()["count"]
            asset_totals = connection.execute(
                "SELECT COUNT(*) count, COALESCE(SUM(byte_count), 0) bytes FROM assets"
            ).fetchone()
            conversation_count = connection.execute(
                "SELECT COUNT(*) count FROM conversations"
            ).fetchone()["count"]
            message_count = connection.execute(
                "SELECT COUNT(*) count FROM messages"
            ).fetchone()["count"]
            unread_conversations = connection.execute(
                "SELECT COUNT(*) count FROM conversations WHERE unread_count>0"
            ).fetchone()["count"]
        return {
            "jobs": jobs,
            "snapshots": snapshots,
            "active_targets": active_targets,
            "profile_collected": profile_collected,
            "assets_archived": asset_totals["count"],
            "asset_bytes": asset_totals["bytes"],
            "conversations": conversation_count,
            "messages": message_count,
            "unread_conversations": unread_conversations,
            "rolling_24h_consumed": budget["consumed"],
            "rolling_2h_consumed": window_budget["consumed"],
            "circuit": circuit,
        }

    def export_payload(
        self,
        *,
        as_of: datetime | None = None,
        snapshot_hash: str | None = None,
        include_unlinked: bool = False,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            assets = {
                row["source_url"]: {
                    "source_url": row["source_url"],
                    "sha256": row["content_hash"],
                    "media_type": row["media_type"],
                    "byte_count": row["byte_count"],
                    "local_url": f"/storage/linkedin-assets/{row['local_path']}",
                    "captured_at": row["captured_at"],
                }
                for row in connection.execute("SELECT * FROM assets")
            }
            target_id: int | None = None
            if snapshot_hash is not None:
                selected = connection.execute(
                    """SELECT target_id, captured_at FROM snapshots
                       WHERE content_hash=? ORDER BY id DESC LIMIT 1""",
                    (snapshot_hash,),
                ).fetchone()
                if selected is None:
                    raise ValueError(f"unknown snapshot hash: {snapshot_hash}")
                target_id = selected["target_id"]
                as_of = datetime.fromisoformat(selected["captured_at"])
            targets = connection.execute(
                """SELECT * FROM targets
                   WHERE (? IS NOT NULL OR active=1)
                     AND (? IS NULL OR id=?)
                   ORDER BY id""",
                (target_id, target_id, target_id),
            ).fetchall()
            profiles = []
            for target in targets:
                cutoff = iso(as_of) if as_of else None
                snapshot_rows = connection.execute(
                    """SELECT id, kind, content_hash, canonical_json, complete,
                              captured_at, checked_at
                       FROM snapshots
                       WHERE target_id=? AND (? IS NULL OR captured_at <= ?)
                       ORDER BY captured_at, id""",
                    (target["id"], cutoff, cutoff),
                ).fetchall()
                if not snapshot_rows:
                    continue
                latest: dict[str, sqlite3.Row] = {}
                for row in snapshot_rows:
                    latest[row["kind"]] = row
                if "profile" not in latest:
                    continue
                metadata = json.loads(target["metadata_json"])
                provider_user_id = (
                    metadata.get("provider_user_id") or target["external_id"]
                )
                contact_id = metadata.get("contact_id")
                if not provider_user_id or (
                    not include_unlinked and not isinstance(contact_id, int)
                ):
                    # A one-off canary can be archived without being projected.
                    continue
                profile_data = json.loads(latest["profile"]["canonical_json"])
                avatar_url = profile_data.get("avatar_url")
                if isinstance(avatar_url, str) and avatar_url in assets:
                    profile_data["avatar_asset"] = assets[avatar_url]
                background_url = profile_data.get("background_url")
                if isinstance(background_url, str) and background_url in assets:
                    profile_data["background_asset"] = assets[background_url]
                posts_data = (
                    json.loads(latest["posts"]["canonical_json"])
                    if "posts" in latest
                    else []
                )
                for post in posts_data:
                    image_urls = post.get("image_urls")
                    if isinstance(image_urls, list):
                        post["image_assets"] = [
                            assets[url]
                            for url in image_urls
                            if isinstance(url, str) and url in assets
                        ]
                    video_url = post.get("video_url")
                    if isinstance(video_url, str) and video_url in assets:
                        post["video_asset"] = assets[video_url]
                mutual_data = (
                    json.loads(latest["mutuals"]["canonical_json"])
                    if "mutuals" in latest
                    else {}
                )
                snapshots = [
                    {
                        "kind": row["kind"],
                        "hash": row["content_hash"],
                        "captured_at": row["captured_at"],
                        "partial": not bool(row["complete"]),
                        "payload": json.loads(row["canonical_json"]),
                    }
                    for row in snapshot_rows
                ]
                changes = []
                for row in connection.execute(
                    """SELECT s.content_hash, f.field_path, f.old_json, f.new_json
                       FROM field_changes f
                       JOIN snapshots s ON s.id=f.snapshot_id
                       WHERE f.target_id=? AND (? IS NULL OR s.captured_at <= ?)
                       ORDER BY f.changed_at, f.id""",
                    (target["id"], cutoff, cutoff),
                ):
                    changes.append(
                        {
                            "snapshot_hash": row["content_hash"],
                            "field_path": row["field_path"],
                            "before": (
                                None
                                if row["old_json"] is None
                                else json.loads(row["old_json"])
                            ),
                            "after": (
                                None
                                if row["new_json"] is None
                                else json.loads(row["new_json"])
                            ),
                        }
                    )
                captured_at = max(row["captured_at"] for row in latest.values())
                profiles.append(
                    {
                        "contact_id": contact_id,
                        "provider_user_id": provider_user_id,
                        "profile_url": target["profile_url"],
                        "captured_at": captured_at,
                        "collector_version": COLLECTOR_VERSION,
                        "partial": {
                            kind: kind not in latest
                            or not bool(latest[kind]["complete"])
                            for kind in ("profile", "posts", "mutuals")
                        },
                        "profile": projector_profile(profile_data),
                        "posts": projector_posts(posts_data),
                        "mutuals": {
                            "count": mutual_data.get("count") or 0,
                            "preview_names": mutual_data.get(
                                "preview_names", mutual_data.get("names", [])
                            ),
                            "connections": mutual_data.get("connections", []),
                            "browse_url": mutual_data.get("browse_url"),
                            "is_complete": bool(
                                mutual_data.get("is_complete", False)
                            ),
                        },
                        "snapshots": snapshots,
                        "changes": changes,
                    }
                )
        return {
            "schema_version": 1,
            "provider": "linkedin",
            "generated_at": iso(utc_now()),
            "profiles": profiles,
            "conversations": self.export_conversations(),
        }


def atomic_write_json(path: Path, payload: Any) -> None:
    """Durably replace a JSON export without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
