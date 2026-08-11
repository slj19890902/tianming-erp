from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_p1_33c3_external_packaging_purchase_confirmation import (
    _confirmation_payload,
    purchase_app,
)
from test_p1_40a_packaging_masterdata import p1_40a_app


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200, response.text


def _price_row(product, *, version: int, value: str, created_by: int):
    from app.models.external_packaging_price import ExternalPackagingPriceVersion

    return ExternalPackagingPriceVersion(
        external_product_id=product.id,
        version_number=version,
        product_version=product.version,
        specification_snapshot_json=product.specification_json,
        quote_unit=product.purchase_unit,
        unit_conversion_basis=None,
        currency="CNY",
        tax_mode="tax_inclusive",
        tax_rate=Decimal("0.13"),
        tax_amount_per_unit=None,
        unit_price=Decimal(value),
        effective_from=date(2026, 1, 1) if version == 1 else date(2026, 8, 11),
        effective_to=None,
        moq_quantity=Decimal("1"),
        moq_unit=product.purchase_unit,
        packaging_multiple=Decimal("1"),
        tier_prices_json="[]",
        shipping_fee_mode="not_provided",
        shipping_fee=None,
        sample_fee=None,
        plate_fee=None,
        die_fee=None,
        evidence_reference=f"P1-40C-v{version}",
        quote_fingerprint=f"p1-40c-{product.id}-{version}",
        created_by=created_by,
    )


def test_product_external_candidates_show_formal_price_only_with_cost_permission(
    p1_40a_app: FastAPI,
) -> None:
    from app.core.security import hash_password
    from app.models.external_packaging_price import ExternalPackagingPriceVersion
    from app.models.supplier import ExternalPackagingProduct
    from app.models.user import User

    ids = p1_40a_app.state.fixture
    with p1_40a_app.state.factory() as db:
        admin = db.scalar(select(User).where(User.username == "p1-40a-admin"))
        product = db.get(ExternalPackagingProduct, ids["CG-870-A"])
        db.add(_price_row(product, version=1, value="1.10", created_by=admin.id))
        db.add(
            User(
                username="p1-40c-sales",
                password_hash=hash_password("123456"),
                role="sales",
                real_name="无成本权限销售",
                must_change_password=False,
            )
        )
        db.commit()
        price_count = db.scalar(
            select(func.count()).select_from(ExternalPackagingPriceVersion)
        )

    params = {
        "customer_id": ids["customer_a"],
        "category_code": "paper_corner_guard",
    }
    with TestClient(p1_40a_app) as client:
        _login(client, "p1-40a-admin")
        admin_response = client.get(
            "/api/master/products/external-supply-candidates", params=params
        )
        assert admin_response.status_code == 200, admin_response.text
        priced = next(
            row
            for row in admin_response.json()["items"]
            if row["supplier_product_code"] == "CG-870-A"
        )
        assert priced["current_purchase_price"] == {
            "id": priced["current_purchase_price"]["id"],
            "version_number": 1,
            "product_version": 1,
            "quote_unit": "根",
            "unit_price": "1.1",
            "currency": "CNY",
            "tax_mode": "tax_inclusive",
            "tax_rate": "0.13",
            "effective_from": "2026-01-01",
            "effective_to": None,
            "evidence_reference": "P1-40C-v1",
        }

        _login(client, "p1-40c-sales")
        sales_response = client.get(
            "/api/master/products/external-supply-candidates", params=params
        )
        assert sales_response.status_code == 200, sales_response.text
        assert sales_response.json()["items"]
        assert all(
            "current_purchase_price" not in row
            for row in sales_response.json()["items"]
        )

    with p1_40a_app.state.factory() as db:
        assert db.scalar(
            select(func.count()).select_from(ExternalPackagingPriceVersion)
        ) == price_count


def test_purchase_print_uses_frozen_price_and_order_drawing_source(
    purchase_app: FastAPI,
) -> None:
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
    from app.models.external_packaging_price import ExternalPackagingPriceVersion
    from app.models.order import OrderItem
    from app.models.supplier import ExternalPackagingProduct
    from app.models.user import User

    order_id = purchase_app.state.fixture["order_id"]
    with purchase_app.state.session_factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        item.drawing_file = "secure/order-drawings/匿名订单图纸_v2.pdf"
        db.commit()

    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        preview = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        confirmation = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json=_confirmation_payload(preview, "p1-40c-print"),
        )
        assert confirmation.status_code == 200, confirmation.text
        purchase_id = confirmation.json()["confirmation"]["purchase_orders"][0]["id"]
        before = client.get(
            f"/api/external-packaging-purchases/{purchase_id}/print"
        )
        assert before.status_code == 200, before.text
        before_rows = before.json()["items"]
        assert before_rows
        assert all(row["source_order_number"] == "TM20260809001" for row in before_rows)
        assert all(row["source_item_order_number"] == "TM20260809001-001" for row in before_rows)
        assert all(row["source_customer_name"] == "匿名采购客户" for row in before_rows)
        assert all(row["order_drawing_file_name"] == "匿名订单图纸_v2.pdf" for row in before_rows)
        assert all(row["order_drawing_version_label"] == "订单下单图纸（冻结文件）" for row in before_rows)
        frozen_prices = [row["unit_price"] for row in before_rows]

        with purchase_app.state.session_factory() as db:
            frozen_item = db.scalar(
                select(ExternalPackagingPurchaseItem).where(
                    ExternalPackagingPurchaseItem.purchase_order_id == purchase_id
                )
            )
            product = db.get(
                ExternalPackagingProduct, frozen_item.external_product_id_snapshot
            )
            admin = db.scalar(select(User).where(User.username == "purchase-admin"))
            current_versions = db.scalar(
                select(func.max(ExternalPackagingPriceVersion.version_number)).where(
                    ExternalPackagingPriceVersion.external_product_id == product.id
                )
            )
            db.add(
                _price_row(
                    product,
                    version=int(current_versions or 1) + 1,
                    value="99.99",
                    created_by=admin.id,
                )
            )
            db.commit()

        after = client.get(
            f"/api/external-packaging-purchases/{purchase_id}/print"
        )
        assert after.status_code == 200, after.text
        assert [row["unit_price"] for row in after.json()["items"]] == frozen_prices
        forbidden = {
            "material_code",
            "flute_type",
            "report_length_mm",
            "report_width_mm",
            "crease_type",
        }
        assert all(not forbidden.intersection(row) for row in after.json()["items"])