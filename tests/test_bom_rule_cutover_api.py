import pytest
from sqlalchemy import select, text

from app.models.product import Product
from app.models.multilevel_bom import OrderBomRuleRevision
from app.models.product_bom import SalesOrderItemBomComponent
from app.services.composite_bom import replace_product_bom
from app.services.multilevel_bom_orders import read_compiled_order_bom
from tests.test_bom_other_products_acceptance import factory_http, paper_receipt_flow
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_multilevel_bom_rule_impact import frozen_order
from tests.test_multilevel_bom_master import save


def configure(db, actor, mode="assembled"):
    product = db.get(Product, 3799)
    replace_product_bom(db, parent_product_id=product.id, expected_version=product.version,
        user=actor, inventory_mode=mode, material_mode="expand_children",
        delivery_mode="components" if mode == "separate" else "parent",
        components=[dict(component_product_id=pid, quantity_per_set=4,
            inventory_relation="accompany" if mode == "separate" else "assembly") for pid in (3771, 3783)])
    db.commit()


def preview(client, item, key="new-rule-http"):
    url = f"/api/orders/items/{item.id}/unstarted-bom-cutover"
    response = client.post(url + "/preview", json={})
    assert response.status_code == 200, response.text
    data = response.json()
    payload = {name: data[name] for name in ("reviewed_hash", "preview_hash", "source_lot_versions", "target_locations", "rule_revision")}
    payload["operation_key"] = key
    return url, data, payload


@pytest.mark.parametrize("mode", ["assembled", "separate"])
def test_admin_changes_existing_frozen_graph_then_receives_delivers_and_reverses(factory_http, mode):
    client, db = factory_http
    actor, item, original = frozen_order(db, quantity=2)
    old_ids = {row.id for row in original.snapshots}
    configure(db, actor, mode)
    url, data, payload = preview(client, item)
    assert payload["rule_revision"] == 0
    assert data["rule_impact"]["inventory_credits_applied"] is False
    before = db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all()
    first = client.post(url + "/execute", json=payload)
    assert first.status_code == 200, first.text
    replay = client.post(url + "/execute", json=payload)
    assert replay.status_code == 200 and replay.json() == first.json()
    db.expire_all()
    assert db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all() == before
    current = read_compiled_order_bom(db, item.id)
    assert current.history_source_ids == old_ids
    assert current.graph.modes.inventory == mode
    detail = client.get(f"/api/orders/{item.order_id}")
    assert detail.status_code == 200, detail.text
    detail_item = next(row for row in detail.json()["items"] if row["id"] == item.id)
    assert {row["id"] for row in detail_item["bom_components"]} == {row.id for row in current.snapshots}
    assert next(row for row in original.snapshots if row.component_product_id == 3771).required_piece_quantity == 6
    paper_receipt_flow(client, db, current, item.id, 3799)
    assert db.scalar(text("SELECT count(*) FROM operation_logs WHERE action_code='switch_unstarted_graph_rule'")) == 1
    # Replaying the successful request remains safe after execution facts exist.
    assert client.post(url + "/execute", json=payload).json() == first.json()


def test_rule_cutover_late_audit_failure_rolls_back_and_retries_same_key(factory_http, monkeypatch):
    client, db = factory_http
    actor, item, original = frozen_order(db)
    configure(db, actor)
    url, _, payload = preview(client, item)
    source_ids = {row.id for row in original.snapshots}
    from app.services import multilevel_bom_rule_cutover as service
    original_append = service.append_audit_event
    def fail(*args, **kwargs):
        raise RuntimeError("late structural audit failure")
    monkeypatch.setattr(service, "append_audit_event", fail)
    with pytest.raises(RuntimeError, match="late structural audit"):
        client.post(url + "/execute", json=payload)
    db.expire_all()
    assert db.scalar(select(OrderBomRuleRevision.id).where(OrderBomRuleRevision.order_item_id == item.id)) is None
    assert set(db.scalars(select(SalesOrderItemBomComponent.id).where(
        SalesOrderItemBomComponent.sales_order_item_id == item.id))) == source_ids
    assert {row.id for row in read_compiled_order_bom(db, item.id).snapshots} == source_ids
    monkeypatch.setattr(service, "append_audit_event", original_append)
    result = client.post(url + "/execute", json=payload)
    assert result.status_code == 200, result.text


