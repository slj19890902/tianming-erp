import hashlib
import json

import pytest
from sqlalchemy import inspect

from app.models.product_bom import SalesOrderItemBomComponent
from app.services.multilevel_bom_orders import freeze_master_order_bom, read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from app.services.multilevel_bom_production_revision import (
    prepare_production_revision, apply_production_revision, production_basis,
)
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_compile import setup_liner


def frozen(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    return read_compiled_order_bom(db, item.id)


def test_projection_preserves_original_rows_and_true_recipe(context):
    original = frozen(context)
    before = production_basis(original)
    document, checksum = prepare_production_revision(original, {
        "1": {"report_width_mm": 710, "production_notes": "本单新版"},
        "3": {"default_cutting_mode": "一开四"},
    })
    effective = apply_production_revision(original, document, expected_hash=checksum)
    rows = {row.component_product_id: row for row in effective.snapshots}
    assert rows[1].snapshot_component_report_width_mm == 710
    assert rows[1].snapshot_component_production_notes == "本单新版"
    assert production_basis(original) == before
    assert effective.graph.edges == original.graph.edges
    assert plan_bom(effective.graph, 100).picking == plan_bom(original.graph, 100).picking
    old_long = next(row for row in plan_bom(original.graph, 100).materials if row.product_id == 3)
    new_long = next(row for row in plan_bom(effective.graph, 100).materials if row.product_id == 3)
    assert (old_long.purchase_sheets, new_long.purchase_sheets) == (100, 50)
    assert [r.id for r in effective.snapshots] == [r.id for r in original.snapshots]
    assert all(inspect(row).transient for row in effective.snapshots)
    db, _, item, _ = context
    db.flush()
    db.expire_all()
    assert production_basis(read_compiled_order_bom(db, item.id)) == before
    for old in original.snapshots:
        if old.component_product_id in (2, 4):
            assert all(getattr(old, col.key) == getattr(rows[old.component_product_id], col.key)
                       for col in SalesOrderItemBomComponent.__table__.columns)


@pytest.mark.parametrize("changes", [
    {}, {"2": {"report_width_mm": 710}}, {"999": {"report_width_mm": 710}},
    {1: {"report_width_mm": 710}}, {"1": {"quantity_per_set": 2}},
    {"1": {"report_width_mm": 0}}, {"1": {"report_width_mm": True}},
    {"1": {"report_width_mm": "710"}}, {"1": {"default_cutting_mode": "未知"}},
    {"1": {"material_id": -1}}, {"1": {"production_notes": "x" * 5000}},
])
def test_rejects_identity_recipe_and_invalid_production_changes(context, changes):
    with pytest.raises(BomPlanError):
        prepare_production_revision(frozen(context), changes)


def test_revision_chain_requires_exact_previous_material_basis(context):
    original = frozen(context)
    first, first_hash = prepare_production_revision(original, {"1": {"report_width_mm": 710}})
    current = apply_production_revision(original, first, expected_hash=first_hash)
    second, second_hash = prepare_production_revision(current, {"1": {"report_width_mm": 720}})
    result = apply_production_revision(current, second, expected_hash=second_hash)
    assert next(r for r in result.snapshots if r.component_product_id == 1).snapshot_component_report_width_mm == 720
    with pytest.raises(BomPlanError, match="冻结版本"):
        apply_production_revision(original, second, expected_hash=second_hash)
    with pytest.raises(BomPlanError, match="校验失败"):
        apply_production_revision(original, first.replace("710", "711"), expected_hash=first_hash)
    malformed = json.dumps({"schema": True, "basis": production_basis(original), "changes": {"1": {"report_width_mm": 710}}})
    with pytest.raises(BomPlanError):
        apply_production_revision(original, malformed, expected_hash=hashlib.sha256(malformed.encode()).hexdigest())


def test_stored_chain_cas_and_outer_rollback(context):
    from sqlalchemy import select, func
    from app.models.multilevel_bom import OrderBomProductionRevision
    from app.services.multilevel_bom_production_versions import append_order_production_revision
    original = frozen(context)
    before = production_basis(original)
    db, actor, item, _ = context
    first = append_order_production_revision(db, order_item_id=item.id,
        changes={"1": {"report_width_mm": 710}}, expected_revision=0, actor=actor)
    assert first.revision == 1
    with pytest.raises(BomPlanError, match="已更新"):
        append_order_production_revision(db, order_item_id=item.id,
            changes={"1": {"report_width_mm": 720}}, expected_revision=0, actor=actor)
    second = append_order_production_revision(db, order_item_id=item.id,
        changes={"1": {"report_width_mm": 720}}, expected_revision=1, actor=actor)
    assert second.previous_id == first.id and second.revision == 2
    assert next(row for row in read_compiled_order_bom(db, item.id).snapshots
                if row.component_product_id == 1).snapshot_component_report_width_mm == 720
    db.rollback()
    assert db.scalar(select(func.count()).select_from(OrderBomProductionRevision)) == 0
    assert production_basis(read_compiled_order_bom(db, item.id)) == before
