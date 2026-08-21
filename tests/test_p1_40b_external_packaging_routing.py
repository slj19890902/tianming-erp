from __future__ import annotations

from datetime import date
from decimal import Decimal
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from test_p1_40a_packaging_masterdata import (
    p1_40a_app,
    _external_payload,
    _honeycomb_payload,
)
from test_p1_33c2_order_external_component_snapshots import snapshot_app, _order_payload


@pytest.fixture()
def routing_app(p1_40a_app: FastAPI) -> FastAPI:
    from app.api.external_packaging_purchases import router as purchase_router
    from app.api.orders import router as orders_router
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.user import User
    from app.core.security import hash_password

    p1_40a_app.include_router(orders_router, prefix="/api/orders")
    p1_40a_app.include_router(purchase_router, prefix="/api")
    with p1_40a_app.state.factory() as db:
        scoped = User(
            username="p1-40b-scoped",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="受限报料查看",
            must_change_password=False,
            customer_access_mode="selected",
        )
        denied = User(
            username="p1-40b-denied",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="无报料权限",
            must_change_password=False,
        )
        db.add_all([scoped, denied])
        db.flush()
        scoped.permission_overrides.append(
            UserPermissionOverride(permission_code="requisition.view", is_allowed=True)
        )
        scoped.customer_scopes.append(
            UserCustomerScope(customer_id=p1_40a_app.state.fixture["customer_b"])
        )
        db.commit()
    return p1_40a_app


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200


def _order_payload_for(product_id: int, customer_id: int) -> dict:
    return {
        "customer_id": customer_id,
        "customer_po": "P1-40B-DIRECT-001",
        "order_date": "2026-08-11",
        "delivery_date": "2026-08-20",
        "items": [{
            "product_id": product_id,
            "quantity": 100,
            "unit_price": "2.50",
            "external_packaging_order_quantity_basis": "1",
            "external_packaging_purchase_quantity_basis": "2",
        }],
    }


def _seed_price(
    app: FastAPI,
    external_product_id: int,
    *,
    quote_unit: str = "米",
    unit_price: str = "1.10",
) -> None:
    from app.models.external_packaging_price import ExternalPackagingPriceVersion
    from app.models.supplier import ExternalPackagingProduct
    from app.models.user import User

    with app.state.factory() as db:
        product = db.get(ExternalPackagingProduct, external_product_id)
        admin = db.scalar(select(User).where(User.username == "p1-40a-admin"))
        assert product is not None and admin is not None
        db.add(
            ExternalPackagingPriceVersion(
                external_product_id=product.id,
                version_number=1,
                product_version=product.version,
                specification_snapshot_json=product.specification_json,
                quote_unit=quote_unit,
                unit_conversion_basis=(
                    "客户单根长度mm÷1000换算" if quote_unit == "米" else "采购单位直接计价"
                ),
                currency="CNY",
                tax_mode="tax_inclusive",
                tax_rate=Decimal("0.13"),
                unit_price=Decimal(unit_price),
                effective_from=date(2026, 1, 1),
                moq_quantity=Decimal("1"),
                moq_unit=quote_unit,
                packaging_multiple=Decimal("1"),
                tier_prices_json="[]",
                shipping_fee_mode="not_provided",
                evidence_reference="P1-40B-UAT",
                quote_fingerprint=f"p1-40b-{product.id}",
                created_by=admin.id,
            )
        )
        db.commit()


