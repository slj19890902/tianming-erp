"""Existing supplier obligations remain visible during rule review."""
from sqlalchemy import select

from app.models.product import Product
from app.models.user import User
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
from app.services.composite_bom import get_product_bom
from app.services.multilevel_bom_external_freeze import freeze_order_procurement
from tests.test_bom_other_products_acceptance import factory_http
from tests.test_multilevel_bom_factory_compile import factory_copy, new_item
from tests.test_multilevel_bom_master import save
from tests.test_p1_33c5_external_packaging_receiving import _confirm
from tests.test_multilevel_bom_external_receipts import receive


def test_external_pending_and_partial_receipt_review_preserves_contract(factory_http):
    client, db = factory_http
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    children = [(row["component_product_id"], int(row["quantity_per_set"]), "accompany")
                for row in get_product_bom(db, 3479)["components"]]
    assert children and all(db.get(Product, pid).supply_mode == "external_purchase" for pid, _, _ in children)
    save(db, actor, 3479, "manufactured", children)
    item = new_item(db, 3479, 2)
    freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    db.commit()
    _confirm(client, item.order_id)
    db.expire_all()
    purchase = db.scalar(select(ExternalPackagingPurchaseItem).where(
        ExternalPackagingPurchaseItem.sales_order_item_id == item.id))
    url = f"/api/orders/items/{item.id}/bom-procurement-impact"
    response = client.get(url)
    assert response.status_code == 200, response.text
    before = response.json()
    line = next(row for row in before["external"] if row["line"]["id"] == purchase.id)
    assert line["received_quantity"] == 0 and line["remaining_quantity"] == purchase.purchase_quantity
    assert before["executable"] is False
    received = receive(client, purchase.purchase_order_id, purchase.id, "impact-partial", 1)
    assert received.status_code == 200, received.text
    response = client.get(url)
    assert response.status_code == 200, response.text
    after = response.json()
    changed = next(row for row in after["external"] if row["line"]["id"] == purchase.id)
    assert changed["line"] == line["line"] and changed["header"] == line["header"]
    assert changed["received_quantity"] == 1
    assert changed["remaining_quantity"] == purchase.purchase_quantity - 1
    assert after["evidence_hash"] != before["evidence_hash"]
    db.refresh(actor)
    actor.role = "sales"
    db.commit()
    assert client.get(url).status_code == 403
