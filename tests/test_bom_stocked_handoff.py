"""Operational stock handoff on a fresh private copy, preserving completed history."""
import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.product import Product
from app.models.production import ProductionCompletion
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.composite_bom import get_product_bom
from app.services.multilevel_bom_external_freeze import freeze_order_procurement
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_stocked_handoff import review_stocked_handoff, execute_stocked_handoff
from tests.test_bom_other_products_acceptance import factory_http, paper_receipt_flow
from tests.test_multilevel_bom_factory_compile import factory_copy, new_item
from tests.test_multilevel_bom_master import save
from tests.test_multilevel_bom_external_receipts import receive
from tests.test_p1_33c5_external_packaging_receiving import _confirm


def test_received_separate_children_handoff_assembles_only_order_stock(factory_http, monkeypatch):
    from app.services.composite_bom import replace_product_bom
    from app.models.multilevel_bom import BomAssembly
    client, db = factory_http
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    replace_product_bom(db, parent_product_id=3799, expected_version=db.get(Product,3799).version,
        user=actor, inventory_mode="separate", material_mode="expand_children", delivery_mode="components",
        components=[dict(component_product_id=pid, quantity_per_set=qty, inventory_relation="accompany")
                    for pid, qty in [(3771,3),(3783,4)]])
    item = new_item(db, 3799, 2)
    original = freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    db.commit()
    paper_receipt_flow(client, db, original, item.id, 3799, stop_after_receipts=True)
    before = {row.id:(row.quantity,row.actual_output_quantity,row.order_reserved_quantity)
        for row in db.scalars(select(ProductionCompletion).where(ProductionCompletion.order_item_id==item.id))}
    replace_product_bom(db, parent_product_id=3799, expected_version=db.get(Product,3799).version,
        user=actor, inventory_mode="assembled", material_mode="expand_children", delivery_mode="parent",
        components=[dict(component_product_id=pid, quantity_per_set=qty, inventory_relation="assembly")
                    for pid, qty in [(3771,3),(3783,4)]])
    db.commit()
    url = f"/api/orders/items/{item.id}/stocked-bom-cutover"
    preview = client.post(url+"/preview",json={})
    assert preview.status_code == 200, preview.text
    assert not preview.json()["ready"]
    preview = client.post(url+"/preview",json={"target_locations":{"3799":1203}})
    assert preview.status_code == 200, preview.text
    review = preview.json()
    assert review["ready"] and review["outputs"][0]["quantity"] == 2
    payload = {key:review[key] for key in ("reviewed_hash","preview_hash","source_lot_versions","target_locations","rule_revision")}
    payload["operation_key"] = "stocked-separated-assemble"
    # A concurrent inventory update invalidates the entire reviewed operation.
    lid = int(next(iter(review["source_lot_versions"])))
    db.get(InventoryLot,lid).version += 1
    db.commit()
    assert client.post(url+"/execute",json=payload).status_code == 409
    preview = client.post(url+"/preview",json={"target_locations":{"3799":1203}})
    assert preview.status_code == 200, preview.text
    payload.update({key:preview.json()[key] for key in ("reviewed_hash","preview_hash","source_lot_versions")})
    from app.models.multilevel_bom import OrderBomRuleRevision
    from app.services.multilevel_bom_cutover_review import _row
    from app.services import audit_log
    original_audit = audit_log.append_audit_event
    def fail_handoff_audit(*args, **kwargs):
        if kwargs.get("action_code") == "switch_stocked_graph_rule":
            raise RuntimeError("isolated late handoff audit failure")
        return original_audit(*args, **kwargs)
    db.expire_all()
    originals = {lot_id:_row(db.get(InventoryLot,lot_id)) for lot_id in map(int,payload["source_lot_versions"])}
    old_reservations = {row.id:_row(row) for row in db.scalars(select(InventoryReservation)
        .where(InventoryReservation.order_item_id==item.id))}
    from app.models.production import ProductionTask
    completed_tasks = {row.id:_row(row) for row in db.scalars(select(ProductionTask)
        .where(ProductionTask.order_item_id==item.id))}
    db.commit()
    monkeypatch.setattr(audit_log,"append_audit_event",fail_handoff_audit)
    with pytest.raises(RuntimeError,match="late handoff audit failure"):
        client.post(url+"/execute",json=payload)
    db.expire_all()
    assert {lot_id:_row(db.get(InventoryLot,lot_id)) for lot_id in originals} == originals
    assert {row.id:_row(row) for row in db.scalars(select(InventoryReservation)
        .where(InventoryReservation.order_item_id==item.id))} == old_reservations
    assert db.scalar(select(OrderBomRuleRevision.id).where(OrderBomRuleRevision.order_item_id==item.id)) is None
    assert db.scalar(select(BomAssembly.id).where(BomAssembly.order_item_id==item.id)) is None
    db.commit()
    monkeypatch.setattr(audit_log,"append_audit_event",original_audit)
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    def submit():
        barrier.wait(timeout=10)
        return client.post(url+"/execute",json=payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        result, concurrent = [future.result(timeout=30) for future in futures]
    assert result.status_code == 200, result.text
    assert concurrent.status_code == 200 and concurrent.json() == result.json(), concurrent.text
    replay = client.post(url+"/execute",json=payload)
    assert replay.status_code == 200 and replay.json() == result.json(), replay.text
    assert client.post(url+"/execute",json={**payload,"rule_revision":1}).status_code == 409
    db.expire_all()
    assert {row.id:_row(row) for row in db.scalars(select(ProductionTask)
        .where(ProductionTask.order_item_id==item.id))} == completed_tasks
    assert {row.id:(row.quantity,row.actual_output_quantity,row.order_reserved_quantity)
        for row in db.scalars(select(ProductionCompletion).where(ProductionCompletion.order_item_id==item.id))} == before
    lots = list(db.scalars(select(InventoryLot).join(InventoryReservation,
        InventoryReservation.inventory_lot_id==InventoryLot.id).where(InventoryReservation.order_item_id==item.id)).unique())
    assert sum(lot.quantity_available for lot in lots if lot.finished_detail.product_id==3771) == 2
    assert sum(lot.quantity_consumed for lot in lots if lot.finished_detail.product_id==3771) == 6
    assert sum(lot.quantity_consumed for lot in lots if lot.finished_detail.product_id==3783) == 8
    parents = [lot for lot in lots if lot.finished_detail.product_id==3799]
    assert len(parents) == 1 and parents[0].quantity_reserved == 2
    assert parents[0].warehouse_location_id == 1203
    assert sum(row.quantity for row in db.scalars(select(BomAssembly).where(BomAssembly.order_item_id==item.id))) == 2
    db.commit()
    delivery = client.post("/api/deliveries",json={"customer_id":original.graph.customer_id,
        "delivery_date":"2026-09-10","items":[{"order_item_id":item.id,"delivered_quantity":2}]})
    assert delivery.status_code == 201, delivery.text
    did = delivery.json()["id"]
    dispatch = client.put(f"/api/deliveries/{did}/dispatch")
    assert dispatch.status_code == 200, dispatch.text
    cancel = client.put(f"/api/deliveries/{did}/cancel")
    assert cancel.status_code == 200, cancel.text
    db.expire_all()
    assert item.delivered_quantity == 0 and parents[0].quantity_reserved == 2
    actor.role = "sales"
    db.commit()
    assert client.post(url+"/preview",json={}).status_code == 403
    assert client.post(url+"/execute",json=payload).status_code == 403


def test_received_manufactured_and_external_stock_handoff_keeps_history(factory_http):
    client, db = factory_http
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    children = [(row["component_product_id"], int(row["quantity_per_set"]), "accompany")
                for row in get_product_bom(db, 3479)["components"]]
    assert children and all(db.get(Product, pid).supply_mode == "external_purchase" for pid, _, _ in children)
    save(db, actor, 3479, "manufactured", children)
    item = new_item(db, 3479, 2)
    original = freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    db.commit()
    _confirm(client, item.order_id)
    db.expire_all()
    purchases = list(db.scalars(select(ExternalPackagingPurchaseItem).where(
        ExternalPackagingPurchaseItem.sales_order_item_id == item.id)))
    for line in purchases:
        result = receive(client, line.purchase_order_id, line.id, f"handoff-external-{line.id}", line.purchase_quantity)
        assert result.status_code == 200, result.text
    paper_receipt_flow(client, db, original, item.id, 3479, stop_after_first_delivery=True)
    before_completions = {row.id: (row.quantity, row.actual_output_quantity, row.order_reserved_quantity)
        for row in db.scalars(select(ProductionCompletion).where(ProductionCompletion.order_item_id == item.id))}
    old_reservations = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == item.id,
        InventoryReservation.reservation_type == "finished_order")))
    consumed = {row.id:row.consumed_stock_quantity for row in old_reservations}
    lot_ids = {row.inventory_lot_id for row in old_reservations}
    totals = {lid: sum(getattr(db.get(InventoryLot,lid), field) for field in
                      ("quantity_available","quantity_reserved","quantity_consumed")) for lid in lot_ids}
    save(db, actor, 3479, "manufactured", [])
    db.commit()
    review = review_stocked_handoff(db, order_item_id=item.id, customer_id=original.graph.customer_id, target_locations={})
    assert review.preview["ready"] and review.preview["execution_quantity"] == 1
    args = dict(order_item_id=item.id, customer_id=original.graph.customer_id,
        reviewed_hash=review.preview["reviewed_hash"], expected_revision=review.preview["rule_revision"],
        target_locations={}, source_lot_versions=review.preview["source_lot_versions"],
        operation_key="received-stock-rule-handoff", actor=actor)
    result = execute_stocked_handoff(db, **args)
    db.commit()
    assert execute_stocked_handoff(db, **args) == result
    db.commit()
    assert item.delivered_quantity == 1
    assert {row.id: (row.quantity, row.actual_output_quantity, row.order_reserved_quantity)
        for row in db.scalars(select(ProductionCompletion).where(ProductionCompletion.order_item_id == item.id))} == before_completions
    assert {row.id:row.consumed_stock_quantity for row in old_reservations} == consumed
    assert {lid: sum(getattr(db.get(InventoryLot,lid), field) for field in
                    ("quantity_available","quantity_reserved","quantity_consumed")) for lid in lot_ids} == totals
    current = read_compiled_order_bom(db, item.id)
    assert len(current.snapshots) == 1 and current.snapshots[0].required_piece_quantity == 1
    delivery = client.post("/api/deliveries", json={"customer_id": current.graph.customer_id,
        "delivery_date":"2026-09-10", "items":[{"order_item_id":item.id,"delivered_quantity":1}]})
    assert delivery.status_code == 201, delivery.text
    did = delivery.json()["id"]
    dispatch = client.put(f"/api/deliveries/{did}/dispatch")
    assert dispatch.status_code == 200, dispatch.text
    cancelled = client.put(f"/api/deliveries/{did}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    db.expire_all()
    assert item.delivered_quantity == 1


def test_stocked_carton_liner_handoff_preserves_two_physical_locations(factory_http):
    from app.models.bom_subkit import ProductSubkit
    from app.models.multilevel_bom import BomAssembly
    from app.services.bom_subkits import save_subkit
    from app.services.composite_bom import replace_product_bom
    from app.services.multilevel_bom_cutover_review import _row
    client, db = factory_http
    actor = db.scalar(select(User).where(User.role=="admin",User.is_active.is_(True)))
    old = db.get(ProductSubkit,3765)
    save_subkit(db,parent_product_id=3765,name=db.get(Product,3822).product_name,kits_per_parent=1,
        members=[dict(product_id=3788,pieces_per_kit=2),dict(product_id=3789,pieces_per_kit=6)],
        expected_version=old.version,actor=actor,enabled=False)
    save(db,actor,3822,"assembled",[(3788,2,"assembly"),(3789,6,"assembly")])
    save(db,actor,3765,"manufactured",[(3822,1,"accompany")])
    item = new_item(db,3765,2)
    original = freeze_order_procurement(db,order_item_id=item.id,actor=actor)
    db.commit()
    paper_receipt_flow(client,db,original,item.id,3765,stop_after_first_delivery=True)
    old_assemblies = {row.id:_row(row) for row in db.scalars(select(BomAssembly).where(BomAssembly.order_item_id==item.id))}
    replace_product_bom(db,parent_product_id=3765,expected_version=db.get(Product,3765).version,
        user=actor,inventory_mode="manufactured",material_mode="expand_children",delivery_mode="parent",
        components=[dict(component_product_id=3822,quantity_per_set=1,inventory_relation="accompany")])
    db.commit()
    url = f"/api/orders/items/{item.id}/stocked-bom-cutover"
    preview = client.post(url+"/preview",json={})
    assert preview.status_code == 200, preview.text
    review = preview.json()
    assert review["ready"] and review["execution_quantity"] == 1 and not review["outputs"]
    retained = [row for row in review["retained_locations"] if row["quantity"]]
    assert len(retained) == 2 and len({row["location_id"] for row in retained}) == 2
    payload = {key:review[key] for key in ("reviewed_hash","preview_hash","source_lot_versions","target_locations","rule_revision")}
    result = client.post(url+"/execute",json={**payload,"operation_key":"stocked-carton-liner"})
    assert result.status_code == 200, result.text
    assert not result.json()["assembly_ids"]
    db.expire_all()
    assert {row.id:_row(row) for row in db.scalars(select(BomAssembly).where(BomAssembly.order_item_id==item.id))} == old_assemblies
    assert item.delivered_quantity == 1
    delivery = client.post("/api/deliveries",json={"customer_id":original.graph.customer_id,
        "delivery_date":"2026-09-10","items":[{"order_item_id":item.id,"delivered_quantity":1}]})
    assert delivery.status_code == 201, delivery.text
    did = delivery.json()["id"]
    detail = client.get(f"/api/deliveries/{did}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["items"][0]["composite_fulfillment_mode"] == "parent_delivery"
    assert {row["location_id"] for row in detail.json()["items"][0]["inventory_sources"]} == {row["location_id"] for row in retained}
    dispatch = client.put(f"/api/deliveries/{did}/dispatch")
    assert dispatch.status_code == 200, dispatch.text
    cancelled = client.put(f"/api/deliveries/{did}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    db.expire_all()
    assert item.delivered_quantity == 1
