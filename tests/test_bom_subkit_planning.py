from dataclasses import replace
from decimal import Decimal

import pytest

from app.services.bom_subkit_planning import (
    SubkitMember, normalize_members, plan_receipt_assembly, plan_subkit_requisition,
)
from app.services.composite_bom_execution import CompositeBOMExecutionError


RECIPE = [SubkitMember(3788, 2), SubkitMember(3789, 6)]


def assemble(pieces, demand=100, **kwargs):
    return plan_receipt_assembly(
        parent_product_id=3765, kit_product_id=9000, members=RECIPE,
        remaining_kit_demand=demand, eligible_pieces=pieces, **kwargs,
    )


def test_000148_two_long_six_short_only_produces_liner_kits():
    result = assemble({3788: 200, 3789: 600})
    assert result.kit_quantity == 100
    assert result.remaining_kit_demand == 0
    assert [(r.product_id, r.consumed_pieces) for r in result.components] == [
        (3788, 200), (3789, 600)
    ]
    assert all(r.retained_pieces == r.loss_pieces == 0 for r in result.components)
    assert 3765 not in {r.product_id for r in result.components}


def test_partial_receipts_keep_unmatched_pieces_until_next_receipt():
    first = assemble({3788: 205})
    assert first.kit_quantity == 0
    second = assemble({r.product_id: r.retained_pieces + (119 if r.product_id == 3789 else 0)
                       for r in first.components}, first.remaining_kit_demand)
    assert second.kit_quantity == 19
    assert [r.retained_pieces for r in second.components] == [167, 5]
    third = assemble({r.product_id: r.retained_pieces + (481 if r.product_id == 3789 else 0)
                      for r in second.components}, second.remaining_kit_demand)
    assert third.kit_quantity == 81
    assert [r.retained_pieces for r in third.components] == [5, 0]
    assert third.remaining_kit_demand == 0


def test_surplus_is_retained_not_automatically_scrapped_or_overassembled():
    result = assemble({3788: 220, 3789: 660})
    assert result.kit_quantity == 100
    assert [r.retained_pieces for r in result.components] == [20, 60]
    assert all(r.loss_pieces == 0 for r in result.components)


def test_explicit_loss_can_split_surplus_between_stock_and_loss():
    result = assemble({3788: 220, 3789: 660},
                      confirmed_loss_pieces={3788: 3, 3789: 12}, loss_confirmed=True)
    assert [r.retained_pieces for r in result.components] == [17, 48]
    assert [r.loss_pieces for r in result.components] == [3, 12]


@pytest.mark.parametrize('confirmation', [False, 1, 'true', None])
def test_loss_requires_explicit_boolean_confirmation(confirmation):
    with pytest.raises(CompositeBOMExecutionError, match='需要确认'):
        assemble({3788: 220, 3789: 660},
                 confirmed_loss_pieces={3788: 1}, loss_confirmed=confirmation)


def test_cannot_scrap_pieces_already_converted_to_kits():
    with pytest.raises(CompositeBOMExecutionError, match='剩余片数'):
        assemble({3788: 220, 3789: 660},
                 confirmed_loss_pieces={3788: 21}, loss_confirmed=True)


def test_next_order_offsets_kits_first_then_retained_loose_pieces():
    result = plan_subkit_requisition(
        parent_product_id=3765, kit_product_id=9000, members=RECIPE,
        required_kits=100, allocated_existing_kits=10,
        allocated_existing_pieces={3788: 20, 3789: 60},
    )
    assert result == {3788: 160, 3789: 480}


@pytest.mark.parametrize('pieces', [{999: 5}, {'3788': 2}, {True: 2}])
def test_no_code_based_or_unknown_identity_fallback(pieces):
    with pytest.raises(CompositeBOMExecutionError, match='非本子套件'):
        assemble(pieces)


@pytest.mark.parametrize('value', [-1, 1.5, True, 'NaN', 'Infinity'])
def test_invalid_quantities_fail_closed(value):
    with pytest.raises(CompositeBOMExecutionError):
        assemble({3788: value})
    with pytest.raises(CompositeBOMExecutionError):
        assemble({}, value)


@pytest.mark.parametrize('members', [[], RECIPE + [RECIPE[0]],
    [replace(RECIPE[0], product_id=3765)], [replace(RECIPE[0], product_id=9000)],
    [replace(RECIPE[0], pieces_per_kit=0)]])
def test_invalid_recipe_is_rejected(members):
    with pytest.raises(CompositeBOMExecutionError):
        normalize_members(members, parent_product_id=3765, kit_product_id=9000)


def test_zero_demand_leaves_everything_in_stock():
    result = assemble({3788: 220, 3789: 660}, demand=0)
    assert result.kit_quantity == 0
    assert [r.retained_pieces for r in result.components] == [220, 660]


def test_decimal_whole_numbers_are_supported_and_inputs_unchanged():
    inputs = {3788: Decimal('200'), 3789: Decimal('600')}
    assert assemble(inputs).kit_quantity == 100
    assert inputs == {3788: Decimal('200'), 3789: Decimal('600')}


def test_quantity_conservation_and_shortest_component_across_small_balances():
    for long in range(15):
        for short in range(40):
            result = assemble({3788: long, 3789: short}, demand=4)
            assert result.kit_quantity == min(4, long // 2, short // 6)
            for row in result.components:
                assert row.before_pieces == row.consumed_pieces + row.retained_pieces + row.loss_pieces
                assert row.retained_pieces >= 0


@pytest.mark.parametrize('kits,pieces', [(101, {}), (100, {3788: 1}), (0, {3789: 601})])
def test_cannot_allocate_more_existing_stock_than_required(kits, pieces):
    with pytest.raises(CompositeBOMExecutionError, match='不能超过'):
        plan_subkit_requisition(
            parent_product_id=3765, kit_product_id=9000, members=RECIPE,
            required_kits=100, allocated_existing_kits=kits,
            allocated_existing_pieces=pieces,
        )