def test_direct_external_order_freezes_routes_skips_production_and_confirms(
    routing_app: FastAPI,
) -> None:
    from app.models.order import OrderItem
    from app.models.order_external_packaging import (
        SalesOrderItemExternalComponentCandidate,
        SalesOrderItemExternalComponent,
    )
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
    from app.models.production import ProductionTask
    from app.models.product import Product
    from app.models.requisition import RequisitionItem

    ids = routing_app.state.fixture
    with TestClient(routing_app) as client:
        _login(client, "p1-40a-admin")
        payload = _external_payload(
            ids,
            candidates=[
                {"external_product_id": ids["CG-870-A"], "is_default": True},
                {"external_product_id": ids["CG-870-G"], "is_default": False},
            ],
        )
        created_product = client.post("/api/master/products", json=payload)
        assert created_product.status_code == 201, created_product.text
        product_id = created_product.json()["id"]
        _seed_price(routing_app, ids["CG-870-A"])

        missing_ratio_payload = _order_payload_for(product_id, ids["customer_a"])
        missing_ratio_payload["items"][0].pop(
            "external_packaging_order_quantity_basis"
        )
        missing_ratio_payload["items"][0].pop(
            "external_packaging_purchase_quantity_basis"
        )
        created_order = client.post("/api/orders", json=missing_ratio_payload)
        assert created_order.status_code == 201, created_order.text
        order = created_order.json()
        order_id = order["id"]

        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
            assert item is not None
            assert item.supply_mode_snapshot == "external_purchase"
            assert item.external_packaging_category_code_snapshot == "paper_corner_guard"
            assert item.external_packaging_purchase_unit_snapshot == "根"
            assert item.external_packaging_product_version_snapshot == 1
            assert Decimal(
                item.external_packaging_order_quantity_basis_snapshot
            ) == Decimal("1")
            assert Decimal(
                item.external_packaging_purchase_quantity_basis_snapshot
            ) == Decimal("2")
            assert Decimal(
                item.external_packaging_quantity_per_finished_unit_snapshot
            ) == Decimal("2")
            assert item.requisition_status == "外购包材待确认"
            assert db.scalar(
                select(func.count()).select_from(ProductionTask).where(
                    ProductionTask.order_item_id == item.id
                )
            ) == 0
            assert db.scalar(
                select(func.count()).select_from(RequisitionItem).where(
                    RequisitionItem.order_item_id == item.id
                )
            ) == 0
            component = db.scalar(
                select(SalesOrderItemExternalComponent).where(
                    SalesOrderItemExternalComponent.sales_order_item_id == item.id
                )
            )
            assert component is not None
            assert component.source_kind == "direct_product"
            assert component.source_component_set_id is None
            assert component.source_component_id is None
            assert Decimal(component.quantity_per_finished_unit) == Decimal("2")
            assert Decimal(component.waste_rate) == Decimal("0")
            candidates = db.scalars(
                select(SalesOrderItemExternalComponentCandidate).where(
                    SalesOrderItemExternalComponentCandidate.order_component_id == component.id
                )
            ).all()
            assert len(candidates) == 2
            assert sum(bool(row.is_default) for row in candidates) == 1

            product = db.get(Product, product_id)
            assert product is not None
            product.external_packaging_specification_summary = "后改规格，不得重冻旧订单"
            product.external_packaging_candidate_snapshot_json = json.dumps(
                [{"external_product_id": ids["CG-1000"], "is_default": True}]
            )
            product.external_packaging_default_order_quantity_basis = Decimal("1")
            product.external_packaging_default_purchase_quantity_basis = Decimal("3")
            db.commit()

            frozen = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
            assert frozen is not None
            assert Decimal(frozen.external_packaging_order_quantity_basis_snapshot) == Decimal("1")
            assert Decimal(frozen.external_packaging_purchase_quantity_basis_snapshot) == Decimal("2")

        pending = client.get("/api/external-packaging-purchases/pending-confirmations")
        assert pending.status_code == 200
        assert pending.json()["total"] == 1
        assert pending.json()["items"][0]["order_id"] == order_id
        assert "870" in pending.json()["items"][0]["specification_summary"]

        preview = client.get(f"/api/orders/{order_id}/external-packaging-purchase")
        assert preview.status_code == 200, preview.text
        preview_data = preview.json()
        assert len(preview_data["items"]) == 1
        assert "870" in preview_data["items"][0]["specification_summary"]
        assert preview_data["items"][0]["suggested_purchase_quantity"] == "200"
        confirmation = {
            "idempotency_key": "p1-40b-direct-confirm-001",
            "lines": [
                {
                    "order_component_id": row["order_component_id"],
                    "candidate_id": row["default_candidate_id"],
                    "purchase_quantity": row["suggested_purchase_quantity"],
                }
                for row in preview_data["items"]
            ],
        }
        confirmed = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json=confirmation,
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["created"] is True
        repeated = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json=confirmation,
        )
        assert repeated.status_code == 200
        assert repeated.json()["created"] is False

        with routing_app.state.factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
            assert item is not None
            assert item.requisition_status == "外购包材已采购"
            assert Decimal(item.unit_price) == Decimal("2.50")
            purchase_item = db.scalar(select(ExternalPackagingPurchaseItem))
            assert purchase_item is not None
            assert Decimal(purchase_item.purchase_quantity) == Decimal("200")
            assert Decimal(purchase_item.unit_price) != Decimal(item.unit_price)
        assert client.get(
            "/api/external-packaging-purchases/pending-confirmations"
        ).json()["total"] == 0

    with TestClient(routing_app) as scoped_client:
        _login(scoped_client, "p1-40b-scoped")
        scoped = scoped_client.get(
            "/api/external-packaging-purchases/pending-confirmations"
        )
        assert scoped.status_code == 200
        assert scoped.json() == {"items": [], "total": 0}
    with TestClient(routing_app) as denied_client:
        _login(denied_client, "p1-40b-denied")
        assert denied_client.get(
            "/api/external-packaging-purchases/pending-confirmations"
        ).status_code == 403