def test_admin_rule_revision_rejects_stale_preview_payload_and_permission(factory_http):
    client, db = factory_http
    actor, item, _ = frozen_order(db)
    configure(db, actor)
    url, _, payload = preview(client, item)
    db.get(Product, 3771).version += 1
    db.commit()
    assert client.post(url + "/execute", json=payload).status_code == 409
    _, _, payload = preview(client, item)
    assert client.post(url + "/execute", json={**payload, "rule_revision": True}).status_code == 422
    result = client.post(url + "/execute", json=payload)
    assert result.status_code == 200, result.text
    assert client.post(url + "/execute", json={**payload, "rule_revision": 1}).status_code == 409
    actor.role = "sales"
    db.commit()
    assert client.post(url + "/execute", json=payload).status_code == 403


def test_successive_admin_rules_preserve_earlier_idempotency_result(factory_http):
    client, db = factory_http
    actor, item, original = frozen_order(db)
    configure(db, actor)
    url, _, first_payload = preview(client, item, "rule-one")
    first = client.post(url + "/execute", json=first_payload)
    assert first.status_code == 200, first.text
    db.expire_all()
    save(db, actor, 3799, "assembled", [(3771, 2, "assembly"), (3783, 4, "assembly")])
    db.commit()
    _, _, second_payload = preview(client, item, "rule-two")
    assert second_payload["rule_revision"] == 1
    second = client.post(url + "/execute", json=second_payload)
    assert second.status_code == 200, second.text
    assert second.json()["rule_revision"] == 2
    assert client.post(url + "/execute", json=first_payload).json() == first.json()
    db.expire_all()
    assert len(read_compiled_order_bom(db, item.id).history_source_ids) == 2 * len(original.snapshots)


def test_admin_adds_existing_multilevel_component_then_completes_receipt_delivery(factory_http):
    client, db = factory_http
    actor, item, original = frozen_order(db, quantity=2)
    save(db, actor, 3822, "assembled", [(3788, 2, "assembly"), (3789, 6, "assembly")])
    save(db, actor, 3799, "assembled", [(3771, 3, "assembly"), (3783, 4, "assembly"), (3822, 1, "assembly")])
    db.commit()
    url, data, payload = preview(client, item, "new-multilevel-rule")
    assert {row["component_product_id"] for row in data["new_sources"]} == {3799, 3771, 3783, 3822, 3788, 3789}
    saved = client.post(url + "/execute", json=payload)
    assert saved.status_code == 200, saved.text
    db.expire_all()
    current = read_compiled_order_bom(db, item.id)
    assert current.history_source_ids == {row.id for row in original.snapshots}
    paper_receipt_flow(client, db, current, item.id, 3799)


def test_admin_revises_body_accompaniment_without_changing_supply_mode(factory_http):
    from app.models.user import User
    from app.services.composite_bom import get_product_bom
    from app.services.multilevel_bom_external_freeze import freeze_order_procurement
    from tests.test_multilevel_bom_factory_compile import new_item
    client, db = factory_http
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    components = get_product_bom(db, 2817)["components"]
    rows = [(row["component_product_id"], int(row["quantity_per_set"]), "accompany") for row in components]
    assert rows
    save(db, actor, 2817, "manufactured", rows)
    item = new_item(db, 2817, 2)
    original = freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    db.commit()
    changed = [(pid, qty + (index == 0), relation) for index, (pid, qty, relation) in enumerate(rows)]
    save(db, actor, 2817, "manufactured", changed)
    db.commit()
    url, _, payload = preview(client, item, "body-rule")
    saved = client.post(url + "/execute", json=payload)
    assert saved.status_code == 200, saved.text
    db.expire_all()
    current = read_compiled_order_bom(db, item.id)
    assert {node.product_id: node.source for node in current.graph.nodes} == {
        node.product_id: node.source for node in original.graph.nodes}
    paper_receipt_flow(client, db, current, item.id, 2817)


