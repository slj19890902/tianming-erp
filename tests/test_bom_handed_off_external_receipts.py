from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3
import pytest
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.product import Product
from app.models.user import User
from app.models.multilevel_bom import OrderBomSourceHandoff, OrderBomExternalReceiptExecution
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingReceiptItem
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.composite_bom import get_product_bom
from app.services.multilevel_bom_external_freeze import freeze_order_procurement
from app.services.multilevel_bom_execution_boundary import _source_identity
from app.services.multilevel_bom_rule_impact import review_current_rule_requirements
from app.services.multilevel_bom_rule_cutover import persist_reviewed_rule
from app.services.multilevel_bom_receipts import own_output_lots
from tests.test_bom_other_products_acceptance import factory_http
from tests.test_multilevel_bom_factory_compile import factory_copy, new_item
from tests.test_multilevel_bom_master import save
from tests.test_p1_33c5_external_packaging_receiving import _confirm
from tests.test_multilevel_bom_external_receipts import receive
from tests.test_multilevel_bom_external_reversal import reverse
from tests.test_p1_33c3_external_packaging_purchase_confirmation import purchase_app


@pytest.mark.parametrize("received_before", [2, 4])
def test_old_purchase_new_receipt_keeps_cost_and_execution_separate(factory_http, monkeypatch, received_before):
    client, db = factory_http
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    children = [(row["component_product_id"], int(row["quantity_per_set"]), "accompany")
        for row in get_product_bom(db, 3479)["components"]]
    for pid, _, _ in children:
        product = db.get(Product, pid)
        product.external_packaging_default_order_quantity_basis = Decimal(1)
        product.external_packaging_default_purchase_quantity_basis = Decimal(3)
        product.version += 1
    save(db, actor, 3479, "manufactured", children)
    item = new_item(db, 3479, 2)
    frozen = freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    db.commit()
    _confirm(client, item.order_id)
    purchase = db.scalar(select(ExternalPackagingPurchaseItem).where(
        ExternalPackagingPurchaseItem.sales_order_item_id == item.id))
    first = receive(client, purchase.purchase_order_id, purchase.id, "before-switch", received_before)
    assert first.status_code == 200, first.text
    first_id = first.json()["receipt"]["id"]
    old = {row.id: row for row in frozen.snapshots}
    review = review_current_rule_requirements(db, order_item_id=item.id, customer_id=frozen.graph.customer_id)
    # Source writer fixture only; administrator pending-procurement entry is
    # still separate work. All subsequent receipt/rollback calls use real HTTP.
    revision = persist_reviewed_rule(db, review=review, item=item, previous=None,
        expected_revision=0, reviewed_hash=review.checksum, request_hash="c" * 64,
        operation_key="isolated-receipt-handoff", actor=actor)
    db.commit()
    quantity = purchase.purchase_quantity - received_before
    blocked = receive(client, purchase.purchase_order_id, purchase.id, "after-switch", quantity)
    assert blocked.status_code == 409, blocked.text
    for node in frozen.graph.nodes:
        if node.source != "purchased":
            continue
        source = next(row for row in old.values() if row.component_product_id == node.product_id)
        target = next(row for row in review.proposed.snapshots if row.component_product_id == node.product_id)
        db.add(OrderBomSourceHandoff(revision_id=revision.id, source_snapshot_id=source.id,
            target_snapshot_id=target.id, order_item_id=item.id, product_id=node.product_id,
            source_kind="purchased", source_basis_hash=_source_identity(source)["hash"],
            target_basis_hash=_source_identity(target)["hash"]))
    db.commit()
    from app.services import multilevel_bom_external_costs
    from app.services.multilevel_bom_external_identity import current_external_links, frozen_purchase_quantities
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.models.order_external_packaging import SalesOrderItemExternalComponent
    from app.models.multilevel_bom import OrderBomExternalComponent
    purchased_product = db.get(OrderBomExternalComponent, purchase.order_component_id).product_id
    def new_purchase_quantity():
        db.expire_all()
        compiled = read_compiled_order_bom(db, item.id)
        links = current_external_links(db, compiled)
        components = [db.get(SalesOrderItemExternalComponent, link.external_component_id) for link in links]
        quantities = frozen_purchase_quantities(db, components, {item.id: item})
        component_id = next(link.external_component_id for link in links if link.product_id == purchased_product)
        return quantities[component_id]
    # Old whole stock has not been explicitly transferred in this fixture;
    # pending purchasing covers the rest without counting that stock again.
    expected_new_purchase = Decimal((received_before // 3) * 3)
    assert new_purchase_quantity() == expected_new_purchase
    from app.services.multilevel_bom_plan import BomPlanError
    def fail(*args, **kwargs):
        raise BomPlanError("isolated post-stock cost failure")
    from tests.test_multilevel_bom_modes_migration import original_facts
    path = Path(db.get_bind().url.database)
    def facts():
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as check:
            columns = {table: [row[1] for row in check.execute(f'PRAGMA table_info("{table}")')]
                for table, in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return original_facts(check, columns)
    before_failure = facts()
    with monkeypatch.context() as patch:
        patch.setattr(multilevel_bom_external_costs, "receipt_output_cost", fail)
        failed = receive(client, purchase.purchase_order_id, purchase.id, "after-switch", quantity)
        assert failed.status_code == 409, failed.text
    assert facts() == before_failure
    db.expire_all()
    assert list(db.scalars(select(OrderBomExternalReceiptExecution))) == []
    assert len(list(db.scalars(select(ExternalPackagingReceiptItem).where(
        ExternalPackagingReceiptItem.purchase_item_id == purchase.id)))) == 1
    received = receive(client, purchase.purchase_order_id, purchase.id, "after-switch", quantity)
    assert received.status_code == 200, received.text
    receipt_id = received.json()["receipt"]["id"]
    db.expire_all()
    line = db.scalar(select(ExternalPackagingReceiptItem).where(ExternalPackagingReceiptItem.receipt_id == receipt_id))
    ownership = db.get(OrderBomExternalReceiptExecution, line.id)
    assert ownership is not None and ownership.source_snapshot_id in old
    lot = db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type == "bom_external_receipt", InventoryLot.source_ref_id == line.id))
    assert lot is not None and lot.quantity_reserved == line.converted_finished_quantity
    reservation = db.scalar(select(InventoryReservation).where(InventoryReservation.inventory_lot_id == lot.id))
    assert reservation.sales_order_item_bom_component_id not in old
    assert lot.id in {row.id for row in own_output_lots(db, item.id)}
    first_line = db.scalar(select(ExternalPackagingReceiptItem).where(ExternalPackagingReceiptItem.receipt_id == first_id))
    assert db.get(OrderBomExternalReceiptExecution, first_line.id) is None
    assert all(row.source_ref_id != first_line.id for row in own_output_lots(db, item.id) if row.source_ref_type == "bom_external_receipt")
    cost = multilevel_bom_external_costs.receipt_output_cost(db, line.id)
    assert cost["bom_snapshot_id"] in old
    expected_cost = purchase.line_amount * Decimal(line.converted_finished_quantity) / (purchase.purchase_quantity / 3)
    assert Decimal(cost["capitalized_material_cost"]) == expected_cost.quantize(Decimal("0.0001"))
    assert len(cost["sources"]) == 2  # includes the two loose units received before switching
    assert new_purchase_quantity() == expected_new_purchase
    assert receive(client, purchase.purchase_order_id, purchase.id, "after-switch", quantity).status_code == 200
    immutable_before = facts()
    for statement in ("UPDATE order_bom_external_receipt_executions SET source_snapshot_id=0",
                      "DELETE FROM order_bom_external_receipt_executions"):
        with pytest.raises(IntegrityError, match="immutable"):
            with db.begin_nested():
                db.execute(text(statement))
    assert facts() == immutable_before
    from alembic import command
    from tests.test_p1_131_material_cost_lineage_migration import _config
    db.rollback()
    before_downgrade = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="已有BOM实收执行来源"):
        command.downgrade(_config(monkeypatch, path), "sm25v8x9z87")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_downgrade
    undone = reverse(client, receipt_id, "reverse-handed-off")
    assert undone.status_code == 200, undone.text
    assert reverse(client, receipt_id, "reverse-handed-off").status_code == 200
    db.expire_all()
    assert db.get(InventoryLot, lot.id).quantity_available == 0
    assert db.get(InventoryLot, lot.id).quantity_reserved == 0
    assert db.get(OrderBomExternalReceiptExecution, line.id) is not None
    assert db.scalar(select(ExternalPackagingReceiptItem).where(ExternalPackagingReceiptItem.receipt_id == first_id)) is not None
    assert new_purchase_quantity() == expected_new_purchase