def test_order_override_is_frozen_and_legacy_product_without_default_is_explicit(
    routing_app: FastAPI,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product

    ids = routing_app.state.fixture
    with TestClient(routing_app) as client:
        _login(client, "p1-40a-admin")
        created_product = client.post(
            "/api/master/products", json=_external_payload(ids)
        )
        assert created_product.status_code == 201, created_product.text
        product_id = created_product.json()["id"]

        override = _order_payload_for(product_id, ids["customer_a"])
        override["customer_po"] = "P1-40B-OVERRIDE-001"
        override["items"][0]["external_packaging_purchase_quantity_basis"] = "3"
        created_order = client.post("/api/orders", json=override)
        assert created_order.status_code == 201, created_order.text
        with routing_app.state.factory() as db:
            item = db.scalar(
                select(OrderItem).where(OrderItem.order_id == created_order.json()["id"])
            )
            assert item is not None
            assert Decimal(item.external_packaging_order_quantity_basis_snapshot) == Decimal("1")
            assert Decimal(item.external_packaging_purchase_quantity_basis_snapshot) == Decimal("3")

            product = db.get(Product, product_id)
            assert product is not None
            product.external_packaging_default_order_quantity_basis = None
            product.external_packaging_default_purchase_quantity_basis = None
            db.commit()

        missing = _order_payload_for(product_id, ids["customer_a"])
        missing["customer_po"] = "P1-40B-LEGACY-MISSING"
        missing["items"][0].pop("external_packaging_order_quantity_basis")
        missing["items"][0].pop("external_packaging_purchase_quantity_basis")
        rejected = client.post("/api/orders", json=missing)
        assert rejected.status_code == 422
        assert "常用箱尚未设置默认采购比例" in rejected.text

        explicit = _order_payload_for(product_id, ids["customer_a"])
        explicit["customer_po"] = "P1-40B-LEGACY-EXPLICIT"
        accepted = client.post("/api/orders", json=explicit)
        assert accepted.status_code == 201, accepted.text


def test_direct_honeycomb_customer_spec_and_order_ratio_reach_supplier_print(
    routing_app: FastAPI,
) -> None:
    ids = routing_app.state.fixture
    with TestClient(routing_app) as client:
        _login(client, "p1-40a-admin")
        created_product = client.post(
            "/api/master/products", json=_honeycomb_payload(ids)
        )
        assert created_product.status_code == 201, created_product.text
        _seed_price(
            routing_app,
            ids["HC-GENERAL"],
            quote_unit="片",
            unit_price="1.97",
        )

        order_payload = _order_payload_for(
            created_product.json()["id"], ids["customer_a"]
        )
        order_payload["customer_po"] = "P1-40B-HONEYCOMB"
        created_order = client.post("/api/orders", json=order_payload)
        assert created_order.status_code == 201, created_order.text
        order_id = created_order.json()["id"]

        preview_response = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert len(preview["items"]) == 1
        row = preview["items"][0]
        assert row["specification_summary"] == (
            "材质170*110*170，孔径15mm，800×180×60mm"
        )
        assert row["suggested_purchase_quantity"] == "200"

        confirmed_response = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json={
                "idempotency_key": "p1-40b-honeycomb-confirm",
                "lines": [
                    {
                        "order_component_id": row["order_component_id"],
                        "candidate_id": row["default_candidate_id"],
                        "purchase_quantity": row["suggested_purchase_quantity"],
                    }
                ],
            },
        )
        assert confirmed_response.status_code == 200, confirmed_response.text
        purchase_id = confirmed_response.json()["confirmation"]["purchase_orders"][0]["id"]
        printed_response = client.get(
            f"/api/external-packaging-purchases/{purchase_id}/print"
        )
        assert printed_response.status_code == 200, printed_response.text
        printed = printed_response.json()["items"][0]
        assert printed["material"] == "170*110*170"
        assert printed["aperture_mm"] == "15"
        assert printed["dimensions_mm"] == "800×180×60"
        assert printed["purchase_quantity"] == "200"
        assert printed["purchase_unit"] == "片"


def test_mixed_bom_keeps_paper_production_and_external_components(
    snapshot_app: FastAPI,
) -> None:
    from app.models.order import OrderItem
    from app.models.order_external_packaging import SalesOrderItemExternalComponent
    from app.models.product import Product
    from app.models.production import ProductionTask

    ids = snapshot_app.state.fixture
    with snapshot_app.state.session_factory() as db:
        product = db.get(Product, ids["product_one"])
        assert product is not None
        product.supply_mode = "mixed_bom"
        db.commit()

    with TestClient(snapshot_app) as client:
        _login(client, "snapshot-sales")
        response = client.post(
            "/api/orders",
            json=_order_payload(
                ids, product_key="product_one", customer_po="P1-40B-MIXED", quantity=50
            ),
        )
        assert response.status_code == 201, response.text
        order_id = response.json()["id"]

    with snapshot_app.state.session_factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        assert item is not None
        assert item.supply_mode_snapshot == "mixed_bom"
        assert db.scalar(
            select(func.count()).select_from(ProductionTask).where(
                ProductionTask.order_item_id == item.id
            )
        ) == 1
        components = db.scalars(
            select(SalesOrderItemExternalComponent).where(
                SalesOrderItemExternalComponent.sales_order_item_id == item.id
            )
        ).all()
        assert len(components) == 3
        assert {row.source_kind for row in components} == {"bound_component"}
