from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


UTC = timezone.utc
BEIJING_UTC_OFFSET = timedelta(hours=8)
BEIJING_TIMEZONE = timezone(BEIJING_UTC_OFFSET, name="UTC+08:00")


def _utc_now_aware() -> datetime:
    return datetime.now(UTC)


def _require_naive_datetime(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is not None:
        raise ValueError(f"{name} must be timezone-naive")
    return value


def _require_date(value: date, *, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be a date")
    return value


def utc_now_naive() -> datetime:
    """Return the current UTC instant for UTC-naive database columns."""

    return _utc_now_aware().replace(tzinfo=None)


def beijing_now_naive() -> datetime:
    """Return the current fixed UTC+08:00 wall time without tzinfo."""

    return _utc_now_aware().astimezone(BEIJING_TIMEZONE).replace(tzinfo=None)


def beijing_today() -> date:
    """Return the current business date in fixed UTC+08:00."""

    return beijing_now_naive().date()


def utc_naive_to_api(value: datetime) -> str:
    """Serialize a UTC-naive database datetime with an explicit Z suffix."""

    naive = _require_naive_datetime(value, name="value")
    return naive.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def beijing_naive_to_api(value: datetime) -> str:
    """Serialize a Beijing-naive datetime with an explicit +08:00 suffix."""

    naive = _require_naive_datetime(value, name="value")
    return naive.replace(tzinfo=BEIJING_TIMEZONE).isoformat()


def utc_naive_to_beijing_date(value: datetime) -> date:
    """Convert a UTC-naive database datetime to its fixed UTC+08:00 date."""

    naive = _require_naive_datetime(value, name="value")
    return naive.replace(tzinfo=UTC).astimezone(BEIJING_TIMEZONE).date()


def beijing_date_bounds_utc_naive(value: date) -> tuple[datetime, datetime]:
    """Return the half-open UTC-naive bounds for one Beijing business date."""

    business_date = _require_date(value, name="value")
    start_beijing = datetime.combine(
        business_date,
        datetime.min.time(),
        tzinfo=BEIJING_TIMEZONE,
    )
    end_beijing = start_beijing + timedelta(days=1)
    return (
        start_beijing.astimezone(UTC).replace(tzinfo=None),
        end_beijing.astimezone(UTC).replace(tzinfo=None),
    )
