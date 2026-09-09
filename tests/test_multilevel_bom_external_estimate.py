from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.models.order import OrderItem
from app.models.product import Product
from app.models.external_packaging_price import ExternalPackagingPriceVersion
from app.services.order_material_cost import estimate_order_item_material_cost, build_material_cost_estimate_context
from tests.test_multilevel_bom_external_receipts import prepare, purchase_app
from tests.test_p1_33c5_external_packaging_receiving import _confirm, _login


@pytest.mark.parametrize("direct,expected", [(True, "297.00"), (False, "594.00")])
def test_external_node_cost_uses_real_product_and_frozen_ratio(purchase_app, direct, expected):
    _, item_id, child_id = prepare(purchase_app, direct=direct)
    with purchase_app.state.session_factory() as db:
        item = db.get(OrderItem, item_id)
        result = estimate_order_item_material_cost(db, item)
        assert result["estimated_material_total_cost"] == expected
        row, = result["material_cost_components"]
        assert row["product_id"] == child_id
        assert row["source_type"] == "bom_graph_external"
        from app.services.composite_bom import get_order_item_bom_components_by_item_ids
        rows = get_order_item_bom_components_by_item_ids(db, [item.id])
        context = build_material_cost_estimate_context(db, [item], bom_components_by_item_id=rows)
        assert estimate_order_item_material_cost(db, item, bom_components=rows[item.id], context=context) == result
        child = db.get(Product, child_id)
        child.external_packaging_default_purchase_quantity_basis = 9
        child.version += 1
        db.commit()
        assert estimate_order_item_material_cost(db, item) == result


def test_fractional_ratio_does_not_round_legacy_multiplier_into_extra_purchase(purchase_app):
    _, item_id, _ = prepare(purchase_app, direct=True, stock_basis=3, purchase_basis=1, quantity=3)
    with purchase_app.state.session_factory() as db:
        result = estimate_order_item_material_cost(db, db.get(OrderItem, item_id))
        assert result["estimated_material_total_cost"] == "9.90"
        assert result["material_cost_components"][0]["purchase_quantity"] == "1"


@pytest.mark.parametrize("problem", ["expired", "foreign_currency", "moq"])
def test_unusable_quote_does_not_become_complete_cny_estimate(purchase_app, problem):
    from datetime import date
    _, item_id, _ = prepare(purchase_app, direct=True)
    with purchase_app.state.session_factory() as db:
        price = db.scalar(select(ExternalPackagingPriceVersion).where(
            ExternalPackagingPriceVersion.external_product_id == purchase_app.state.fixture["not_frozen_product_id"]))
        if problem == "expired":
            price.effective_to = date(2026, 1, 2)
        elif problem == "foreign_currency":
            price.currency = "USD"
        else:
            price.moq_quantity = Decimal("100")
        db.commit()
        result = estimate_order_item_material_cost(db, db.get(OrderItem, item_id))
        assert result["estimated_material_total_cost"] is None
        assert result["material_cost_missing_items"]


@pytest.mark.parametrize("corner_guard", [False, True])
def test_tier_and_tax_estimate_matches_actual_purchase_confirmation(purchase_app, corner_guard):
    from app.models.supplier import ExternalPackagingProduct
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
    # Reuse real supplier category and pricing contracts, not a pricing stub.
    if corner_guard:
        with purchase_app.state.session_factory() as db:
            product = db.scalar(select(ExternalPackagingProduct).where(ExternalPackagingProduct.supplier_product_code == "CORNER-A"))
            purchase_app.state.fixture["not_frozen_product_id"] = product.id
    order_id, item_id, _ = prepare(purchase_app, direct=True)
    with purchase_app.state.session_factory() as db:
        price = db.scalar(select(ExternalPackagingPriceVersion).where(
            ExternalPackagingPriceVersion.external_product_id == purchase_app.state.fixture["not_frozen_product_id"]))
        price.tax_mode = "tax_exclusive"
        price.tier_prices_json = json.dumps([{"min_quantity": "20", "unit_price": "1.00"}])
        db.commit()
        estimate = estimate_order_item_material_cost(db, db.get(OrderItem, item_id))
        row, = estimate["material_cost_components"]
        assert row["purchase_quantity"] == "30"
        assert Decimal(row["pricing_quantity"]) == (Decimal("26.1") if corner_guard else Decimal(30))
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)
    with purchase_app.state.session_factory() as db:
        purchase = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == item_id))
        assert Decimal(row["quoted_line_amount"]) == purchase.line_amount
        assert Decimal(row["quoted_tax_amount"]) == purchase.tax_amount
        assert Decimal(estimate["estimated_material_total_cost"]) == purchase.total_amount


@pytest.mark.parametrize("current_scope", [False, True])
def test_other_customer_candidate_is_rejected(purchase_app, current_scope):
    from app.models.customer import Customer
    from app.models.supplier import ExternalPackagingProduct
    from app.models.order_external_packaging import SalesOrderItemExternalComponentCandidate
    from app.services.multilevel_bom_plan import BomPlanError
    _, item_id, _ = prepare(purchase_app, direct=True)
    with purchase_app.state.session_factory() as db:
        customer = Customer(name="隔离的其他客户")
        db.add(customer)
        db.flush()
        if current_scope:
            product = db.get(ExternalPackagingProduct, purchase_app.state.fixture["not_frozen_product_id"])
            product.customer_scope_id = customer.id
        else:
            candidate = db.scalar(select(SalesOrderItemExternalComponentCandidate).where(
                SalesOrderItemExternalComponentCandidate.external_product_id_snapshot == purchase_app.state.fixture["not_frozen_product_id"]))
            candidate.customer_scope_id_snapshot = customer.id
        db.commit()
        with pytest.raises(BomPlanError, match="客户"):
            estimate_order_item_material_cost(db, db.get(OrderItem, item_id))


def test_quote_freeze_is_idempotent_and_keeps_original_version(purchase_app):
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
    from app.services.order_material_cost_snapshot import freeze_order_item_material_cost
    _, item_id, _ = prepare(purchase_app, direct=True)
    with purchase_app.state.session_factory() as db:
        item = db.get(OrderItem, item_id)
        before = db.scalar(select(func.count()).select_from(ExternalPackagingPurchaseItem))
        first, created = freeze_order_item_material_cost(db, item)
        db.commit()
        original = first.components_json
        same, repeated = freeze_order_item_material_cost(db, item)
        assert created and not repeated and first.id == same.id
        old = db.scalar(select(ExternalPackagingPriceVersion).where(
            ExternalPackagingPriceVersion.external_product_id == purchase_app.state.fixture["not_frozen_product_id"]))
        values = {column.name: getattr(old, column.name) for column in ExternalPackagingPriceVersion.__table__.columns
                  if column.name not in ("id", "created_at")}
        values.update(version_number=old.version_number + 1, unit_price=Decimal("8"), quote_fingerprint="new-quote-estimate-only")
        db.add(ExternalPackagingPriceVersion(**values))
        db.commit()
        second, created = freeze_order_item_material_cost(db, item)
        db.commit()
        assert created and second.snapshot_version == first.snapshot_version + 1
        assert first.components_json == original
        assert first.estimated_material_total_cost == Decimal("297")
        assert second.estimated_material_total_cost == Decimal("240")
        assert db.scalar(select(func.count()).select_from(ExternalPackagingPurchaseItem)) == before
