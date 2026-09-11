"""Remainder transfer cannot turn unused sheet capacity into order credit."""
from types import SimpleNamespace

import pytest

from app.services.multilevel_bom_carried_material import remaining_semi_allocation
from app.services.multilevel_bom_plan import BomPlanError


def reservation(**changes):
    values = dict(id=1, reserved_stock_quantity=6, consumed_stock_quantity=2,
        released_stock_quantity=1, credited_requirement_quantity=23,
        consumed_requirement_quantity=8, released_requirement_quantity=3, yield_factor=4)
    return SimpleNamespace(**(values | changes))


def test_partial_sheet_credit_and_history_are_preserved():
    row = reservation(credited_requirement_quantity=22)
    before = vars(row).copy()
    assert remaining_semi_allocation(row) == (3, 11)
    assert vars(row) == before


@pytest.mark.parametrize("changes", [
    dict(consumed_stock_quantity=7),
    dict(consumed_requirement_quantity=24),
    dict(consumed_requirement_quantity=9),
    dict(released_requirement_quantity=5),
    dict(credited_requirement_quantity=25),
    dict(credited_requirement_quantity=24, released_requirement_quantity=1),
    dict(yield_factor=0),
    dict(yield_factor=True),
    dict(reserved_stock_quantity=-1),
])
def test_invalid_remainder_cannot_be_transferred(changes):
    with pytest.raises(BomPlanError):
        remaining_semi_allocation(reservation(**changes))


def test_fully_consumed_credit_is_not_transferred():
    assert remaining_semi_allocation(reservation(consumed_stock_quantity=6,
        released_stock_quantity=0, consumed_requirement_quantity=23,
        released_requirement_quantity=0)) == (0, 0)
