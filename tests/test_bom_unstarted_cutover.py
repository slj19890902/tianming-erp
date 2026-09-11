from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.product_bom import SalesOrderItemBomComponent
from app.services.composite_bom import create_order_item_bom_snapshots, replace_product_bom
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_unstarted_cutover import unstarted_preview, execute_unstarted_cutover
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_bom_other_products_acceptance import factory_http


def setup(db, mode="assembled", pid=3799, quantity=10):
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    product = db.get(Product, pid)
    order = Order(order_number=f"ISOLATED-UNSTARTED-{pid}", customer_id=product.customer_id,
                  order_date=date(2026, 9, 10))
    db.add(order)
    db.flush()
    item = OrderItem(order_id=order.id, product_id=pid, quantity=quantity, unit_price=Decimal("1"),
        subtotal=Decimal(quantity), snapshot_product_name=product.product_name,
        composite_fulfillment_mode_snapshot="parent_delivery")
    db.add(item)
    db.flush()
    create_order_item_bom_snapshots(db, order_item=item, parent_product=product)
    db.commit()
    history = list(db.scalars(select(SalesOrderItemBomComponent).where(
        SalesOrderItemBomComponent.sales_order_item_id == item.id)))
    replace_product_bom(db, parent_product_id=pid, expected_version=product.version,
        user=actor, inventory_mode=mode, material_mode="expand_children",
        delivery_mode="components" if mode == "separate" else "parent",
        components=[dict(component_product_id=row.component_product_id, quantity_per_set=row.quantity_per_set,
            inventory_relation="assembly" if mode == "assembled" else "accompany") for row in history])
    db.commit()
    return actor, item, history


def request(db, actor, item):
    preview = unstarted_preview(db, order_item_id=item.id, customer_id=item.order.customer_id)
    return dict(order_item_id=item.id, customer_id=item.order.customer_id,
        reviewed_hash=preview["reviewed_hash"], operation_key="unstarted-test", actor=actor)


@pytest.mark.parametrize("mode,pid", [("assembled", 3799), ("separate", 3799),
                                      ("manufactured", 2817), ("manufactured", 3479)])
def test_unstarted_modes_keep_old_sources_and_all_stock(factory_copy, mode, pid):
    db = factory_copy
    actor, item, history = setup(db, mode, pid)
    payload = request(db, actor, item)
    before = db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all()
    old = {row.id: row.required_piece_quantity for row in history}
    result = execute_unstarted_cutover(db, **payload)
    db.commit()
    assert result["output_lot_ids"] == []
    compiled = read_compiled_order_bom(db, item.id)
    assert compiled.graph.modes.inventory == ("body" if mode == "manufactured" else mode)
    assert compiled.execution_window.delivered_before == 0
    assert {row.id: row.required_piece_quantity for row in history} == old
    assert {row.id for row in compiled.snapshots}.isdisjoint(old)
    assert item.composite_fulfillment_mode_snapshot == ("component_delivery" if mode == "separate" else "parent_delivery")
    assert db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all() == before
    assert execute_unstarted_cutover(db, **payload) == result
    db.rollback()


def test_unstarted_audit_failure_rolls_back_and_same_key_retries(factory_copy, monkeypatch):
    db = factory_copy
    actor, item, _ = setup(db)
    payload = request(db, actor, item)
    from app.services import multilevel_bom_unstarted_cutover as service
    original = service.append_audit_event
    def fail(*args, **kwargs):
        raise RuntimeError("late audit failure")
    monkeypatch.setattr(service, "append_audit_event", fail)
    with pytest.raises(RuntimeError, match="late audit"):
        execute_unstarted_cutover(db, **payload)
    db.rollback()
    assert read_compiled_order_bom(db, item.id) is None
    monkeypatch.setattr(service, "append_audit_event", original)
    execute_unstarted_cutover(db, **payload)
    db.commit()


def test_unstarted_rejects_changed_master_and_inactive_admin(factory_copy):
    db = factory_copy
    actor, item, _ = setup(db)
    payload = request(db, actor, item)
    db.get(Product, 3771).version += 1
    db.commit()
    with pytest.raises(BomPlanError, match="已变化"):
        execute_unstarted_cutover(db, **payload)
    db.rollback()
    payload = request(db, actor, item)
    actor.is_active = False
    db.commit()
    with pytest.raises(BomPlanError, match="活动管理员"):
        execute_unstarted_cutover(db, **payload)


