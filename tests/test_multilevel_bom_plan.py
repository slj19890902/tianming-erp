from dataclasses import replace

import pytest

from app.services.multilevel_bom_plan import (
    BomEdge as E, BomPlanError, FrozenBom, MaterialRoute as R,
    ProductNode as N, plan_bom, plan_assembly,
)


def liner_graph():
    return FrozenBom(1, 136, (
        N(1, 136, 1, "纸盒", "只", "manufactured", (R("body"),)),
        N(2, 136, 1, "内衬", "套", "assembled"),
        N(3, 136, 1, "长片", "片", "manufactured", (R("long", 1, 2),)),
        N(4, 136, 1, "短片", "片", "manufactured", (R("short", 1, 4),)),
    ), (E(1, 2, 1, "accompany"), E(2, 3, 2, "assembly"), E(2, 4, 6, "assembly")))


def test_liner_hierarchy_explodes_material_but_keeps_two_pick_positions():
    plan = plan_bom(liner_graph(), 100)
    assert plan.picking == ((1, 100), (2, 100))
    assert [(m.product_id, m.purchase_sheets) for m in plan.materials] == [(1, 100), (3, 100), (4, 150)]


def test_00205_assembled_root_has_no_phantom_parent_material():
    graph = FrozenBom(5, 136, (
        N(5, 136, 1, "格挡", "套", "assembled"),
        N(6, 136, 1, "长片", "片", "manufactured", (R("long", 1, 4),)),
        N(7, 136, 1, "短片", "片", "manufactured", (R("short", 1, 4),)),
    ), (E(5, 6, 3, "assembly"), E(5, 7, 4, "assembly")))
    plan = plan_bom(graph, 300)
    assert plan.picking == ((5, 300),)
    assert [(p.product_id, p.required_units) for p in plan.products] == [(5, 300), (6, 900), (7, 1200)]
    assert [m.purchase_sheets for m in plan.materials] == [225, 300]


def test_parent_stock_does_not_erase_accompanying_liner_demand():
    plan = plan_bom(liner_graph(), 100, eligible_stock={1: 100, 2: 20, 3: 10})
    assert [p.make_units for p in plan.products] == [0, 80, 150, 480]
    assert [m.purchase_sheets for m in plan.materials] == [0, 75, 120]
    assert plan.picking == ((1, 100), (2, 100))


def test_shared_child_stock_is_credited_once_not_per_branch():
    graph = liner_graph()
    graph = replace(graph, nodes=graph.nodes + (N(5, 136, 1, "另一个内衬", "套", "assembled"),),
                    edges=graph.edges + (E(1, 5, 1, "accompany"), E(5, 3, 3, "assembly")))
    plan = plan_bom(graph, 10, eligible_stock={3: 15})
    long = next(p for p in plan.products if p.product_id == 3)
    assert (long.required_units, long.credited_units, long.make_units) == (50, 15, 35)
    assert dict(plan.picking) == {1: 10, 2: 10, 5: 10}


def test_physical_piece_groups_are_not_offset_against_each_other():
    node = N(1, 136, 1, "天地盖", "套", "manufactured", (R("cover", 2, 3), R("base", 1, 2)))
    plan = plan_bom(FrozenBom(1, 136, (node,), ()), 5,
                    eligible_pieces={(1, "cover"): 2})
    assert [(m.required_pieces, m.purchase_sheets, m.excess_pieces) for m in plan.materials] == [(10, 3, 1), (5, 3, 1)]


@pytest.mark.parametrize("quantity", [-1, True, 1.5, "2"])
def test_invalid_quantities_rejected(quantity):
    with pytest.raises(BomPlanError):
        plan_bom(liner_graph(), quantity)


@pytest.mark.parametrize("mutation", [
    lambda g: replace(g, edges=g.edges + (E(4, 1, 1, "assembly"),)),
    lambda g: replace(g, edges=g.edges + (g.edges[0],)),
    lambda g: replace(g, edges=g.edges + (E(1, 999, 1, "accompany"),)),
    lambda g: replace(g, nodes=(replace(g.nodes[0], customer_id=999),) + g.nodes[1:]),
    lambda g: replace(g, nodes=(replace(g.nodes[0], version=0),) + g.nodes[1:]),
    lambda g: replace(g, nodes=g.nodes + (N(10, 136, 1, "孤立", "只", "purchased"),)),
    lambda g: replace(g, edges=(replace(g.edges[0], relation="unknown"),) + g.edges[1:]),
    lambda g: replace(g, edges=(replace(g.edges[0], quantity=0),) + g.edges[1:]),
    lambda g: replace(g, edges=(replace(g.edges[0], parent_id=True),) + g.edges[1:]),
    lambda g: replace(g, nodes=(replace(g.nodes[0], routes=()),) + g.nodes[1:]),
])
def test_invalid_graphs_fail_closed(mutation):
    with pytest.raises(BomPlanError):
        plan_bom(mutation(liner_graph()), 1)


