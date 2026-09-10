import pytest

from app.services.multilevel_bom_fulfillment import component_fulfillment, preview_component_dispatch
from app.services.multilevel_bom_plan import BomPlanError
from tests.test_multilevel_bom_separate_plan import separate_graph


def test_nonproportional_batches_and_reversal_leave_exact_child_balances():
    graph = separate_graph()
    first = preview_component_dispatch(graph, 100, dispatched={}, requested={2: 120, 3: 100})
    assert not first.complete and first.physically_paired_sets == 25
    assert {r.product_id: r.remaining for r in first.components} == {2: 180, 3: 300}
    second = preview_component_dispatch(graph, 100, dispatched={2: 120, 3: 100}, requested={2: 180, 3: 300})
    assert second.complete and second.physically_paired_sets == 100
    # Existing allocation reversals supply these net physical quantities.
    assert component_fulfillment(graph, 100, {2: 120, 3: 100}) == first
    empty = component_fulfillment(graph, 100, {})
    assert {r.product_id: r.remaining for r in empty.components} == {2: 300, 3: 400}


def test_one_child_fully_delivered_cannot_close_order_or_drop_loose_pieces():
    result = preview_component_dispatch(separate_graph(), 100, dispatched={}, requested={2: 300, 3: 1})
    assert not result.complete and result.physically_paired_sets == 0
    assert result.components[1].remaining == 399


@pytest.mark.parametrize("requested", [{1: 1}, {2: 301}, {2: -1}, {2: True}, {2: 0}, {}, {2: 1.5}])
def test_invalid_dispatch_fails_without_rounding_or_identity_guessing(requested):
    with pytest.raises(BomPlanError):
        preview_component_dispatch(separate_graph(), 100, dispatched={}, requested=requested)
