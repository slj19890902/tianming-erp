import pytest

from app.services.multilevel_bom_plan import (
    FrozenBom, ProductNode as N, MaterialRoute as R, BomEdge as E,
    plan_assembly, BomPlanError,
)


def graph():
    return FrozenBom(1, 1, (
        N(1, 1, 1, "带附件成品", "套", "manufactured", (R("whole"),)),
        N(2, 1, 1, "附件", "件", "purchased"),
    ), (E(1, 2, 2, "assembly"),))


def test_finished_body_and_finished_product_are_not_interchangeable():
    result = plan_assembly(graph(), 10, eligible_stock={1: 3, 2: 12}, body_stock={1: 4})
    assert [(s.product_id, s.produced_units, s.consumed, s.consumed_body_units)
            for s in result.steps] == [(1, 4, ((2, 8),), 4)]
    assert dict(result.remaining_stock) == {1: 7, 2: 4}
    assert dict(result.remaining_body_stock) == {1: 0}


def test_missing_or_empty_body_evidence_cannot_manufacture_from_children():
    with pytest.raises(BomPlanError):
        plan_assembly(graph(), 10, eligible_stock={2: 20})
    result = plan_assembly(graph(), 10, eligible_stock={2: 20}, body_stock={})
    assert result.steps == ()
    assert dict(result.remaining_stock) == {1: 0, 2: 20}


@pytest.mark.parametrize("bodies", [{2: 1}, {999: 1}, {1: -1}, {1: True}, {1: 1.5}])
def test_invalid_body_identity_or_count_fails(bodies):
    with pytest.raises(BomPlanError):
        plan_assembly(graph(), 10, eligible_stock={2: 20}, body_stock=bodies)


def test_already_fulfilled_output_is_not_available_body_or_stock():
    result = plan_assembly(graph(), 10, eligible_stock={2: 20},
                           fulfilled_stock={1: 8}, body_stock={1: 5})
    assert result.steps[0].produced_units == 2
    assert dict(result.remaining_stock) == {1: 2, 2: 16}
    assert dict(result.remaining_body_stock) == {1: 3}


def test_nested_manufactured_body_is_consumed_once_by_outer_assembly():
    inner = graph()
    nested = FrozenBom(3, 1, inner.nodes + (N(3, 1, 1, "外套件", "套", "assembled"),),
                       inner.edges + (E(3, 1, 2, "assembly"),))
    result = plan_assembly(nested, 3, eligible_stock={2: 20}, body_stock={1: 5})
    assert [(s.product_id, s.produced_units, s.consumed_body_units)
            for s in result.steps] == [(1, 5, 5), (3, 2, 0)]
    assert dict(result.remaining_stock) == {3: 2, 1: 1, 2: 10}
    assert dict(result.remaining_body_stock) == {1: 0}


def test_every_shortage_and_surplus_retains_units_without_mutating_inputs():
    for body in range(8):
        for child in range(15):
            stock, bodies = {2: child}, {1: body}
            result = plan_assembly(graph(), 5, eligible_stock=stock, body_stock=bodies)
            produced = sum(s.produced_units for s in result.steps)
            assert produced == min(5, body, child // 2)
            assert dict(result.remaining_stock)[2] + 2 * produced == child
            assert dict(result.remaining_body_stock)[1] + produced == body
            assert stock == {2: child} and bodies == {1: body}
