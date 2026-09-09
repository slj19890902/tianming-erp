from datetime import date, datetime

import pytest
from fastapi import HTTPException

from app.api.requisition import (
    _bom_pending_component_requirements, _bom_snapshot_requirements,
    _composite_parent_requisition_is_suppressed,
)
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, WarehouseLocation
from app.services.multilevel_bom_orders import freeze_master_order_bom
from app.services.multilevel_bom_requirements import read_graph_requirements
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_compile import setup_liner
from tests.test_multilevel_bom_master import save
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login, _component_payload


def frozen(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    compiled = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    return db, item, compiled


def reserve(db, item, snapshot, quantity, *, consumed=0, released=0, semi=False):
    key = f"test-{snapshot.id if snapshot else 'root'}-{quantity}-{semi}"
    location = WarehouseLocation(location_code=key, location_name=key, warehouse_type="shared")
    db.add(location)
    db.flush()
    lot = InventoryLot(lot_number=key, inventory_type="semi_finished" if semi else "finished",
        warehouse_location_id=location.id, quantity_available=0,
        quantity_reserved=quantity-consumed-released, quantity_consumed=consumed,
        quantity_damaged=0, quantity_scrapped=0, unit="sheets" if semi else "boxes",
        status="active", source_type="manual", stock_date=date(2026, 9, 9),
        last_movement_at=datetime(2026, 9, 9))
    db.add(lot)
    db.flush()
    db.add(InventoryReservation(reservation_number=key, inventory_lot_id=lot.id,
        reservation_type="semi_order" if semi else "finished_order", order_item_id=item.id,
        sales_order_item_bom_component_id=snapshot.id if snapshot else None,
        reserved_stock_quantity=quantity, credited_requirement_quantity=quantity,
        consumed_stock_quantity=consumed, consumed_requirement_quantity=consumed,
        released_stock_quantity=released, released_requirement_quantity=released, yield_factor=1,
        status="active"))
    db.commit()


def test_material_pending_has_root_once_and_no_assembled_liner(context):
    db, item, compiled = frozen(context)
    assert _composite_parent_requisition_is_suppressed(item, compiled.snapshots)
    rows = _bom_pending_component_requirements(db, item)
    assert [(r["product_id"], r["requisition_qty"]) for r in rows] == [(1, 100), (3, 100), (4, 150)]
    liner = next(s for s in compiled.snapshots if s.component_product_id == 2)
    with pytest.raises(HTTPException) as error:
        _bom_snapshot_requirements(db, liner)
    assert error.value.status_code == 409
    assert "纸板材料" in error.value.detail


def test_reserved_liner_nets_children_before_leaf_stock_and_physical_credit(context):
    db, item, compiled = frozen(context)
    snaps = {s.component_product_id: s for s in compiled.snapshots}
    reserve(db, item, snaps[2], 20)
    reserve(db, item, snaps[3], 10)
    reserve(db, item, snaps[3], 6, semi=True)
    rows = {r["product_id"]: r for r in _bom_pending_component_requirements(db, item)}
    assert rows[1]["requisition_qty"] == 100
    assert rows[3]["required_piece_quantity"] == 160
    assert rows[3]["remaining_required_piece_qty"] == 144
    assert rows[3]["requisition_qty"] == 72
    assert rows[4]["requisition_qty"] == 120
    graph = read_graph_requirements(db, item.id)
    assert [(m.product_id, m.purchase_sheets) for m in graph.plan.materials] == [(1, 100), (3, 72), (4, 120)]


def test_root_delivered_not_double_counted_but_accompany_still_required(context):
    db, item, compiled = frozen(context)
    snaps = {s.component_product_id: s for s in compiled.snapshots}
    item.delivered_quantity = 20
    db.commit()
    reserve(db, item, None, 20, consumed=20)
    reserve(db, item, snaps[1], 15, consumed=5, released=2)
    result = read_graph_requirements(db, item.id)
    assert result.finished_units[1] == 28
    assert [(m.product_id, m.purchase_sheets) for m in result.plan.materials] == [(1, 72), (3, 100), (4, 150)]


def test_assembled_root_only_buys_leaf_materials_after_root_credit(context):
    db, actor, item, _ = context
    save(db, actor, 1, "assembled", [(3, 3, "assembly"), (4, 4, "assembly")])
    compiled = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    reserve(db, item, compiled.snapshots[0], 20)
    rows = _bom_pending_component_requirements(db, item)
    assert {r["product_id"]: r["required_piece_quantity"] for r in rows} == {3: 240, 4: 320}


def test_fully_covered_does_not_purchase_spare_sheets(context):
    db, actor, item, _ = context
    setup_liner(db, actor)
    from app.models.product_bom import ProductBomComponent
    from sqlalchemy import select
    edge = db.scalar(select(ProductBomComponent).where(ProductBomComponent.component_product_id == 3))
    edge.spare_sheet_quantity = 10
    db.commit()
    compiled = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    liner = next(s for s in compiled.snapshots if s.component_product_id == 2)
    reserve(db, item, liner, 100)
    rows = {r["product_id"]: r for r in _bom_pending_component_requirements(db, item)}
    assert rows[3]["requisition_qty"] == 0
    assert rows[3]["spare_sheet_quantity"] == 0
    assert not rows[3]["can_requisition"]


def test_cached_flat_overrides_cannot_change_authoritative_graph_demand(context):
    db, item, compiled = frozen(context)
    leaf = next(s for s in compiled.snapshots if s.component_product_id == 3)
    result = _bom_snapshot_requirements(db, leaf, effective_sets_override=999,
        required_piece_quantity_override=999,
        inventory_coverage_override={"total_piece_quantity": 999, "finished_piece_quantity": 999,
                                     "semi_piece_quantity": 0})
    assert result["effective_set_quantity"] == 100
    assert result["required_piece_quantity"] == 200
    assert result["requisition_qty"] == 100


def test_graph_batch_api_persists_only_true_materials_and_rejects_duplicate(composite_requisition_app):
    from fastapi.testclient import TestClient
    from sqlalchemy import delete, select
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent, RequisitionItemBomSource
    from app.models.user import User
    from app.models.material import Material
    app, sessions = composite_requisition_app
    with sessions() as db:
        # This fixture is a brand-new anonymous temporary database, not a
        # conversion of any formal or historical order snapshot.
        db.execute(delete(SalesOrderItemBomComponent))
        actor = db.scalar(select(User))
        material = Material(code="K616K", supplier_name="N039 供应商")
        db.add(material)
        db.flush()
        for pid in (1, 2, 3):
            p = db.get(Product, pid)
            p.report_length_mm = 1000
            p.report_width_mm = 700
            p.material_id = material.id
        save(db, actor, 1, "assembled", [(2, 3, "assembly"), (3, 4, "assembly")])
        item = db.get(OrderItem, 1)
        compiled = freeze_master_order_bom(db, order_item_id=1, actor=actor)
        db.commit()
        snaps = {s.component_product_id: s for s in compiled.snapshots}
        reserve(db, item, snaps[1], 2)
        source_ids = [snaps[2].id, snaps[3].id]
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200, pending.text
        reqs = pending.json()["items"][0]["component_requirements"]
        assert {r["product_id"]: r["requisition_qty"] for r in reqs} == {2: 24, 3: 32}
        payload = {"supplier_name": "N039 供应商", "items": [_component_payload(sid) for sid in source_ids]}
        created = client.post("/api/requisition/batches", json=payload)
        assert created.status_code == 201, created.text
        repeated = client.post("/api/requisition/batches", json=payload)
        assert repeated.status_code == 409, repeated.text
    with sessions() as db:
        sources = db.scalars(select(RequisitionItemBomSource)).all()
        assert {s.sales_order_item_bom_component_id: int(s.required_piece_quantity) for s in sources} == {
            source_ids[0]: 24, source_ids[1]: 32}
        assert all(s.demand_basis == "order_specific_pieces" for s in sources)
