from decimal import Decimal
import json
import pytest

from sqlalchemy import select

from app.models.processing_cost import ProcessingCostSettings, ProductProcessingProfile
from app.models.product import Product
from app.services.multilevel_bom_orders import freeze_master_order_bom
from app.services.processing_cost import estimate_order_item_processing_cost
from app.services.order_material_cost_snapshot import freeze_order_item_material_cost
from app.services.order_estimated_cost_snapshot import freeze_order_item_estimated_cost
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_compile import setup_liner
from tests.test_multilevel_bom_material_estimate import materialize


def prepare(db, actor, item, *, kit=False, shared=False, body=False):
    from tests.test_multilevel_bom_master import save
    setup_liner(db, actor)
    if kit:
        save(db, actor, 1, "assembled", [(3, 3, "assembly"), (4, 4, "assembly")])
    elif body:
        save(db, actor, 1, "manufactured", [(2, 1, "assembly")])
    elif shared:
        save(db, actor, 1, "manufactured", [(2, 1, "accompany"), (3, 1, "accompany")])
    materialize(db)
    settings = db.get(ProcessingCostSettings, 1)
    settings.average_worker_monthly_salary = Decimal("2600")
    settings.working_days_per_month = 26
    for product in db.scalars(select(Product)):
        product.print_content = "无印刷"
        product.production_process = "无需结合"
        db.add(ProductProcessingProfile(product_id=product.id, printer_mode="none",
            die_cut_mode="small_normal" if product.id in (3, 4) else "none",
            assembly_worker_days_per_1000=Decimal(2) if product.id == (1 if kit else 2) else None))
    db.commit()
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()


def test_body_assembly_labor_missing_then_completed_without_changing_frozen_printing(context):
    db, actor, item, _ = context
    prepare(db, actor, item, body=True)
    result = estimate_order_item_processing_cost(db, item)
    assert result["calculation_status"] == "incomplete"
    assert any("组装人工" in text for text in result["missing_items"])
    material, _ = freeze_order_item_material_cost(db, item)
    first, _ = freeze_order_item_estimated_cost(db, item, material_snapshot=material)
    db.commit()
    original = first.breakdown_json
    profile = db.scalar(select(ProductProcessingProfile).where(ProductProcessingProfile.product_id == 1))
    profile.assembly_worker_days_per_1000 = Decimal(3)
    profile.printer_mode = "new"
    profile.version += 1
    db.commit()
    second, created = freeze_order_item_estimated_cost(db, item, material_snapshot=material)
    assert created and second.calculation_status == "calculated"
    root = next(r for r in json.loads(second.breakdown_json)["standard_processing"]["nodes"] if r["product_id"] == 1)
    assert root["processing"]["printing"]["printer_mode"] == "none"
    assert Decimal(root["processing"]["extra_assembly"]["assembly_worker_days_per_1000"]) == Decimal(3)
    assert first.breakdown_json == original


def test_liner_processing_counts_real_nodes_not_only_root(context):
    db, actor, item, _ = context
    prepare(db, actor, item)
    result = estimate_order_item_processing_cost(db, item)
    rows = {row["product_id"]: row for row in result["nodes"]}
    assert rows[2]["processing"]["extra_assembly"]["piece_quantity"] == 100
    assert rows[3]["processing"]["die_cut"]["piece_quantity"] == 200
    assert rows[4]["processing"]["die_cut"]["piece_quantity"] == 600
    assert result["calculation_status"] == "calculated"
    assert Decimal(result["estimated_processing_cost"]) == sum(
        Decimal(row["processing"]["estimated_processing_cost"]) for row in rows.values())
    assert Decimal(result["estimated_processing_cost"]) > 20


@pytest.mark.parametrize("kit", [False, True])
def test_shared_and_whole_kit_processing_uses_gross_node_quantities_once(context, kit):
    db, actor, item, _ = context
    item.quantity = 300 if kit else 100
    prepare(db, actor, item, kit=kit, shared=not kit)
    result = estimate_order_item_processing_cost(db, item)
    rows = {r["product_id"]: r for r in result["nodes"]}
    assert len(result["nodes"]) == len(rows) == (3 if kit else 4)
    assert rows[3]["processing"]["die_cut"]["piece_quantity"] == (900 if kit else 300)
    assert rows[4]["processing"]["die_cut"]["piece_quantity"] == (1200 if kit else 600)
    assert result["calculation_status"] == "calculated"


