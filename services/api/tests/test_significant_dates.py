from datetime import date

import pytest

from app.significant_dates import DateError, normalize_date, present_date

TODAY = date(2026, 9, 21)


def shown(kind="birthday", label=None, **parts):
    return present_date(normalize_date(kind=kind, label=label, today=TODAY, **parts), TODAY)


def test_exact_birthday_includes_age_and_next_occurrence():
    saved = shown(precision="exact", year=1990, month=3, day=15)
    assert saved == {"kind": "birthday", "label": None, "precision": "exact", "year": 1990, "month": 3, "day": 15,
                     "years": 36, "years_exact": True, "next": "2027-03-15", "days_until": 175}


def test_exact_date_before_this_years_occurrence_counts_one_year_fewer():
    saved = shown(precision="exact", year=1990, month=12, day=1)
    assert saved["years"] == 35 and saved["years_exact"] is True and saved["days_until"] == 71


def test_month_and_day_keep_the_year_empty():
    saved = shown(precision="month_day", year=None, month=12, day=23)
    assert saved["year"] is None and saved["years"] is None and saved["years_exact"] is False


def test_approximate_year_month_and_full_date_stay_partial():
    year_only = shown(precision="approximate", year=1990, month=None, day=None)
    year_month = shown(precision="approximate", year=1990, month=9, day=None)
    rough_day = shown(precision="approximate", year=1990, month=9, day=21)
    assert year_only["years"] == 36 and year_only["years_exact"] is False and year_only["next"] is None
    assert (year_month["year"], year_month["month"], year_month["day"]) == (1990, 9, None)
    assert rough_day["years_exact"] is False and rough_day["years"] == 36 and rough_day["days_until"] == 0


def test_february_29_is_allowed_without_a_year_and_rejected_on_non_leap_years():
    assert shown(precision="month_day", year=None, month=2, day=29)["day"] == 29
    assert shown(precision="exact", year=2000, month=2, day=29)["year"] == 2000
    with pytest.raises(DateError, match="invalid_day"):
        shown(precision="exact", year=1990, month=2, day=29)
    with pytest.raises(DateError, match="invalid_day"):
        shown(precision="approximate", year=1990, month=4, day=31)


def test_incomplete_dates_and_future_birthdays_are_rejected():
    with pytest.raises(DateError, match="missing_exact_parts"):
        shown(precision="exact", year=1990, month=3, day=None)
    with pytest.raises(DateError, match="month_day_has_year"):
        shown(precision="month_day", year=1990, month=3, day=15)
    with pytest.raises(DateError, match="approximate_empty"):
        shown(precision="approximate", year=None, month=None, day=None)
    with pytest.raises(DateError, match="day_without_month"):
        shown(precision="approximate", year=1990, month=None, day=15)
    with pytest.raises(DateError, match="future"):
        shown(precision="exact", year=2026, month=12, day=1)
    with pytest.raises(DateError, match="future"):
        shown(precision="approximate", year=2027, month=None, day=None)


def test_anniversaries_count_years_and_other_dates_may_be_planned():
    wedding = shown(kind="anniversary", label=" 结婚纪念日 ", precision="exact", year=2018, month=10, day=1)
    assert wedding["label"] == "结婚纪念日" and wedding["years"] == 7 and wedding["days_until"] == 10
    planned = shown(kind="other", label="预产期", precision="exact", year=2027, month=2, day=3)
    assert planned["years"] is None and planned["next"] == "2027-02-03"
    assert shown(label="ignored", precision="month_day", year=None, month=1, day=2)["label"] is None
    with pytest.raises(DateError, match="kind"):
        shown(kind="holiday", precision="month_day", year=None, month=1, day=2)
