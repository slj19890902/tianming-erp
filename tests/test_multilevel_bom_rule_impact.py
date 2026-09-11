from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.product import Product
from app.models.user import User
from app.services.multilevel_bom_compile import compile_master_order_bom
from app.services.multilevel_bom_orders import freeze_master_order_bom, read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError, BomModes
from app.services.multilevel_bom_rule_impact import rule_quantity_impact, review_current_rule_requirements
from app.services.multilevel_bom_snapshot import dump_graph
from tests.test_multilevel_bom_factory_compile import factory_copy, new_item
from tests.test_multilevel_bom_master import save


def frozen_order(db, quantity=100, explicit_modes=False):
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    save(db, actor, 3799, "assembled", [(3771, 3, "assembly"), (3783, 4, "assembly")])
    if explicit_modes:
        from app.services.composite_bom import replace_product_bom
        replace_product_bom(db, parent_product_id=3799, expected_version=db.get(Product, 3799).version,
            user=actor, inventory_mode="assembled", material_mode="expand_children", delivery_mode="parent",
            components=[dict(component_product_id=pid, quantity_per_set=quantity, inventory_relation="assembly")
                        for pid, quantity in [(3771, 3), (3783, 4)]])
    item = new_item(db, 3799, quantity)
    frozen = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    return actor, item, frozen


def test_change_to_separate_keeps_child_identity_and_changes_pick_units(factory_copy):
    db = factory_copy
    actor, item, frozen = frozen_order(db)
    original = dump_graph(frozen.graph)
    # Detached candidate represents the same validated quantity graph with
    # physical assembly edges explicitly changed to accompaniment.
    graph = replace(frozen.graph,
        nodes=tuple(replace(node, source="separate") if node.product_id == 3799 else node
                    for node in frozen.graph.nodes),
        edges=tuple(replace(edge, relation="accompany") for edge in frozen.graph.edges),
        modes=BomModes("expand_children", "separate", "components"))
    proposed = replace(frozen, graph=graph)
    impact = rule_quantity_impact(frozen, proposed, remaining_quantity=40)
    rows = {row["product_id"]: row for row in impact["products"]}
    assert rows[3799]["before"]["pick_quantity"] == 40
    assert rows[3799]["after"]["pick_quantity"] == 0
    assert not rows[3799]["physical_identity_compatible"]
    assert rows[3771]["physical_identity_compatible"]
    assert rows[3771]["material_conversion_compatible"]
    assert rows[3771]["after"]["pick_quantity"] == 120
    assert rows[3783]["after"]["pick_quantity"] == 160
    assert rows[3771]["after"]["materials"][0]["purchase_sheets"] == 30
    assert not impact["inventory_credits_applied"]
    assert dump_graph(read_compiled_order_bom(db, item.id).graph) == original
    assert not db.new and not db.dirty and not db.deleted


def test_master_ratio_change_compares_physical_recipe_and_material_sheets(factory_copy):
    db = factory_copy
    actor, item, frozen = frozen_order(db)
    save(db, actor, 3799, "assembled", [(3771, 4, "assembly"), (3783, 4, "assembly")])
    db.commit()
    proposed = compile_master_order_bom(db, item)
    result = rule_quantity_impact(frozen, proposed, remaining_quantity=100)
    rows = {row["product_id"]: row for row in result["products"]}
    assert rows[3771]["required_delta"] == 100
    assert rows[3771]["before"]["materials"][0]["purchase_sheets"] == 75
    assert rows[3771]["after"]["materials"][0]["purchase_sheets"] == 100
    assert rows[3771]["physical_identity_compatible"]
    assert not rows[3799]["physical_identity_compatible"]
    assert rows[3799]["required_delta"] == 0
    assert len(rows) == 3


