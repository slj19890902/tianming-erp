"""Quantity contract only; not receipt, inventory-matching or delivery acceptance."""
from app.services.multilevel_bom_plan import (
    BomEdge, FrozenBom, MaterialRoute, ProductNode, plan_bom,
)


def graph(short_yield=2):
    return FrozenBom(1, 136, (
        ProductNode(1, 136, 1, "测试组合", "套", "assembled"),
        ProductNode(2, 136, 1, "测试长片", "片", "manufactured", (MaterialRoute("main", 1, 4),)),
        ProductNode(3, 136, 1, "测试短片", "片", "manufactured", (MaterialRoute("main", 1, short_yield),)),
    ), (BomEdge(1, 2, 3, "assembly"), BomEdge(1, 3, 4, "assembly")))


def test_twenty_long_fifty_short_are_credited_before_sheet_conversion():
    # These are already confirmed eligible reservations, not gross stock.
    plan = plan_bom(graph(), 100, eligible_stock={2: 20, 3: 50})
    products = {p.product_id: p for p in plan.products}
    assert (products[2].required_units, products[2].credited_units, products[2].make_units) == (300, 20, 280)
    assert (products[3].required_units, products[3].credited_units, products[3].make_units) == (400, 50, 350)
    materials = {m.product_id: m for m in plan.materials}
    assert (materials[2].required_pieces, materials[2].purchase_sheets) == (280, 70)
    assert (materials[3].required_pieces, materials[3].purchase_sheets) == (350, 175)


def test_credit_does_not_require_even_one_complete_set():
    plan = plan_bom(graph(), 100, eligible_stock={2: 20, 3: 0})
    assert {p.product_id: p.make_units for p in plan.products} == {1: 100, 2: 280, 3: 400}


def test_sheet_rounding_keeps_excess_piece_explicit():
    plan = plan_bom(graph(short_yield=3), 100, eligible_stock={2: 20, 3: 50})
    short = next(m for m in plan.materials if m.product_id == 3)
    assert (short.required_pieces, short.purchase_sheets, short.excess_pieces) == (350, 117, 1)
