from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.material import Material
from app.models.product import Product
from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
from app.services.composite_bom import get_order_item_bom_components_by_item_ids
from app.services.multilevel_bom_orders import freeze_master_order_bom
from app.services.multilevel_bom_plan import BomPlanError
from app.services.order_material_cost import (
    build_material_cost_estimate_context, estimate_order_item_material_cost,
)
from tests.test_multilevel_bom_compile import setup_liner
from tests.test_multilevel_bom_master import save
from tests.test_multilevel_bom_orders import context


def materialize(db):
    material = Material(code="TEST-GRAPH", supplier_name="匿名供应商", layer_count=3,
                        quote_price=Decimal("2"), is_active=True)
    db.add(material)
    db.flush()
    for product in db.scalars(select(Product)):
        product.material_id = material.id
        product.report_length_mm = product.report_width_mm = 1000
        product.layer_count = 3
    db.commit()
    return material


def estimates(db, item):
    rows = get_order_item_bom_components_by_item_ids(db, [item.id])
    context = build_material_cost_estimate_context(db, [item], bom_components_by_item_id=rows)
    direct = estimate_order_item_material_cost(db, item)
    batched = estimate_order_item_material_cost(db, item, bom_components=rows[item.id], context=context)
    assert direct == batched
    return direct


def test_liner_cost_uses_three_real_materials_and_frozen_dimensions(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    materialize(db)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    result = estimates(db, item)
    assert result["estimated_material_total_cost"] == "700.00"
    assert {r["product_id"]: r["purchase_sheet_quantity"] for r in result["material_cost_components"]} == {1: 100, 3: 100, 4: 150}
    db.get(Product, 3).report_length_mm = 10
    db.get(Product, 3).default_cutting_mode = "一开一"
    db.commit()
    assert estimates(db, item) == result


def test_00205_no_phantom_parent_or_name_multiplier(context):
    db, actor, item, _ = context
    save(db, actor, 1, "assembled", [(3, 3, "assembly"), (4, 4, "assembly")])
    item.quantity = 300
    for pid, name in ((3, "长片15片（5套）"), (4, "短片20片（5套）")):
        product = db.get(Product, pid)
        product.product_name = name
        product.box_style = "隔板"
        product.default_cutting_mode = "一开四"
    materialize(db)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    result = estimates(db, item)
    assert result["estimated_material_total_cost"] == "1050.00"
    assert {r["product_id"]: r["required_piece_quantity"] for r in result["material_cost_components"]} == {3: 900, 4: 1200}


def test_splice_cover_base_and_spares_cost_each_physical_route_once(context):
    db, actor, item, _ = context
    save(db, actor, 1, "assembled", [(3, 2, "assembly"), (4, 1, "assembly")])
    materialize(db)
    long = db.get(Product, 3)
    long.box_style, long.pieces_per_box, long.default_cutting_mode = "隔板", 2, "一开四"
    box = db.get(Product, 4)
    box.box_style, box.pieces_per_box = "A3 天地盖", 2
    box.base_report_length_mm, box.base_report_width_mm = 500, 1000
    relation = db.scalar(select(ProductBomComponent).where(ProductBomComponent.component_product_id == 4))
    relation.spare_sheet_quantity = 3
    db.commit()
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    result = estimates(db, item)
    assert result["estimated_material_total_cost"] == "509.00"
    rows = {(r["product_id"], r["route_key"]): r for r in result["material_cost_components"]}
    assert {key: r["purchase_sheet_quantity"] for key, r in rows.items()} == {(3, "whole"): 100, (4, "cover"): 103, (4, "base"): 103}
    assert rows[4, "base"]["report_length_mm"] == "500"
    assert rows[3, "whole"]["required_piece_quantity"] == 400


def test_shared_child_cost_aggregates_real_identity_once(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    save(db, actor, 1, "manufactured", [(2, 1, "accompany"), (3, 1, "accompany")])
    materialize(db)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    result = estimates(db, item)
    assert result["estimated_material_total_cost"] == "800.00"
    assert len(result["material_cost_components"]) == 3


def test_mold_yield_replaces_cut_factor_instead_of_multiplying_it(context):
    from app.models.mold_tool import MoldTool
    from app.services.composite_bom import replace_product_bom
    db, actor, item, _ = context
    materialize(db)
    mold = MoldTool(mold_code="COST-YIELD", mold_name="匿名四出模具",
                    rack_location="TEST-R1", is_active=True)
    db.add(mold)
    db.flush()
    child = db.get(Product, 3)
    child.box_category, child.box_style = "die_cut", "隔板"
    child.mold_tool_id, child.default_cutting_mode = mold.id, "一开一"
    db.commit()
    replace_product_bom(db, parent_product_id=1, inventory_mode="assembled",
        expected_version=db.get(Product, 1).version, user=actor, components=[
            {"component_product_id": 3, "quantity_per_set": 3, "inventory_relation": "assembly",
             "is_die_cut": True, "mold_tool_id": mold.id, "mold_max_yield_per_sheet": 4,
             "spare_sheet_quantity": 2}])
    db.commit()
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    result = estimates(db, item)
    row, = result["material_cost_components"]
    assert row["yield_per_purchase_sheet"] == 4
    assert row["purchase_sheet_quantity"] == 77
    assert result["estimated_material_total_cost"] == "154.00"


def test_missing_material_does_not_return_complete_cost(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    materialize(db)
    db.get(Product, 3).report_length_mm = None
    db.commit()
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    result = estimates(db, item)
    assert result["material_cost_status"] == "partial"
    assert result["estimated_material_total_cost"] is None
    assert any("缺少报料长宽" in text for text in result["material_cost_missing_items"])


@pytest.mark.parametrize("corruption", ["quantity", "schema"])
def test_corrupt_snapshot_cannot_fall_back_to_old_flat_calculation(context, corruption):
    db, actor, item, _ = context
    setup_liner(db, actor)
    materialize(db)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    row = db.scalar(select(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.component_product_id == 3))
    if corruption == "quantity":
        # Remain valid under the local SQL formula but contradict the graph.
        row.quantity_per_set = 3
        row.required_piece_quantity = row.order_set_quantity * 3
    else:
        for snapshot in db.scalars(select(SalesOrderItemBomComponent)):
            snapshot.snapshot_schema_version = 6
    db.commit()
    with pytest.raises(BomPlanError, match="身份或数量"):
        estimate_order_item_material_cost(db, item)


def test_cost_freeze_preserves_prior_quote_and_same_input_is_idempotent(context):
    from app.services.order_material_cost_snapshot import freeze_order_item_material_cost
    db, actor, item, _ = context
    setup_liner(db, actor)
    material = materialize(db)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    first, created = freeze_order_item_material_cost(db, item, actor_id=actor.id)
    db.commit()
    original = first.components_json
    same, repeated = freeze_order_item_material_cost(db, item, actor_id=actor.id)
    assert created and not repeated and same.id == first.id
    material.quote_price = Decimal("3")
    material.version += 1
    db.commit()
    later, created = freeze_order_item_material_cost(db, item, actor_id=actor.id)
    db.commit()
    assert created and later.snapshot_version == first.snapshot_version + 1
    assert first.estimated_material_total_cost == Decimal("700")
    assert later.estimated_material_total_cost == Decimal("1050")
    assert first.components_json == original


def test_purchased_node_is_not_board_or_silently_zero_cost(context):
    db, actor, item, _ = context
    materialize(db)
    root = db.get(Product, 1)
    root.supply_mode = "external_purchase"
    root.external_packaging_category_code = "other_packaging"
    root.external_packaging_specification_json = '{"summary":"匿名外购"}'
    root.external_packaging_specification_summary = "匿名外购"
    root.external_packaging_purchase_unit = "只"
    root.external_packaging_candidate_snapshot_json = "[]"
    root.external_packaging_default_order_quantity_basis = 1
    root.external_packaging_default_purchase_quantity_basis = 3
    db.commit()
    save(db, actor, 1, "purchased", [])
    db.commit()
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    result = estimates(db, item)
    assert result["material_cost_status"] == "missing"
    assert result["material_cost_components"] == []
    assert result["estimated_material_total_cost"] is None
    assert any("外购节点成本待核定" in text for text in result["material_cost_missing_items"])
