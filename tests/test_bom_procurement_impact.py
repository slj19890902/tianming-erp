"""Existing supplier obligations remain visible during rule review."""
from decimal import Decimal
import pytest
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


@pytest.mark.parametrize("purchase_basis", [1, 3])
def test_external_pending_and_partial_receipt_review_preserves_contract(factory_http, purchase_basis):
    client, db = factory_http
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    children = [(row["component_product_id"], int(row["quantity_per_set"]), "accompany")
                for row in get_product_bom(db, 3479)["components"]]
    assert children and all(db.get(Product, pid).supply_mode == "external_purchase" for pid, _, _ in children)
    for pid, _, _ in children:
        product = db.get(Product, pid)
        product.external_packaging_default_order_quantity_basis = Decimal(1)
        product.external_packaging_default_purchase_quantity_basis = Decimal(purchase_basis)
        product.version += 1
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
    assert line["mapping"]["current_source"]
    assert line["mapping"]["purchase_conversion_compatible"], before["quantity_impact"]
    assert line["mapping"]["received_stock_capacity"] == 0
    assert line["mapping"]["pending_stock_capacity"] == int(purchase.purchase_quantity) // purchase_basis
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
    mapping = changed["mapping"]
    assert mapping["received_stock_capacity"] == 1 // purchase_basis
    assert Decimal(mapping["received_purchase_remainder"]) == 1 % purchase_basis
    assert mapping["pending_stock_capacity"] + mapping["received_stock_capacity"] == line["mapping"]["pending_stock_capacity"]
    assert Decimal(mapping["final_purchase_remainder"]) == 0
    # A master conversion change cannot reinterpret the old supplier contract.
    product = db.get(Product, changed["product_id"])
    product.external_packaging_default_purchase_quantity_basis = Decimal(purchase_basis + 1)
    product.version += 1
    db.commit()
    response = client.get(url)
    assert response.status_code == 200, response.text
    revised = next(row for row in response.json()["external"] if row["line"]["id"] == purchase.id)
    assert revised["line"] == line["line"] and revised["header"] == line["header"]
    assert not revised["mapping"]["purchase_conversion_compatible"]
    for field in ("received_stock_capacity", "pending_stock_capacity", "received_purchase_remainder", "final_purchase_remainder"):
        assert revised["mapping"][field] == mapping[field]
    # Equivalent ratios and a renamed product keep the same physical identity;
    # compare exact fractions, not serialized decimal strings or names.
    product.external_packaging_default_order_quantity_basis = Decimal(2)
    product.external_packaging_default_purchase_quantity_basis = Decimal(2 * purchase_basis)
    product.product_name = "ISOLATED renamed external component"
    product.version += 1
    db.commit()
    response = client.get(url)
    assert response.status_code == 200, response.text
    equivalent = next(row for row in response.json()["external"] if row["line"]["id"] == purchase.id)
    assert equivalent["mapping"]["purchase_conversion_compatible"]
    assert equivalent["line"] == line["line"] and equivalent["header"] == line["header"]
    db.refresh(actor)
    actor.role = "sales"
    db.commit()
    assert client.get(url).status_code == 403