def test_handed_off_receipt_assembles_and_reverses_under_execution_version(purchase_app, monkeypatch):
    from fastapi.testclient import TestClient
    from app.models.order import OrderItem
    from app.models.multilevel_bom import BomAssembly
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from tests.test_multilevel_bom_external_receipts import prepare, _login
    from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _use_p181_published_map_identity
    _use_p181_published_map_identity(monkeypatch)
    factory = purchase_app.state.session_factory
    _seed_material_and_staging(factory)
    oid, iid, child_id = prepare(purchase_app, quantity=2)
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, oid)
        with factory() as db:
            purchase = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == iid))
            pid, lid, remaining = purchase.purchase_order_id, purchase.id, purchase.purchase_quantity - 2
        assert receive(client, pid, lid, "assembly-before", 2).status_code == 200
        with factory() as db:
            item = db.get(OrderItem, iid)
            actor = db.scalar(select(User).where(User.username == "purchase-admin"))
            frozen = read_compiled_order_bom(db, iid)
            review = review_current_rule_requirements(db, order_item_id=iid, customer_id=frozen.graph.customer_id)
            revision = persist_reviewed_rule(db, review=review, item=item, previous=None,
                expected_revision=0, reviewed_hash=review.checksum, request_hash="e" * 64,
                operation_key="isolated-assembly-handoff", actor=actor)
            source = next(row for row in frozen.snapshots if row.component_product_id == child_id)
            target = next(row for row in review.proposed.snapshots if row.component_product_id == child_id)
            db.add(OrderBomSourceHandoff(revision_id=revision.id, source_snapshot_id=source.id,
                target_snapshot_id=target.id, order_item_id=iid, product_id=child_id, source_kind="purchased",
                source_basis_hash=_source_identity(source)["hash"], target_basis_hash=_source_identity(target)["hash"]))
            db.commit()
        response = receive(client, pid, lid, "assembly-after", remaining)
        assert response.status_code == 200, response.text
        rid = response.json()["receipt"]["id"]
        with factory() as db:
            assembly = db.scalar(select(BomAssembly).where(BomAssembly.order_item_id == iid))
            assert assembly is not None and assembly.status == "posted"
            assert db.get(InventoryLot, assembly.output_lot_id).quantity_reserved == 2
        result = reverse(client, rid, "reverse-assembly-handoff")
        assert result.status_code == 200, result.text
        with factory() as db:
            assembly = db.scalar(select(BomAssembly).where(BomAssembly.order_item_id == iid))
            assert assembly.status == "reversed"
            lot = db.get(InventoryLot, assembly.output_lot_id)
            assert lot.quantity_available == lot.quantity_reserved == 0


