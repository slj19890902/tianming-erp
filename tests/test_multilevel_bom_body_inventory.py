from datetime import datetime

import pytest
from sqlalchemy import select, func

from app.models.production import ProductionTask, ProductionCompletionBatch, ProductionCompletion
from app.models.product import Product
from app.models.user import User
from app.models.multilevel_bom import BomBodyInventoryDetail
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation, WarehouseLocation
from app.services.multilevel_bom_body_inventory import receive_body_inventory
from app.services.multilevel_bom_orders import freeze_order_graph
from app.services.multilevel_bom_plan import FrozenBom, ProductNode, BomEdge, MaterialRoute
from app.services.bom_subkits import SubkitError
from tests.test_n039_composite_bom_requisition import composite_requisition_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _seed_material_and_staging


def setup(factory):
    with factory() as db:
        db.get(WarehouseLocation, 1).location_code = "BODY-OLD-FIXTURE"
        db.commit()
    _seed_material_and_staging(factory)
    with factory() as db:
        products = [db.get(Product, pid) for pid in (1,2)]
        graph = FrozenBom(1, 1, tuple(ProductNode(p.id,1,p.version,p.product_name,p.unit,
            "manufactured" if p.id==1 else "purchased", (MaterialRoute("whole"),) if p.id==1 else ())
            for p in products), (BomEdge(1,2,2,"assembly"),))
        freeze_order_graph(db, order_item_id=1, graph=graph, actor=db.get(User,1))
        task = ProductionTask(order_item_id=1)
        batch = ProductionCompletionBatch(idempotency_key="body-test", request_hash="a"*64,
            item_count=1, completed_by=1, completed_at=datetime(2026,9,10))
        db.add_all([task,batch])
        db.flush()
        location = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code=="F1-FIN-001-L001"))
        completion = ProductionCompletion(batch_id=batch.id, task_id=task.id, order_item_id=1,
            expected_version=1, quantity=5, initial_disposition="stock", origin="receipt_auto",
            warehouse_location_id=location.id, completed_by=1, completed_at=datetime(2026,9,10))
        db.add(completion)
        db.commit()
        return completion.id, location.id


def test_body_post_replay_and_outer_rollback(composite_requisition_app, _p181_published_map_identity):
    _, factory = composite_requisition_app
    cid, lid = setup(factory)
    args = dict(completion_id=cid, location_id=lid, operator_id=1,
                idempotency_key="body-in", expected_layout_version=2)
    with factory() as db:
        lot = receive_body_inventory(db, **args)
        assert lot.inventory_type == "assembly_body" and lot.finished_detail is None
        assert lot.quantity_available == 5 and lot.quantity_reserved == 0
        assert db.get(BomBodyInventoryDetail,lot.id).product_id == 1
        assert db.scalar(select(func.count()).select_from(InventoryReservation)) == 0
        assert receive_body_inventory(db, **args).id == lot.id
        with pytest.raises(SubkitError):
            receive_body_inventory(db, **{**args,"expected_layout_version":3})
        db.rollback()
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(BomBodyInventoryDetail)) == 0
        assert db.get(ProductionCompletion,cid).inventory_lot_id is None
        lot = receive_body_inventory(db, **args)
        db.commit()
        assert receive_body_inventory(db, **args).id == lot.id
        assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 1


def test_body_post_failure_rolls_back_stock_detail_and_completion(composite_requisition_app, _p181_published_map_identity, monkeypatch):
    _, factory = composite_requisition_app
    cid, lid = setup(factory)
    from app.services import warehouse_inventory
    original = warehouse_inventory._movement
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("after stock detail and movement")
    monkeypatch.setattr(warehouse_inventory,"_movement",fail)
    with factory() as db:
        with pytest.raises(RuntimeError):
            receive_body_inventory(db, completion_id=cid, location_id=lid, operator_id=1,
                idempotency_key="body-fail", expected_layout_version=2)
        db.commit()
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0
        assert db.scalar(select(func.count()).select_from(BomBodyInventoryDetail)) == 0
        assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 0
        assert db.get(ProductionCompletion,cid).inventory_lot_id is None


def test_actual_completion_stock_branch_posts_body_without_finished_reservation(composite_requisition_app, _p181_published_map_identity):
    _, factory = composite_requisition_app
    cid, lid = setup(factory)
    from app.models.order import Order, OrderItem
    from app.services.production_workflow import _stock_completion_lot, CompletionCommand
    with factory() as db:
        completion = db.get(ProductionCompletion,cid)
        item = db.get(OrderItem,1)
        lot = _stock_completion_lot(db, completion=completion,
            task=db.get(ProductionTask,completion.task_id), item=item, order=db.get(Order,item.order_id),
            command=CompletionCommand(task_id=completion.task_id, expected_version=1,
                disposition="stock", location_id=lid, expected_layout_version=2),
            operator_id=1, idempotency_prefix="body-actual-branch", source_type="production_completion")
        assert lot.inventory_type == "assembly_body"
        assert lot.finished_detail is None and lot.quantity_available == 5
        assert db.scalar(select(func.count()).select_from(InventoryReservation)) == 0
        db.commit()


@pytest.mark.parametrize("invalid", ["manual", "reversed", "inactive_actor", "ended_order"])
def test_invalid_body_source_cannot_write(composite_requisition_app, _p181_published_map_identity, invalid):
    _, factory = composite_requisition_app
    cid, lid = setup(factory)
    from app.models.order import Order
    with factory() as db:
        completion = db.get(ProductionCompletion,cid)
        if invalid == "manual":
            completion.origin = "manual"
        elif invalid == "reversed":
            completion.status = "reversed"
        elif invalid == "inactive_actor":
            db.get(User,1).is_active = False
        else:
            db.get(Order,1).status = "completed"
        db.commit()
        with pytest.raises(SubkitError):
            receive_body_inventory(db, completion_id=cid, location_id=lid, operator_id=1,
                idempotency_key="invalid-body", expected_layout_version=2)
        db.commit()
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0