def test_cost_freeze_retains_node_process_after_master_edit(context):
    db, actor, item, _ = context
    prepare(db, actor, item)
    material, _ = freeze_order_item_material_cost(db, item)
    first, created = freeze_order_item_estimated_cost(db, item, material_snapshot=material)
    db.commit()
    original = first.breakdown_json
    assert first.rule_version == "multilevel-bom-estimated-v1"
    child = db.get(Product, 3)
    child.print_content = "四色"
    child.production_process = "双拼"
    child.version += 1
    profile = db.scalar(select(ProductProcessingProfile).where(ProductProcessingProfile.product_id == 3))
    profile.die_cut_mode = "oversize"
    profile.version += 1
    db.commit()
    second, repeated = freeze_order_item_estimated_cost(db, item, material_snapshot=material)
    assert json.loads(first.breakdown_json) == json.loads(second.breakdown_json)
    assert created and not repeated and first.id == second.id
    assert first.breakdown_json == original
    assert json.loads(original)["standard_processing"]["nodes"]


def test_missing_assembly_override_uses_50_per_hour_and_freezes(context):
    db, actor, item, _ = context
    prepare(db, actor, item)
    profile = db.scalar(select(ProductProcessingProfile).where(ProductProcessingProfile.product_id == 2))
    profile.assembly_worker_days_per_1000 = None
    db.commit()
    result = estimate_order_item_processing_cost(db, item)
    assert result["calculation_status"] == "calculated"
    node=next(r for r in result['nodes'] if r['product_id']==2)
    assert Decimal(node['processing']['extra_assembly']['assembly_worker_days_per_1000'])==Decimal('2.5')
    assert Decimal(result["known_processing_subtotal"]) > 0
    material, _ = freeze_order_item_material_cost(db, item)
    first, _ = freeze_order_item_estimated_cost(db, item, material_snapshot=material)
    db.commit()
    original = first.breakdown_json
    profile.assembly_worker_days_per_1000 = Decimal(2)
    profile.version += 1
    db.commit()
    second, created = freeze_order_item_estimated_cost(db, item, material_snapshot=material)
    assert not created and second.id==first.id
    assert first.calculation_status == "calculated" and first.breakdown_json == original


def test_changed_master_before_first_cost_cannot_invent_historical_printing(context):
    from app.services.multilevel_bom_plan import BomPlanError
    db, actor, item, _ = context
    prepare(db, actor, item)
    db.get(Product, 3).version += 1
    db.commit()
    with pytest.raises(BomPlanError, match="版本已变化"):
        estimate_order_item_processing_cost(db, item)


def test_assembly_ignores_manufacturing_profile_overrides(context):
    db, actor, item, _ = context
    prepare(db, actor, item)
    profile = db.scalar(select(ProductProcessingProfile).where(ProductProcessingProfile.product_id == 2))
    profile.printer_mode, profile.die_cut_mode = "new", "oversize"
    db.commit()
    result = estimate_order_item_processing_cost(db, item)
    row = next(r["processing"] for r in result["nodes"] if r["product_id"] == 2)
    assert row["printing"]["printer_mode"] == row["die_cut"]["die_cut_mode"] == row["joining"]["joining_mode"] == "none"
    assert row["extra_assembly"]["assembly_mode"] == "product_override"
    assert Decimal(row["estimated_processing_cost"]) == 20


def test_purchased_node_does_not_add_manufacturing_or_assembly_labor(context):
    from tests.test_multilevel_bom_master import save
    db, actor, item, _ = context
    root = db.get(Product, 1)
    root.supply_mode = "external_purchase"
    root.external_packaging_category_code = "other_packaging"
    root.external_packaging_specification_json = '{"summary":"匿名外购"}'
    root.external_packaging_specification_summary = "匿名外购"
    root.external_packaging_purchase_unit = "只"
    root.external_packaging_candidate_snapshot_json = "[]"
    root.external_packaging_default_order_quantity_basis = 1
    root.external_packaging_default_purchase_quantity_basis = 1
    root.print_content = "四色"
    root.production_process = "双拼"
    db.add(ProductProcessingProfile(product_id=1, printer_mode="new", die_cut_mode="oversize",
                                    assembly_worker_days_per_1000=Decimal(2)))
    db.commit()
    save(db, actor, 1, "purchased", [])
    db.commit()
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    result = estimate_order_item_processing_cost(db, item)
    assert result["estimated_processing_cost"] == "0.00"
    assert result["calculation_status"] == "calculated"


def test_tampered_frozen_labor_inputs_are_rejected(context):
    from app.services.multilevel_bom_plan import BomPlanError
    db, actor, item, _ = context
    prepare(db, actor, item)
    material, _ = freeze_order_item_material_cost(db, item)
    first, _ = freeze_order_item_estimated_cost(db, item, material_snapshot=material)
    db.commit()
    data = json.loads(first.breakdown_json)
    data["standard_processing"]["frozen_inputs"]["nodes"][0]["product"]["print_content"] = "四色"
    first.breakdown_json = json.dumps(data, ensure_ascii=False)
    db.commit()
    with pytest.raises(BomPlanError, match="校验失败"):
        estimate_order_item_processing_cost(db, item)