@pytest.mark.parametrize("new_ratio,expected_purchase", [(2, 0), (3, 1)])
def test_carried_pack_capacity_is_subtracted_before_new_purchase_rounding(purchase_app, new_ratio, expected_purchase, monkeypatch):
    from fastapi.testclient import TestClient
    from app.models.order import OrderItem
    from app.models.order_external_packaging import SalesOrderItemExternalComponent
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.multilevel_bom_external_identity import current_external_links, frozen_purchase_quantities
    from tests.test_multilevel_bom_external_receipts import prepare, _login
    oid, iid, child_id = prepare(purchase_app, quantity=2, stock_basis=5, purchase_basis=1)
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, oid)
        with purchase_app.state.session_factory() as db:
            item = db.get(OrderItem, iid)
            actor = db.scalar(select(User).where(User.username == "purchase-admin"))
            frozen = read_compiled_order_bom(db, iid)
            purchase = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == iid))
            assert purchase.purchase_quantity == 1  # one purchased pack contains five stock units
            original_purchase_id = purchase.id
            original_contract = {column.key: getattr(purchase, column.key) for column in purchase.__table__.columns}
            save(db, actor, item.product_id, "assembled", [(child_id, new_ratio, "assembly")])
            db.commit()
            review = review_current_rule_requirements(db, order_item_id=iid, customer_id=frozen.graph.customer_id)
            revision = persist_reviewed_rule(db, review=review, item=item, previous=None,
                expected_revision=0, reviewed_hash=review.checksum, request_hash="f" * 64,
                operation_key="isolated-pack-handoff", actor=actor)
            source = next(row for row in frozen.snapshots if row.component_product_id == child_id)
            target = next(row for row in review.proposed.snapshots if row.component_product_id == child_id)
            db.add(OrderBomSourceHandoff(revision_id=revision.id, source_snapshot_id=source.id,
                target_snapshot_id=target.id, order_item_id=iid, product_id=child_id, source_kind="purchased",
                source_basis_hash=_source_identity(source)["hash"], target_basis_hash=_source_identity(target)["hash"]))
            db.commit()
            current = read_compiled_order_bom(db, iid)
            link = current_external_links(db, current)[0]
            component = db.get(SalesOrderItemExternalComponent, link.external_component_id)
            assert frozen_purchase_quantities(db, [component], {iid: item}) == {component.id: Decimal(expected_purchase)}
            assert purchase.purchase_quantity == 1
            component_id = component.id
            candidate_id = next(row.id for row in component.candidates if row.is_default)
        preview = client.get(f"/api/orders/{oid}/external-packaging-purchase")
        assert preview.status_code == 200, preview.text
        assert preview.json()["additional_purchase"] is True
        assert len(preview.json()["history"]) == 1
        payload = {"idempotency_key": "additional-pack-purchase", "lines": [{
            "order_component_id": component_id, "candidate_id": candidate_id, "purchase_quantity": "1"}]}
        if expected_purchase == 0:
            assert preview.json()["status"] == "stock_covered" and preview.json()["items"] == []
            assert client.post(f"/api/orders/{oid}/external-packaging-purchase/confirm", json=payload).status_code == 409
        else:
            assert preview.json()["status"] == "pending"
            assert Decimal(preview.json()["items"][0]["suggested_purchase_quantity"]) == 1
            from app.api import external_packaging_purchases as api
            from sqlalchemy.exc import OperationalError
            from tests.test_multilevel_bom_modes_migration import original_facts
            with purchase_app.state.session_factory() as db:
                database_path = Path(db.get_bind().url.database)
            def facts():
                with sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True) as check:
                    columns = {table: [row[1] for row in check.execute(f'PRAGMA table_info("{table}")')]
                        for table, in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    return original_facts(check, columns)
            before_failure = facts()
            def fail_audit(*args, **kwargs):
                raise OperationalError("isolated audit failure", {}, Exception("rollback proof"))
            with monkeypatch.context() as patch:
                patch.setattr(api, "append_audit_event", fail_audit)
                failed = client.post(f"/api/orders/{oid}/external-packaging-purchase/confirm", json=payload)
                assert failed.status_code == 409, failed.text
            assert facts() == before_failure
            from concurrent.futures import ThreadPoolExecutor
            requests = [payload, {**payload, "idempotency_key": "racing-additional-pack"}]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(client.post, f"/api/orders/{oid}/external-packaging-purchase/confirm", json=request)
                    for request in requests]
                responses = [future.result() for future in futures]
            assert sorted(response.status_code for response in responses) == [200, 409], [response.text for response in responses]
            winner = next(i for i, response in enumerate(responses) if response.status_code == 200)
            payload, confirmed = requests[winner], responses[winner]
            replay = client.post(f"/api/orders/{oid}/external-packaging-purchase/confirm", json=payload)
            assert replay.status_code == 200, replay.text
            assert replay.json()["confirmation"]["batch_id"] == confirmed.json()["confirmation"]["batch_id"]
            duplicate = client.post(f"/api/orders/{oid}/external-packaging-purchase/confirm",
                json={**payload, "idempotency_key": "different-additional-key"})
            assert duplicate.status_code == 409, duplicate.text
            after = client.get(f"/api/orders/{oid}/external-packaging-purchase")
            assert after.status_code == 200 and after.json()["status"] == "confirmed"
            assert len(after.json()["history"]) == 2
            cancelled = client.post(f"/api/orders/{oid}/external-packaging-purchase/cancel", json={
                "expected_batch_id": confirmed.json()["confirmation"]["batch_id"], "confirmed": True,
                "reason": "隔离验证撤销追加批次，保留原合同"})
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["preview"]["additional_purchase"] is True
            assert Decimal(cancelled.json()["preview"]["items"][0]["suggested_purchase_quantity"]) == 1
            recreated = client.post(f"/api/orders/{oid}/external-packaging-purchase/confirm",
                json={**payload, "idempotency_key": "recreate-additional-pack"})
            assert recreated.status_code == 200, recreated.text
        with purchase_app.state.session_factory() as db:
            purchases = list(db.scalars(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == iid)))
            assert len(purchases) == 1 + 2 * int(expected_purchase > 0)
            assert all(row.purchase_quantity == 1 for row in purchases)
            original = db.get(ExternalPackagingPurchaseItem, original_purchase_id)
            assert {column.key: getattr(original, column.key) for column in original.__table__.columns} == original_contract
            from app.models.external_packaging_purchase import ExternalPackagingPurchaseCancellation
            assert db.scalar(select(ExternalPackagingPurchaseCancellation).where(
                ExternalPackagingPurchaseCancellation.purchase_order_id == original.purchase_order_id)) is None
