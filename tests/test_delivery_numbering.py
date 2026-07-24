from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.delivery_numbering import (
    DELIVERY_NUMBER_MAX_LENGTH,
    DELIVERY_NUMBER_MAX_PREFIX_LENGTH,
    DeliveryNumberingError,
    next_delivery_number,
)


class _ScalarResult:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class _RowsResult:
    def __init__(self, rows: list[tuple[int, str | None]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[int, str | None]]:
        return self.rows


class _FakeSession:
    def __init__(
        self,
        sequence: int = 1,
        customers: list[tuple[int, str | None]] | None = None,
    ) -> None:
        self.sequence = sequence
        self.customers = customers or []
        self.calls: list[dict[str, str]] = []

    def execute(self, _statement, parameters=None):
        if parameters is None:
            return _RowsResult(self.customers)
        self.calls.append(parameters)
        return _ScalarResult(self.sequence)


@pytest.mark.parametrize(
    ("customer_code", "expected_prefix"),
    [
        ("SME", "SME"),
        (" th ", "TH"),
        ("天华", "天华"),
    ],
)
def test_customer_code_is_trimmed_and_uppercased(
    customer_code: str,
    expected_prefix: str,
) -> None:
    db = _FakeSession()
    customer = SimpleNamespace(id=12, customer_code=customer_code)

    number = next_delivery_number(
        db,
        customer=customer,
        delivery_date=date(2026, 7, 24),
    )

    assert number == f"{expected_prefix}-20260724-001"
    assert db.calls == [{"sequence_date": "2026-07-24"}]


@pytest.mark.parametrize("customer_code", [None, "", "   "])
def test_empty_customer_code_uses_stable_customer_id_fallback(
    customer_code: str | None,
) -> None:
    db = _FakeSession(sequence=7)
    customer = SimpleNamespace(id=35, customer_code=customer_code)

    number = next_delivery_number(
        db,
        customer=customer,
        delivery_date=date(2026, 7, 24),
    )

    assert number == "KH35-20260724-007"


def test_overlong_customer_code_is_rejected_before_sequence_is_consumed() -> None:
    db = _FakeSession()
    customer = SimpleNamespace(
        id=1,
        customer_code="X" * (DELIVERY_NUMBER_MAX_PREFIX_LENGTH + 1),
    )

    with pytest.raises(DeliveryNumberingError, match="客户缩写过长"):
        next_delivery_number(
            db,
            customer=customer,
            delivery_date=date(2026, 7, 24),
        )

    assert db.calls == []


def test_maximum_prefix_still_fits_delivery_column() -> None:
    db = _FakeSession(sequence=999)
    customer = SimpleNamespace(
        id=1,
        customer_code="X" * DELIVERY_NUMBER_MAX_PREFIX_LENGTH,
    )

    number = next_delivery_number(
        db,
        customer=customer,
        delivery_date=date(2026, 7, 24),
    )

    assert len(number) == DELIVERY_NUMBER_MAX_LENGTH


def test_case_insensitive_customer_prefix_collision_is_rejected() -> None:
    db = _FakeSession(customers=[(12, "th"), (13, "TH")])
    customer = SimpleNamespace(id=12, customer_code="th")

    with pytest.raises(
        DeliveryNumberingError,
        match="请在客户资料中分别设置唯一缩写",
    ):
        next_delivery_number(
            db,
            customer=customer,
            delivery_date=date(2026, 7, 24),
        )

    assert db.calls == []


def test_both_delivery_creation_paths_use_shared_numbering_service() -> None:
    root = Path(__file__).resolve().parents[1]
    ordinary = (root / "app" / "api" / "deliveries.py").read_text(
        encoding="utf-8"
    )
    tianhua = (
        root / "app" / "services" / "tianhua_pre_delivery.py"
    ).read_text(encoding="utf-8")

    for source in (ordinary, tianhua):
        assert "next_delivery_number(" in source
        assert "INSERT INTO delivery_daily_sequences" not in source

    tianhua_api = (
        root / "app" / "api" / "tianhua_pre_delivery.py"
    ).read_text(encoding="utf-8")
    save_start = tianhua_api.index("def _save(")
    save_end = tianhua_api.index("@router.post", save_start)
    save_block = tianhua_api[save_start:save_end]
    assert save_block.index("except DeliveryNumberingError") < (
        save_block.index("except ValueError")
    )
    assert "HTTPException(409,str(e))" in save_block
