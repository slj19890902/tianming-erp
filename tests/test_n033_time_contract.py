from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core import time_contract


@pytest.mark.parametrize(
    ("utc_aware", "beijing_naive", "business_date"),
    [
        (
            datetime(2026, 7, 16, 16, 30, tzinfo=timezone.utc),
            datetime(2026, 7, 17, 0, 30),
            date(2026, 7, 17),
        ),
        (
            datetime(2026, 7, 16, 23, 59, tzinfo=timezone.utc),
            datetime(2026, 7, 17, 7, 59),
            date(2026, 7, 17),
        ),
        (
            datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 17, 8, 0),
            date(2026, 7, 17),
        ),
        (
            datetime(2026, 7, 17, 15, 59, tzinfo=timezone.utc),
            datetime(2026, 7, 17, 23, 59),
            date(2026, 7, 17),
        ),
        (
            datetime(2026, 7, 17, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 18, 0, 0),
            date(2026, 7, 18),
        ),
    ],
)
def test_now_helpers_cover_beijing_day_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    utc_aware: datetime,
    beijing_naive: datetime,
    business_date: date,
) -> None:
    monkeypatch.setattr(time_contract, "_utc_now_aware", lambda: utc_aware)

    assert time_contract.utc_now_naive() == utc_aware.replace(tzinfo=None)
    assert time_contract.beijing_now_naive() == beijing_naive
    assert time_contract.beijing_today() == business_date
    assert time_contract.beijing_today().isoformat() == business_date.isoformat()


def test_explicit_api_serialization_contracts() -> None:
    value = datetime(2026, 7, 17, 0, 30, 45, 123456)

    assert time_contract.utc_naive_to_api(value) == "2026-07-17T00:30:45.123456Z"
    assert (
        time_contract.beijing_naive_to_api(value)
        == "2026-07-17T00:30:45.123456+08:00"
    )


@pytest.mark.parametrize(
    ("utc_value", "expected_date"),
    [
        (datetime(2026, 7, 16, 15, 59, 59), date(2026, 7, 16)),
        (datetime(2026, 7, 16, 16, 0), date(2026, 7, 17)),
        (datetime(2026, 7, 16, 23, 59), date(2026, 7, 17)),
        (datetime(2026, 7, 17, 0, 0), date(2026, 7, 17)),
        (datetime(2026, 7, 17, 15, 59, 59), date(2026, 7, 17)),
        (datetime(2026, 7, 17, 16, 0), date(2026, 7, 18)),
    ],
)
def test_utc_naive_to_beijing_date_crosses_at_utc_1600(
    utc_value: datetime,
    expected_date: date,
) -> None:
    assert time_contract.utc_naive_to_beijing_date(utc_value) == expected_date


def test_beijing_date_query_bounds_are_half_open_utc_naive() -> None:
    start, end = time_contract.beijing_date_bounds_utc_naive(date(2026, 7, 17))

    assert start == datetime(2026, 7, 16, 16, 0)
    assert end == datetime(2026, 7, 17, 16, 0)
    assert start.tzinfo is None
    assert end.tzinfo is None
    assert end - start == time_contract.timedelta(days=1)


@pytest.mark.parametrize(
    "serializer",
    [
        time_contract.utc_naive_to_api,
        time_contract.beijing_naive_to_api,
        time_contract.utc_naive_to_beijing_date,
    ],
)
def test_naive_datetime_contract_rejects_aware_values(serializer) -> None:
    with pytest.raises(ValueError, match="timezone-naive"):
        serializer(datetime(2026, 7, 17, tzinfo=timezone.utc))


def test_date_bounds_reject_datetime_subclass() -> None:
    with pytest.raises(TypeError, match="must be a date"):
        time_contract.beijing_date_bounds_utc_naive(datetime(2026, 7, 17))
