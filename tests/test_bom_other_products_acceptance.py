"""Each confirmed accompanying product, using the export's actual supply modes."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
import pytest

from app.models.product import Product
from app.models.user import User
from app.services.multilevel_bom_orders import read_compiled_order_bom
from tests.test_multilevel_bom_factory_compile import factory_copy

PRODUCTS = [2817, 3113, 3156, 3479, 3484, 3485, 3489, 3496,
            3585, 3586, 3751, 3757, 3758, 3774, 3793, 3797]


@pytest.fixture
def factory_http(factory_copy):
    from app.api import auth, products, orders, requisition, incoming, production, warehouse, deliveries
    from app.api.deps import get_db
    from app.core.security import hash_password
    db = factory_copy
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    actor.password_hash = hash_password("isolated-bom-acceptance")
    actor.must_change_password = False
    username = actor.username
    db.commit()
    app = FastAPI()
    for name, module in (("auth", auth), ("products", products), ("orders", orders),
            ("requisition", requisition), ("incoming", incoming), ("production", production),
            ("warehouse", warehouse), ("deliveries", deliveries)):
        app.include_router(module.router, prefix="/api/" + name)
    from app.api.external_packaging_purchases import router as external_router
    app.include_router(external_router, prefix="/api")
    def sessions():
        with Session(db.get_bind()) as request_db:
            yield request_db
    app.dependency_overrides[get_db] = sessions
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": username, "password": "isolated-bom-acceptance"})
        assert login.status_code == 200, login.text
        yield client, db


@pytest.mark.parametrize("pid", PRODUCTS)
def test_actual_supply_preserved_through_admin_save_reopen_and_order(factory_http, pid, occupy_released=False):
    from app.api.products import ProductBOMComponentPayload
    client, db = factory_http
    original = client.get(f"/api/products/{pid}/bom")
    assert original.status_code == 200, original.text
    root = db.get(Product, pid)
    assert root.is_active and root.supply_mode == "corrugated_production"
    identities = {pid, *(row["component_product_id"] for row in original.json()["components"])}
    before = {p: db.get(Product, p).supply_mode for p in identities}
    rows = [{**{key: value for key, value in row.items() if key in ProductBOMComponentPayload.model_fields},
             "inventory_relation": "accompany"} for row in original.json()["components"]]
    response = client.put(f"/api/products/{pid}/bom", json={"expected_version": root.version,
        "inventory_mode": "manufactured", "material_mode": "expand_children",
        "delivery_mode": "parent" if root.composite_fulfillment_mode == "parent_delivery" else "components",
        "components": rows})
    assert response.status_code == 200, response.text
    reopened = client.get(f"/api/products/{pid}/bom")
    assert reopened.status_code == 200, reopened.text
    assert [(r["component_product_id"], r["quantity_per_set"]) for r in reopened.json()["components"]] == [
        (r["component_product_id"], r["quantity_per_set"]) for r in original.json()["components"]]
    created = client.post("/api/orders", json={"customer_id": root.customer_id,
        "customer_po": f"ISOLATED-16-{pid}", "order_date": "2026-09-10", "delivery_date": "2026-09-20",
        "items": [{"client_line_id": f"ISOLATED-16-{pid}", "product_id": pid,
        "product_code": root.product_code, "product_name": root.product_name,
        "quantity": 2, "unit_price": "100"}]})
    if (pid in (3585, 3586) and root.mold_tool_id is None and created.status_code == 400
            and created.json().get("detail") == "第1个模切组件必须选择启用中的模具"):
        pytest.xfail("只读导出缺少本体模具身份，已请求工厂资料；保存通过但新下单及全链未通过")
    assert created.status_code == 201, created.text
    iid = created.json()["items"][0]["id"]
    db.expire_all()
    compiled = read_compiled_order_bom(db, iid)
    assert all(edge.relation == "accompany" for edge in compiled.graph.edges)
    assert {n.product_id: n.source for n in compiled.graph.nodes} == {
        p: "purchased" if source == "external_purchase" else "manufactured" for p, source in before.items()}
    assert {p: db.get(Product, p).supply_mode for p in identities} == before
    assert db.get(Product, 3494).is_active is False
    if any(source == "external_purchase" for source in before.values()):
        from app.models.order import OrderItem
        from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
        from tests.test_p1_33c5_external_packaging_receiving import _confirm
        from tests.test_multilevel_bom_external_receipts import receive
        _confirm(client, db.get(OrderItem, iid).order_id)
        db.expire_all()
        lines = list(db.scalars(select(ExternalPackagingPurchaseItem).where(
            ExternalPackagingPurchaseItem.sales_order_item_id == iid)))
        assert lines
        for line in lines:
            received = receive(client, line.purchase_order_id, line.id,
                f"other16-external-{pid}-{line.id}", line.purchase_quantity)
            assert received.status_code == 200, received.text
    paper_receipt_flow(client, db, compiled, iid, pid, occupy_released=occupy_released)


def paper_receipt_flow(client, db, compiled, iid, pid, *, occupy_released=False,
                       stop_after_first_delivery=False, stop_after_receipts=False, key_suffix=""):
    from collections import defaultdict
    from app.models.order import OrderItem
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.models.requisition import RequisitionItem
    from app.api.requisition import _bom_pending_component_requirements
    from tests.test_p1_81_receipt_purpose_flow import FrozenSource, _freeze_receipt_fact, _receive
    requirements = _bom_pending_component_requirements(db, db.get(OrderItem, iid))
    grouped = defaultdict(list)
    by_snapshot = {}
    for row in requirements:
        if pid == 3793 and row["product_id"] == 3793 and not row["material_id"] and not row["report_length_mm"]:
            pytest.xfail("父件3793缺少自身材质/供应商/开料尺寸，已请求真实资料；新单通过但全链未通过")
        assert row["supplier_name"] and row["material_id"], row
        assert row["report_length_mm"] and row["report_width_mm"], row
        by_snapshot[row["snapshot_id"]] = row
        grouped[row["supplier_name"]].append({"order_item_id": iid, "bom_snapshot_id": row["snapshot_id"],
            "component_type": row["component_type"], "cardboard_len": row["report_length_mm"],
            "cardboard_width": row["report_width_mm"], "special_process": row["cutting_mode"],
            **({"actual_yield_per_sheet": row["actual_yield_per_sheet"]} if row["is_die_cut"] else {})})
    for index, (supplier, items) in enumerate(grouped.items()):
        saved = client.post("/api/requisition/batches", json={"request_key": f"isolated-other16-{pid}-{index}{key_suffix}",
            "supplier_name": supplier, "items": items})
        assert saved.status_code == 201, saved.text
    db.expire_all()
    rows = list(db.scalars(select(PurchasePurposeSourceSnapshot).where(
        PurchasePurposeSourceSnapshot.source_bom_requisition_source_id.in_(select(RequisitionItemBomSource.id).where(
            RequisitionItemBomSource.sales_order_item_bom_component_id.in_(by_snapshot))))))
    assert len(rows) == len(requirements)
    for index, purpose in enumerate(rows):
        bom = db.get(RequisitionItemBomSource, purpose.source_bom_requisition_source_id)
        req = db.get(RequisitionItem, bom.requisition_item_id)
        source = FrozenSource(source_key=purpose.source_key, route_key=f"r{req.id}",
            supplier_item_id=req.id, source_version=req.version, purpose_snapshot_id=purpose.id,
            purpose_snapshot_version=purpose.snapshot_version, receipt_plan_fingerprint=purpose.preview_fingerprint,
            component_type=purpose.component_type, material_id=by_snapshot[bom.sales_order_item_bom_component_id]["material_id"],
            order_purpose_sheet_qty=purpose.order_purpose_sheet_qty, reserve_purpose_sheet_qty=purpose.reserve_purpose_sheet_qty)
        fact = _freeze_receipt_fact(client, source, idempotency_key=f"other16-price-{pid}-{index}{key_suffix}", unit_price="0.1234")
        assert fact.status_code == 200, fact.text
        received = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
            idempotency_key=f"other16-receive-{pid}-{index}{key_suffix}")
        assert received.status_code == 200, received.text
    if stop_after_receipts:
        db.expire_all()
        return
    from app.services.multilevel_bom_plan import plan_bom
    from app.services.multilevel_bom_receipts import own_output_lots
    expected = dict(plan_bom(compiled.graph, 2).picking)
    def balances(delivered):
        db.expire_all()
        lots = own_output_lots(db, iid)
        for product_id, required in expected.items():
            selected = [lot for lot in lots if lot.finished_detail.product_id == product_id]
            assert sum(lot.quantity_consumed for lot in selected) == required * delivered // 2
            assert sum(lot.quantity_reserved for lot in selected) == required * (2-delivered) // 2
            assert all(lot.warehouse_location_id for lot in selected)
        assert db.get(OrderItem, iid).delivered_quantity == delivered
    balances(0)
    delivery = client.post("/api/deliveries", json={"customer_id": compiled.graph.customer_id,
        "delivery_date": "2026-09-10", "items": [{"order_item_id": iid, "delivered_quantity": 1}]})
    assert delivery.status_code == 201, delivery.text
    did = delivery.json()["id"]
    dispatch = client.put(f"/api/deliveries/{did}/dispatch")
    assert dispatch.status_code == 200, dispatch.text
    balances(1)
    from app.models.delivery import DeliveryItem
    from app.models.graph_material_cost import FinanceDeliveryGraphCostFact, FinanceDeliveryGraphCostPortion
    facts = list(db.scalars(select(FinanceDeliveryGraphCostFact).where(FinanceDeliveryGraphCostFact.delivery_item_id.in_(
        select(DeliveryItem.id).where(DeliveryItem.delivery_id == did)))))
    assert len(facts) >= len(expected)
    portions = list(db.scalars(select(FinanceDeliveryGraphCostPortion).where(
        FinanceDeliveryGraphCostPortion.fact_id.in_([f.id for f in facts]))))
    assert portions and all((p.purchase_receipt_fact_id and p.purpose_allocation_id)
                            or p.external_receipt_item_id for p in portions)
    if stop_after_first_delivery:
        return did
    second = client.post("/api/deliveries", json={"customer_id": compiled.graph.customer_id,
        "delivery_date": "2026-09-10", "items": [{"order_item_id": iid, "delivered_quantity": 1}]})
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    dispatch = client.put(f"/api/deliveries/{second_id}/dispatch")
    assert dispatch.status_code == 200, dispatch.text
    balances(2)
    from app.models.order import Order
    assert db.get(Order, db.get(OrderItem, iid).order_id).status == "delivered"
    if occupy_released:
        from datetime import date
        from app.models.user import User
        from app.models.warehouse_inventory import Floor3LocationLayout
        from app.services.warehouse_inventory import manual_finished_in
        from tests.test_bom_cutover_writer import facts
        original_lot = own_output_lots(db, iid)[0]
        location_id = original_lot.warehouse_location_id
        layout = db.scalar(select(Floor3LocationLayout).where(Floor3LocationLayout.location_id == location_id))
        actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
        blocker = manual_finished_in(db, customer_id=compiled.graph.customer_id,
            product_id=original_lot.finished_detail.product_id, location_id=location_id,
            quantity=1, stock_date=date(2026, 9, 10), source_type="manual",
            remarks="隔离验收：发完后另一批占位", operator_id=actor.id,
            idempotency_key="isolated-other16-slot-blocker", expected_layout_version=layout.version)
        db.commit()
        before = facts(db)
        cancelled = client.put(f"/api/deliveries/{second_id}/cancel")
        assert cancelled.status_code == 409, cancelled.text
        db.expire_all()
        assert facts(db) == before
        assert blocker.quantity_available == 1
        balances(2)
        return
    cancelled = client.put(f"/api/deliveries/{second_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    balances(1)
    cancelled = client.put(f"/api/deliveries/{did}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    balances(0)


def test_completed_bom_delivery_cannot_restore_into_another_pallet(factory_http):
    test_actual_supply_preserved_through_admin_save_reopen_and_order(factory_http, 2817, occupy_released=True)