def test_name_change_is_not_identity_change_but_units_are_not_subtracted(factory_copy):
    db = factory_copy
    actor, item, frozen = frozen_order(db)
    child = db.get(Product, 3771)
    child.product_name = "ISOLATED renamed long part"
    child.version += 1
    db.commit()
    proposed = compile_master_order_bom(db, item)
    impact = rule_quantity_impact(frozen, proposed, remaining_quantity=10)
    assert all(row["physical_identity_compatible"] for row in impact["products"])
    child.unit = "片"
    child.version += 1
    db.commit()
    proposed = compile_master_order_bom(db, item)
    rows = {row["product_id"]: row for row in rule_quantity_impact(
        frozen, proposed, remaining_quantity=10)["products"]}
    assert rows[3771]["required_delta"] is None
    assert rows[3771]["pick_delta"] is None
    assert not rows[3771]["physical_identity_compatible"]
    assert not rows[3799]["physical_identity_compatible"]


def test_rule_comparison_rejects_cross_order_customer_and_invalid_quantity(factory_copy):
    _, _, frozen = frozen_order(factory_copy)
    altered = replace(frozen, snapshots=tuple(SimpleNamespace(**{
        **{column.key: getattr(row, column.key) for column in row.__table__.columns},
        "sales_order_item_id": row.sales_order_item_id + 1}) for row in frozen.snapshots))
    with pytest.raises(BomPlanError, match="同一订单"):
        rule_quantity_impact(frozen, altered, remaining_quantity=10)
    with pytest.raises(BomPlanError, match="客户身份"):
        rule_quantity_impact(frozen, replace(frozen, graph=replace(frozen.graph, customer_id=999)),
                             remaining_quantity=10)
    with pytest.raises(BomPlanError):
        rule_quantity_impact(frozen, frozen, remaining_quantity=True)
    with pytest.raises(BomPlanError, match="超过"):
        rule_quantity_impact(frozen, frozen, remaining_quantity=101)


def test_finished_match_does_not_authorize_changed_cutting_conversion(factory_copy):
    db = factory_copy
    _, item, frozen = frozen_order(db)
    child = db.get(Product, 3771)
    child.default_cutting_mode = "一开二"
    child.version += 1
    db.commit()
    proposed = compile_master_order_bom(db, item)
    rows = {row["product_id"]: row for row in rule_quantity_impact(
        frozen, proposed, remaining_quantity=10)["products"]}
    assert not rows[3771]["material_conversion_compatible"]
    assert rows[3783]["material_conversion_compatible"]


def test_current_rule_review_is_detached_and_binds_changed_master(factory_copy):
    db = factory_copy
    actor, item, frozen = frozen_order(db)
    original = dump_graph(frozen.graph)
    source_ids = {row.id for row in frozen.snapshots}
    args = dict(order_item_id=item.id, customer_id=frozen.graph.customer_id)
    first = review_current_rule_requirements(db, **args)
    assert first.impact["remaining_quantity"] == 100
    assert all(row.id is None for row in first.proposed.snapshots)
    assert {row.id for row in first.previous.snapshots} == source_ids
    assert min(row.display_order for row in first.proposed.snapshots) > max(
        row.display_order for row in first.previous.snapshots)
    assert not db.new and not db.dirty and not db.deleted
    assert review_current_rule_requirements(db, **args).checksum == first.checksum
    save(db, actor, 3799, "assembled", [(3771, 4, "assembly"), (3783, 4, "assembly")])
    db.commit()
    second = review_current_rule_requirements(db, **args)
    assert second.checksum != first.checksum
    assert dump_graph(read_compiled_order_bom(db, item.id).graph) == original
    assert {row.id for row in read_compiled_order_bom(db, item.id).snapshots} == source_ids
    with pytest.raises(BomPlanError, match="客户不一致"):
        review_current_rule_requirements(db, **{**args, "customer_id": 999999})
    item.quantity += 1
    with pytest.raises(BomPlanError, match="未提交"):
        review_current_rule_requirements(db, **args)
    db.rollback()
