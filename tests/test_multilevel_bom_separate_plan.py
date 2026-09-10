"""Physical demand and immutable modes; database flows have separate tests."""
import json
from dataclasses import replace

import pytest

from app.services.multilevel_bom_plan import (
    BomEdge, BomModes, BomPlanError, FrozenBom, ProductNode, plan_assembly, plan_bom,
)
from app.services.multilevel_bom_snapshot import dump_graph, load_graph
from tests.test_bom_independent_piece_credit_contract import graph


def separate_graph():
    original = graph()
    return replace(original,
        nodes=(replace(original.nodes[0], source="separate"), *original.nodes[1:]),
        edges=tuple(replace(e, relation="accompany") for e in original.edges),
        modes=BomModes("expand_children", "separate", "components"))


def test_separate_parts_credit_and_pick_without_parent_or_assembly():
    frozen = load_graph(dump_graph(separate_graph()))
    demand = plan_bom(frozen, 100, eligible_stock={2: 20, 3: 50})
    assert dict(demand.picking) == {2: 300, 3: 400}
    assert {r.product_id: r.purchase_sheets for r in demand.materials} == {2: 70, 3: 175}
    receipt = plan_assembly(frozen, 100, eligible_stock={2: 300, 3: 400})
    assert receipt.steps == ()
    assert dict(receipt.remaining_stock) == {1: 0, 2: 300, 3: 400}


def test_nested_separate_nodes_and_shared_child_do_not_create_pickable_parents():
    original = separate_graph()
    nested = replace(original,
        nodes=(*original.nodes, ProductNode(4, 136, 1, "组合需求", "套", "separate")),
        edges=(BomEdge(1, 2, 3, "accompany"), BomEdge(1, 4, 2, "accompany"),
               BomEdge(4, 2, 1, "accompany"), BomEdge(4, 3, 4, "accompany")))
    demand = plan_bom(nested, 100)
    assert dict(demand.picking) == {2: 500, 3: 800}
    assert {p.product_id: p.required_units for p in demand.products} == {1: 100, 4: 200, 2: 500, 3: 800}


def test_separate_parent_cannot_credit_phantom_inventory():
    with pytest.raises(BomPlanError, match="不存在可抵扣"):
        plan_bom(separate_graph(), 100, eligible_stock={1: 5})


@pytest.mark.parametrize("modes", [None,
    BomModes("invented", "separate", "components"),
    BomModes("expand_children", "assembled", "components"),
    BomModes("expand_children", "separate", "invalid")])
def test_incompatible_modes_fail_closed(modes):
    with pytest.raises(BomPlanError):
        dump_graph(replace(separate_graph(), modes=modes))


def test_new_modes_are_frozen_without_changing_legacy_bytes():
    legacy = graph()
    old = dump_graph(legacy)
    assert json.loads(old)["schema_version"] == 1
    assert dump_graph(load_graph(old)) == old
    current = separate_graph()
    document = dump_graph(current)
    assert json.loads(document)["schema_version"] == 3
    changed = replace(current, modes=replace(current.modes, delivery="parent"))
    assert dump_graph(changed) != document
    assert load_graph(document).modes.delivery == "components"


def test_virtual_child_cannot_be_consumed_into_an_assembly():
    current = separate_graph()
    with pytest.raises(BomPlanError, match="组合需求不能作为组装"):
        replace(current, root_id=4, nodes=(*current.nodes,
            ProductNode(4, 136, 1, "实体组装", "套", "assembled")),
            edges=(*current.edges, BomEdge(4, 1, 1, "assembly")),
            modes=BomModes("expand_children", "assembled", "parent")).validated()