@pytest.mark.parametrize("same_key", [True, False])
def test_two_connections_cannot_apply_the_same_preview_twice(factory_copy, same_key):
    """Real concurrent transactions, not two requests sharing one Session."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    from app.models.multilevel_bom import OrderBomExecutionCutover

    db = factory_copy
    actor, item, history = setup(db)
    payload = request(db, actor, item)
    actor_id, item_id = actor.id, item.id
    old_ids = {row.id for row in history}
    payload.pop("actor")
    stock_before = db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all()
    engine = db.get_bind()
    db.rollback()
    barrier = Barrier(2)

    def submit(index):
        with Session(engine) as connection:
            current_actor = connection.get(User, actor_id)
            barrier.wait(timeout=10)
            args = {**payload, "actor": current_actor}
            if not same_key:
                args["operation_key"] = f"concurrent-unstarted-{index}"
            try:
                result = execute_unstarted_cutover(connection, **args)
                connection.commit()
                return "ok", result
            except BomPlanError as exc:
                connection.rollback()
                return "conflict", str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, (1, 2)))
    if same_key:
        assert results[0] == results[1]
        assert results[0][0] == "ok"
    else:
        assert sorted(row[0] for row in results) == ["conflict", "ok"]
        assert "订单已切换" in next(row[1] for row in results if row[0] == "conflict")
    db.expire_all()
    assert db.get(OrderBomExecutionCutover, item_id) is not None
    compiled = read_compiled_order_bom(db, item_id)
    assert {row.id for row in compiled.snapshots}.isdisjoint(old_ids)
    all_sources = set(db.scalars(select(SalesOrderItemBomComponent.id).where(
        SalesOrderItemBomComponent.sales_order_item_id == item_id)))
    assert all_sources == old_ids | {row.id for row in compiled.snapshots}
    assert db.scalar(text("SELECT count(*) FROM operation_logs WHERE action_code='switch_unstarted_bom'")) == 1
    assert db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all() == stock_before


def test_admin_http_preview_execute_and_uncertain_result_replay(factory_http):
    client, db = factory_http
    actor, item, _ = setup(db, "separate")
    url = f"/api/orders/items/{item.id}/unstarted-bom-cutover"
    preview = client.post(url + "/preview", json={})
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["new_modes"]["delivery"] == "components"
    assert data["old_sources"] and data["new_sources"] and data["new_requirements"]["materials"]
    payload = {key: data[key] for key in ("target_locations", "source_lot_versions", "reviewed_hash", "preview_hash")}
    payload["operation_key"] = "http-unstarted-same-key"
    first = client.post(url + "/execute", json=payload)
    assert first.status_code == 200, first.text
    replay = client.post(url + "/execute", json=payload)
    assert replay.status_code == 200 and replay.json() == first.json()
    db.expire_all()
    assert db.get(OrderItem, item.id).composite_fulfillment_mode_snapshot == "component_delivery"
    assert db.execute(text("SELECT count(*) FROM operation_logs WHERE action_code='switch_unstarted_bom'")).scalar() == 1
    changed = client.post(url + "/execute", json={**payload, "operation_key": "another-key"})
    assert changed.status_code == 409
    actor.role = "sales"
    db.commit()
    assert client.post(url + "/execute", json=payload).status_code == 403


def test_unstarted_entry_does_not_accept_existing_execution(factory_http):
    client, db = factory_http
    from tests.test_bom_cutover_review import prepare
    prepare(db)
    before = db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all()
    response = client.post("/api/orders/items/10050/unstarted-bom-cutover/preview", json={})
    assert response.status_code == 409 and "未开始" in response.json()["detail"]
    assert db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all() == before


@pytest.mark.parametrize("mode,pid", [("assembled", 3799), ("separate", 3799),
                                      ("manufactured", 2817), ("manufactured", 3479)])
def test_unstarted_body_switch_continues_to_receipt_shipping_and_cancel(factory_http, mode, pid):
    client, db = factory_http
    actor, item, _ = setup(db, mode, pid, quantity=2)
    execute_unstarted_cutover(db, **request(db, actor, item))
    db.commit()
    compiled = read_compiled_order_bom(db, item.id)
    if any(node.source == "purchased" for node in compiled.graph.nodes):
        from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
        from tests.test_p1_33c5_external_packaging_receiving import _confirm
        from tests.test_multilevel_bom_external_receipts import receive
        _confirm(client, item.order_id)
        db.expire_all()
        lines = list(db.scalars(select(ExternalPackagingPurchaseItem).where(
            ExternalPackagingPurchaseItem.sales_order_item_id == item.id)))
        assert lines
        for line in lines:
            received = receive(client, line.purchase_order_id, line.id,
                f"unstarted-external-{line.id}", line.purchase_quantity)
            assert received.status_code == 200, received.text
    from tests.test_bom_other_products_acceptance import paper_receipt_flow
    paper_receipt_flow(client, db, compiled, item.id, pid)