@pytest.mark.parametrize("same_key", [True, False])
def test_two_real_connections_apply_one_structural_revision(factory_copy, same_key):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    from app.models.user import User
    from app.services.multilevel_bom_plan import BomPlanError
    from app.services.multilevel_bom_rule_cutover import rule_cutover_preview, execute_rule_cutover
    db = factory_copy
    actor, item, _ = frozen_order(db)
    configure(db, actor)
    data = rule_cutover_preview(db, order_item_id=item.id, customer_id=item.order.customer_id)
    actor_id = actor.id
    args = dict(order_item_id=item.id, customer_id=item.order.customer_id,
        reviewed_hash=data["reviewed_hash"], expected_revision=data["rule_revision"])
    engine = db.get_bind()
    db.rollback()
    barrier = Barrier(2)
    def execute(index):
        with Session(engine) as session:
            user = session.get(User, actor_id)
            barrier.wait(timeout=10)
            try:
                result = execute_rule_cutover(session, **args, actor=user,
                    operation_key="concurrent-rule" if same_key else f"concurrent-rule-{index}")
                session.commit()
                return "ok", result
            except BomPlanError as error:
                session.rollback()
                return "conflict", str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, (1, 2)))
    if same_key:
        assert results[0] == results[1] and results[0][0] == "ok"
    else:
        assert sorted(row[0] for row in results) == ["conflict", "ok"]
    db.expire_all()
    assert db.scalar(select(text("count(*)")).select_from(OrderBomRuleRevision)) == 1
    assert db.scalar(text("SELECT count(*) FROM operation_logs WHERE action_code='switch_unstarted_graph_rule'")) == 1


def test_000148_real_carton_and_assembled_liner_keep_separate_stock_after_rule_switch(factory_http):
    from app.models.user import User
    from app.models.bom_subkit import ProductSubkit
    from app.services.bom_subkits import save_subkit
    from app.services.multilevel_bom_external_freeze import freeze_order_procurement
    from app.services.multilevel_bom_plan import plan_bom
    from tests.test_multilevel_bom_factory_compile import new_item
    client, db = factory_http
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    old = db.get(ProductSubkit, 3765)
    save_subkit(db, parent_product_id=3765, name=db.get(Product, 3822).product_name,
        kits_per_parent=1, members=[dict(product_id=3788, pieces_per_kit=2), dict(product_id=3789, pieces_per_kit=6)],
        expected_version=old.version, actor=actor, enabled=False)
    save(db, actor, 3822, "assembled", [(3788, 2, "assembly"), (3789, 6, "assembly")])
    save(db, actor, 3765, "manufactured", [(3822, 1, "accompany")])
    item = new_item(db, 3765, 2)
    original = freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    db.commit()
    carton = db.get(Product, 3765)
    replace_product_bom(db, parent_product_id=3765, expected_version=carton.version, user=actor,
        inventory_mode="manufactured", material_mode="expand_children", delivery_mode="parent",
        components=[dict(component_product_id=3822, quantity_per_set=1, inventory_relation="accompany")])
    db.commit()
    url, _, payload = preview(client, item, "real-carton-liner-rule")
    changed = client.post(url + "/execute", json=payload)
    assert changed.status_code == 200, changed.text
    db.expire_all()
    current = read_compiled_order_bom(db, item.id)
    assert dict(plan_bom(current.graph, 2).picking) == {3765: 2, 3822: 2}
    assert current.history_source_ids == {row.id for row in original.snapshots}
    paper_receipt_flow(client, db, current, item.id, 3765)
    from app.services.multilevel_bom_receipts import own_output_lots
    positions = {pid: {lot.warehouse_location_id for lot in own_output_lots(db, item.id)
        if lot.finished_detail.product_id == pid and lot.quantity_reserved > 0} for pid in (3765, 3822)}
    assert all(positions.values()) and positions[3765].isdisjoint(positions[3822])
    delivery = client.post("/api/deliveries", json={"customer_id": current.graph.customer_id,
        "delivery_date": "2026-09-10", "items": [{"order_item_id": item.id, "delivered_quantity": 1}]})
    assert delivery.status_code == 201, delivery.text
    did = delivery.json()["id"]
    detail = client.get(f"/api/deliveries/{did}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["items"][0]["composite_fulfillment_mode"] == "parent_delivery"
    pick = client.post(f"/api/deliveries/{did}/pick-task")
    assert pick.status_code == 201, pick.text
    lines = pick.json()["items"][0]["location_lines"]
    assert {line["location_id"] for line in lines} >= positions[3765] | positions[3822]
