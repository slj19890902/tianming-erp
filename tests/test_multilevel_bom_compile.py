import pytest
from sqlalchemy import func, select

from app.models.product import Product
from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
from app.models.multilevel_bom import OrderBomGraph
from app.services.multilevel_bom_compile import compile_master_order_bom
from app.services.multilevel_bom_orders import freeze_master_order_bom, read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_master import configure_liner, save


def setup_liner(db, actor):
    configure_liner(db, actor)
    for pid, mode in ((3, "一开二"), (4, "一开四")):
        product = db.get(Product, pid)
        product.box_style = "隔板"
        product.default_cutting_mode = mode
        product.pieces_per_box = 1
    db.commit()


def test_real_liner_compiles_parent_and_two_materials_not_liner_material(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    compiled = compile_master_order_bom(db, item)
    plan = plan_bom(compiled.graph, 100)
    assert plan.picking == ((1, 100), (2, 100))
    assert [(m.product_id, m.purchase_sheets) for m in plan.materials] == [(1, 100), (3, 100), (4, 150)]
    assert {r.component_product_id: int(r.required_piece_quantity) for r in compiled.snapshots} == {
        1: 100, 2: 100, 3: 200, 4: 600}
    assert all(r.snapshot_schema_version == 5 for r in compiled.snapshots)
    assert db.scalar(select(func.count()).select_from(SalesOrderItemBomComponent)) == 0


def test_00205_three_long_four_short_not_five_cartons_multiplier(context):
    db, actor, item, _ = context
    save(db, actor, 1, "assembled", [(3, 3, "assembly"), (4, 4, "assembly")])
    item.quantity = 300
    for pid, name in ((3, "长片15片（每箱5套）"), (4, "短片20片（每箱5套）")):
        p = db.get(Product, pid)
        p.product_name = name
        p.box_style = "隔板"
        p.default_cutting_mode = "一开四"
    db.commit()
    compiled = compile_master_order_bom(db, item)
    plan = plan_bom(compiled.graph, item.quantity)
    assert plan.picking == ((1, 300),)
    assert [(m.product_id, m.purchase_sheets) for m in plan.materials] == [(3, 225), (4, 300)]
    assert db.get(Product, 1).unit == "套"


def test_splice_and_cutting_each_applied_once_with_separate_cover_base(context):
    db, actor, item, _ = context
    save(db, actor, 1, "assembled", [(3, 2, "assembly"), (4, 1, "assembly")])
    long = db.get(Product, 3)
    long.box_style = "隔板"
    long.pieces_per_box = 2
    long.default_cutting_mode = "一开四"
    box = db.get(Product, 4)
    box.box_style = "A3 天地盖"
    box.pieces_per_box = 2  # Must not double each lid/base again.
    box.report_length_mm = 800
    box.base_report_length_mm = 700
    db.commit()
    compiled = compile_master_order_bom(db, item)
    plan = plan_bom(compiled.graph, 100, eligible_pieces={(4, "cover"): 30})
    assert {(m.product_id, m.route_key): m.purchase_sheets for m in plan.materials} == {
        (3, "whole"): 100, (4, "cover"): 70, (4, "base"): 100}
    snap = next(r for r in compiled.snapshots if r.component_product_id == 4)
    assert snap.snapshot_component_report_length_mm == 800
    assert snap.snapshot_component_base_report_length_mm == 700


def test_shared_product_once_but_all_true_edges_retained(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    save(db, actor, 1, "manufactured", [(2, 1, "accompany"), (3, 1, "accompany")])
    db.commit()
    compiled = compile_master_order_bom(db, item)
    assert len(compiled.snapshots) == 4
    shared = next(r for r in compiled.snapshots if r.component_product_id == 3)
    assert shared.required_piece_quantity == 300
    assert shared.product_bom_component_id is None
    plan = plan_bom(compiled.graph, 100, eligible_stock={3: 50})
    assert next(m.purchase_sheets for m in plan.materials if m.product_id == 3) == 125
    assert len(compiled.graph.edges) == 4


def test_conflicting_shared_process_not_silently_netting_stock(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    save(db, actor, 1, "manufactured", [(2, 1, "accompany"), (3, 1, "accompany")])
    row = db.scalar(select(ProductBomComponent).where(ProductBomComponent.parent_product_id == 1,
                                                    ProductBomComponent.component_product_id == 3))
    row.spare_sheet_quantity = 3
    db.commit()
    with pytest.raises(BomPlanError, match="共享子件"):
        compile_master_order_bom(db, item)


def test_detached_material_snapshot_not_changed_by_later_master_edit(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    product = db.get(Product, 3)
    product.report_length_mm = 560
    product.production_notes = "原生产注意事项"
    db.commit()
    compiled = compile_master_order_bom(db, item)
    product.report_length_mm = 999
    product.production_notes = "新注意事项"
    product.default_cutting_mode = "一开六"
    db.commit()
    snap = next(r for r in compiled.snapshots if r.component_product_id == 3)
    assert snap.snapshot_component_report_length_mm == 560
    assert snap.snapshot_component_production_notes == "原生产注意事项"
    assert next(m.purchase_sheets for m in plan_bom(compiled.graph, 100).materials if m.product_id == 3) == 100


def test_invalid_or_legacy_root_never_compiles_by_guessing(context):
    db, actor, item, _ = context
    with pytest.raises(BomPlanError, match="尚未配置"):
        compile_master_order_bom(db, item)
    setup_liner(db, actor)
    item.quantity = 0
    with pytest.raises(BomPlanError, match="正整数"):
        compile_master_order_bom(db, item)


def test_freeze_material_and_recipe_once_then_replay_without_current_master(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    first = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    ids = [r.id for r in first.snapshots]
    db.commit()
    db.get(Product, 3).default_cutting_mode = "一开六"
    db.get(Product, 3).product_name = "新版长片"
    db.get(Product, 3).version += 1
    db.commit()
    again = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    assert [r.id for r in again.snapshots] == ids
    assert next(n.name for n in again.graph.nodes if n.product_id == 3) == "长片"
    assert next(m.purchase_sheets for m in plan_bom(again.graph, 100).materials if m.product_id == 3) == 100


def test_whole_snapshot_transaction_is_rolled_back_on_cancel(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.rollback()
    assert db.get(OrderBomGraph, item.id) is None
    assert db.scalar(select(func.count()).select_from(SalesOrderItemBomComponent)) == 0


def test_failure_after_graph_insert_rolls_back_all_sources(context, monkeypatch):
    db, actor, item, _ = context
    setup_liner(db, actor)
    from app.services import multilevel_bom_orders as service
    def fail(*args, **kwargs):
        raise BomPlanError("注入材料校验失败")
    monkeypatch.setattr(service, "read_compiled_order_bom", fail)
    with pytest.raises(BomPlanError, match="注入"):
        freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    assert db.get(OrderBomGraph, item.id) is None
    assert db.scalar(select(func.count()).select_from(SalesOrderItemBomComponent)) == 0


def test_missing_material_row_not_rebuilt_from_live_master(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    compiled = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    db.delete(compiled.snapshots[-1])
    db.commit()
    with pytest.raises(BomPlanError, match="不完整"):
        freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    assert db.scalar(select(func.count()).select_from(SalesOrderItemBomComponent)) == 3


def test_material_yield_tampering_fails_read_validation(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    compiled = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    next(r for r in compiled.snapshots if r.component_product_id == 3).snapshot_component_default_cutting_mode = "一开六"
    db.flush()
    with pytest.raises(BomPlanError, match="物理片组"):
        read_compiled_order_bom(db, item.id)


def test_a3_route_order_survives_canonical_graph_serialization(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    db.get(Product, 3).box_style = "A3 天地盖"
    db.commit()
    compiled = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    assert {r.key for n in compiled.graph.nodes if n.product_id == 3 for r in n.routes} == {"cover", "base"}


def test_existing_legacy_snapshot_not_overwritten_with_graph(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    old = compile_master_order_bom(db, item).snapshots[0]
    old.snapshot_schema_version = 4
    db.add(old)
    db.commit()
    with pytest.raises(BomPlanError, match="审计转换"):
        freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    assert db.get(OrderBomGraph, item.id) is None


def test_assembled_output_does_not_require_its_legacy_board_mold(context):
    db, actor, item, _ = context
    save(db, actor, 1, "assembled", [(3, 3, "assembly"), (4, 4, "assembly")])
    db.get(Product, 1).box_category = "die_cut"
    db.commit()
    compiled = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    assert next(n for n in compiled.graph.nodes if n.product_id == 1).routes == ()
    assert next(r for r in compiled.snapshots if r.component_product_id == 1).is_die_cut is False


def test_delivered_order_cannot_be_converted_by_new_order_freezer(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    item.delivered_quantity = 1
    db.commit()
    with pytest.raises(BomPlanError, match="已有送货"):
        freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    assert db.get(OrderBomGraph, item.id) is None
    assert db.scalar(select(func.count()).select_from(SalesOrderItemBomComponent)) == 0