def test_unrelated_stock_and_material_cannot_be_credited():
    with pytest.raises(BomPlanError):
        plan_bom(liner_graph(), 1, eligible_stock={999: 3})
    with pytest.raises(BomPlanError):
        plan_bom(liner_graph(), 1, eligible_pieces={(1, "wrong"): 3})


def test_rename_does_not_change_identity_and_old_snapshot_stays_frozen():
    old = liner_graph()
    renamed = replace(old, nodes=tuple(replace(n, name="新的名称", version=2) if n.product_id == 2 else n for n in old.nodes))
    assert plan_bom(old, 20) == plan_bom(renamed, 20)
    assert old.nodes[1].name == "内衬"


def test_cutting_conservation_over_many_quantities():
    for quantity in range(51):
        plan = plan_bom(liner_graph(), quantity, eligible_stock={2: 7})
        routes = {(n.product_id, r.key): r for n in liner_graph().nodes for r in n.routes}
        for row in plan.materials:
            assert row.purchase_sheets * routes[row.product_id, row.route_key].pieces_per_sheet + row.credited_pieces == row.required_pieces + row.excess_pieces


def test_accompanying_accessory_survives_ancestor_assembled_stock_credit():
    graph = liner_graph()
    graph = replace(graph, nodes=graph.nodes + (N(5, 136, 1, "附送附件", "件", "purchased"),),
                    edges=graph.edges + (E(3, 5, 1, "accompany"),))
    result = plan_bom(graph, 10, eligible_stock={2: 10})
    assert dict(result.picking) == {1: 10, 2: 10, 5: 20}
    assert next(p for p in result.products if p.product_id == 5).make_units == 20
    assert next(p for p in result.products if p.product_id == 3).make_units == 0


def test_receipt_assembly_preserves_carton_and_retains_excess():
    result = plan_assembly(liner_graph(), 100, eligible_stock={1: 50, 3: 210, 4: 590})
    assert len(result.steps) == 1
    assert result.steps[0].produced_units == 98
    assert dict(result.remaining_stock) == {1: 50, 2: 98, 3: 14, 4: 2}
    # Replaying current balances does not convert the same pieces a second time.
    replay = plan_assembly(liner_graph(), 100, eligible_stock=dict(result.remaining_stock))
    assert replay.steps == ()


def test_multilevel_assembly_works_bottom_up_and_only_incrementally():
    graph = liner_graph()
    graph = replace(graph, nodes=(N(1, 136, 1, "成套总成", "套", "assembled"),) + graph.nodes[1:],
                    edges=(E(1, 2, 2, "assembly"),) + graph.edges[1:])
    result = plan_assembly(graph, 10, eligible_stock={3: 40, 4: 120})
    assert [(s.product_id, s.produced_units) for s in result.steps] == [(2, 20), (1, 10)]
    assert dict(result.remaining_stock) == {1: 10, 2: 0, 3: 0, 4: 0}


def test_receipt_assembly_shortage_never_consumes_another_component_twice():
    graph = liner_graph()
    graph = replace(graph, nodes=graph.nodes + (N(5, 136, 1, "副套", "套", "assembled"),),
                    edges=graph.edges + (E(1, 5, 1, "accompany"), E(5, 3, 2, "assembly")))
    result = plan_assembly(graph, 10, eligible_stock={3: 20, 4: 60})
    assert sum(dict(s.consumed).get(3, 0) for s in result.steps) <= 20
    assert all(q >= 0 for _, q in result.remaining_stock)


def test_manufactured_assembly_requires_own_completion_evidence():
    graph = liner_graph()
    graph = replace(graph, edges=(E(1, 2, 1, "assembly"),) + graph.edges[1:])
    with pytest.raises(BomPlanError, match="本体完工来源"):
        plan_assembly(graph, 10, eligible_stock={2: 10})


def test_fulfilled_stock_limits_new_assembly_but_is_not_a_physical_input():
    result = plan_assembly(liner_graph(), 100, eligible_stock={3: 200, 4: 600}, fulfilled_stock={2: 50})
    assert [(s.product_id, s.produced_units) for s in result.steps] == [(2, 50)]
    assert dict(result.remaining_stock)[2] == 50
    with pytest.raises(BomPlanError):
        plan_assembly(liner_graph(), 100, eligible_stock={}, fulfilled_stock={1: 50})
