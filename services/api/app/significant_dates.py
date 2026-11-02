"""Significant dates per person: the birthday plus anniversaries and other dates worth remembering.

Each date is exact, month-and-day, or an approximate partial date. Only a
birthday must lie in the past; an anniversary or other date may be planned.
"""

from calendar import monthrange
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .profile_facts import next_occurrence

KINDS = ("birthday", "anniversary", "other")
PRECISIONS = ("month_day", "approximate", "exact")
COLUMNS = "id, kind, label, date_precision AS precision, year, month, day"


class DateError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_date(*, kind: str, label: str | None, precision: str, year: int | None, month: int | None,
                   day: int | None, today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    if kind not in KINDS:
        raise DateError("kind")
    if precision not in PRECISIONS:
        raise DateError("precision")
    label = (label or "").strip() or None
    if kind == "birthday":
        label = None
    elif label and len(label) > 60:
        raise DateError("label_length")
    year = _part("year", year, 1800, 2200)
    month = _part("month", month, 1, 12)
    day = _part("day", day, 1, 31)
    if precision == "exact":
        if year is None or month is None or day is None:
            raise DateError("missing_exact_parts")
        _validate_day(year, month, day)
    elif precision == "month_day":
        if year is not None:
            raise DateError("month_day_has_year")
        if month is None or day is None:
            raise DateError("missing_month_day")
        _validate_day(None, month, day)
    else:
        if year is None and month is None:
            raise DateError("approximate_empty")
        if day is not None and month is None:
            raise DateError("day_without_month")
        if month is not None and day is not None:
            _validate_day(year, month, day)
    if kind == "birthday" and _is_future(year, month, day, today):
        raise DateError("future")
    return {"kind": kind, "label": label, "precision": precision, "year": year, "month": month, "day": day}


def present_date(parts: dict, today: date | None = None) -> dict:
    """Add the years elapsed (age or anniversary count) and the next yearly occurrence."""
    today = today or datetime.now(UTC).date()
    year, month, day, precision = parts["year"], parts["month"], parts["day"], parts["precision"]
    upcoming = next_occurrence(month, day, today) if month and day else None
    years = _years_since(year, month, day, today) if year is not None and parts["kind"] != "other" else None
    return {
        **({"id": str(parts["id"])} if parts.get("id") else {}),
        "kind": parts["kind"], "label": parts.get("label"),
        "precision": precision, "year": year, "month": month, "day": day,
        "years": years if years is None or years >= 0 else None,
        "years_exact": precision == "exact" and years is not None and years >= 0,
        "next": upcoming.isoformat() if upcoming else None,
        "days_until": (upcoming - today).days if upcoming else None,
    }


def list_dates(db: Session, profile_id: UUID) -> list[dict]:
    rows = db.execute(text(f"""SELECT {COLUMNS} FROM profile_dates WHERE profile_id = :id
        ORDER BY kind = 'birthday' DESC, month NULLS LAST, day NULLS LAST, created_at"""), {"id": profile_id}).mappings()
    return [present_date(dict(row)) for row in rows]


def birthday_of(dates: list[dict]) -> dict | None:
    return next((item for item in dates if item["kind"] == "birthday"), None)


SAME_DATE = """profile_id = :profile_id AND kind = :kind AND COALESCE(label, '') = COALESCE(:label, '')
    AND date_precision = :precision AND COALESCE(year, 0) = COALESCE(:year, 0)
    AND COALESCE(month, 0) = COALESCE(:month, 0) AND COALESCE(day, 0) = COALESCE(:day, 0)"""


def create_date(db: Session, profile_id: UUID, user_id: UUID | None, **parts) -> dict:
    """Add a date; submitting the same date again returns the one already stored."""
    saved = {**normalize_date(**parts), "profile_id": profile_id}
    try:
        with db.begin_nested():
            row = db.execute(text(f"""INSERT INTO profile_dates
                (profile_id, kind, label, date_precision, year, month, day, updated_by)
                VALUES (:profile_id, :kind, :label, :precision, :year, :month, :day, :user_id)
                RETURNING {COLUMNS}"""), {**saved, "user_id": user_id}).mappings().one()
    except IntegrityError as exc:
        row = db.execute(text(f"SELECT {COLUMNS} FROM profile_dates WHERE {SAME_DATE}"), saved).mappings().one_or_none()
        if row is None:
            raise DateError("birthday_exists") from exc
    return present_date(dict(row))


def update_date(db: Session, profile_id: UUID, date_id: UUID, user_id: UUID | None, **parts) -> dict:
    saved = normalize_date(**parts)
    try:
        with db.begin_nested():
            row = db.execute(text(f"""UPDATE profile_dates SET kind = :kind, label = :label,
                  date_precision = :precision, year = :year, month = :month, day = :day,
                  updated_at = now(), updated_by = :user_id
                WHERE id = :date_id AND profile_id = :profile_id RETURNING {COLUMNS}"""),
                {**saved, "profile_id": profile_id, "date_id": date_id, "user_id": user_id}).mappings().one_or_none()
    except IntegrityError as exc:
        raise DateError("birthday_exists") from exc
    if row is None:
        raise DateError("not_found")
    return present_date(dict(row))


def delete_date(db: Session, profile_id: UUID, date_id: UUID) -> bool:
    return db.execute(text("DELETE FROM profile_dates WHERE id = :date_id AND profile_id = :profile_id"),
                      {"date_id": date_id, "profile_id": profile_id}).rowcount > 0


def _part(name: str, value: int | None, low: int, high: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DateError("year_range" if name == "year" else "invalid_day")
    return value


def _validate_day(year: int | None, month: int, day: int) -> None:
    if day > monthrange(year if year is not None else 2024, month)[1]:
        raise DateError("invalid_day")


def _is_future(year: int | None, month: int | None, day: int | None, today: date) -> bool:
    if year is None:
        return False
    if year > today.year:
        return True
    if year < today.year or month is None:
        return False
    if month > today.month:
        return True
    return day is not None and month == today.month and day > today.day


def _years_since(year: int, month: int | None, day: int | None, today: date) -> int:
    years = today.year - year
    if month is not None and (today.month, today.day if day is not None else 1) < (month, day or 1):
        years -= 1
    return years
