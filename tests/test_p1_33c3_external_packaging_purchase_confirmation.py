from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app.core.time_contract import beijing_today


def _price(product, *, unit_price: str, created_by: int | None = None):
    from app.models.external_packaging_price import ExternalPackagingPriceVersion

    return ExternalPackagingPriceVersion(
        external_product_id=product.id,
        version_number=1,
        product_version=product.version,
        specification_snapshot_json=product.specification_json,
        quote_unit=product.purchase_unit,
        unit_conversion_basis=None,
        currency="CNY",
        tax_mode="tax_inclusive",
        tax_rate=Decimal("0.13"),
        tax_amount_per_unit=None,
        unit_price=Decimal(unit_price),
        effective_from=date(2026, 1, 1),
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
        evidence_reference=f"UAT-{product.supplier_product_code}",
        quote_fingerprint=f"fingerprint-{product.id}",
        created_by=created_by,
    )


@pytest.fixture()
def purchase_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.external_packaging_purchases import router as purchase_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.external_packaging_component import (
        ProductExternalComponent,
        ProductExternalComponentCandidate,
        ProductExternalComponentSet,
    )
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.supplier import ExternalPackagingProduct, Supplier
    from app.models.user import User
    from app.services.order_external_packaging import freeze_order_item_external_components
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "external-purchase.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="purchase-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="采购管理员",
            must_change_password=False,
        )
        boss = User(
            username="purchase-boss",
            password_hash=hash_password("123456"),
            role="boss",
            real_name="老板",
            must_change_password=False,
        )
        sales = User(
            username="purchase-sales",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="销售",
            must_change_password=False,
        )
        db.add_all([admin, boss, sales])
        customer = Customer(name="匿名采购客户", customer_code="UAT-PURCHASE")
        db.add(customer)
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="UAT-PURCHASE-BOX",
            customer_material_code="UAT-PURCHASE-BOX",
            product_name="匿名外购包装纸箱",
            unit="只",
            box_category="normal",
        )
        db.add(product)
        db.flush()

        supplier_a = Supplier(
            standard_name="匿名包装供应商甲",
            normalized_name=normalize_supplier_identity("匿名包装供应商甲"),
            display_name="供应商甲",
            business_code="SUP-A",
            is_active=True,
            version=1,
        )
        supplier_b = Supplier(
            standard_name="匿名包装供应商乙",
            normalized_name=normalize_supplier_identity("匿名包装供应商乙"),
            display_name="供应商乙",
            business_code="SUP-B",
            is_active=True,
            version=1,
        )
        db.add_all([supplier_a, supplier_b])
        db.flush()

        definitions = (
            (supplier_a, "CORNER-A", "匿名护角", "paper_corner_guard", "根", "1.10"),
            (supplier_b, "CORNER-B", "匿名备选护角", "paper_corner_guard", "根", "1.20"),
            (supplier_b, "EPE-B", "匿名EPE", "epe_cushion", "套", "6.60"),
            (supplier_a, "CARTON-A", "匿名彩盒", "printed_folding_carton", "只", "2.50"),
            (supplier_a, "NOT-FROZEN", "未冻结产品", "other_packaging", "只", "9.90"),
        )
        external = {}
        for supplier, code, name, category, unit, price in definitions:
            row = ExternalPackagingProduct(
                supplier_id=supplier.id,
                category_code=category,
                supplier_product_code=code,
                normalized_supplier_product_code=code,
                product_name=name,
                purchase_unit=unit,
                specification_summary=f"匿名规格 {code}",
                specification_json=json.dumps({"code": code}, sort_keys=True),
                is_active=True,
                version=1,
            )
            db.add(row)
            db.flush()
            db.add(_price(row, unit_price=price, created_by=admin.id))
            external[code] = row

        component_set = ProductExternalComponentSet(
            product_id=product.id, version=1, is_current=True
        )
        specs = (
            (1, "四角防护", "4", "0.02", "根", external["CORNER-A"], external["CORNER-B"]),
            (2, "缓冲内衬", "1", "0", "套", external["EPE-B"], None),
            (3, "彩印内盒", "1", "0.01", "只", external["CARTON-A"], None),
        )
        for display_order, purpose, quantity, waste, unit, default, alternative in specs:
            component = ProductExternalComponent(
                display_order=display_order,
                purpose=purpose,
                quantity_per_finished_unit=Decimal(quantity),
                waste_rate=Decimal(waste),
                consumption_unit=unit,
                is_required=True,
                category_code=default.category_code,
                specification_json=default.specification_json,
                specification_summary=default.specification_summary,
            )
            candidates = [(default, True)]
            if alternative is not None:
                candidates.append((alternative, False))
            for candidate_product, is_default in candidates:
                component.candidates.append(
                    ProductExternalComponentCandidate(
                        external_product_id=candidate_product.id,
                        is_default=is_default,
                        supplier_id_snapshot=candidate_product.supplier_id,
                        supplier_name_snapshot=candidate_product.supplier.display_name,
                        supplier_product_code_snapshot=candidate_product.supplier_product_code,
                        product_name_snapshot=candidate_product.product_name,
                        purchase_unit_snapshot=candidate_product.purchase_unit,
                        customer_scope_id_snapshot=None,
                        external_product_version_snapshot=candidate_product.version,
                    )
                )
            component_set.components.append(component)
        db.add(component_set)
        order = Order(
            order_number="TM20260809001",
            customer_id=customer.id,
            customer_po="PO-UAT-PURCHASE",
            order_date=date(2026, 8, 9),
            total_amount=Decimal("100"),
            created_by=admin.id,
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            item_order_number="TM20260809001-001",
            item_sequence=1,
            quantity=100,
            unit_price=Decimal("1"),
            subtotal=Decimal("100"),
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
        )
        db.add(item)
        db.flush()
        freeze_order_item_external_components(db, order_item=item)
        db.commit()
        fixture = {
            "order_id": order.id,
            "not_frozen_product_id": external["NOT-FROZEN"].id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(purchase_router, prefix="/api")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = factory
    app.state.fixture = fixture
    yield app
    engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200


def _confirmation_payload(preview: dict, key: str = "purchase-key-001") -> dict:
    return {
        "idempotency_key": key,
        "lines": [
            {
                "order_component_id": row["order_component_id"],
                "candidate_id": row["default_candidate_id"],
                "purchase_quantity": row["suggested_purchase_quantity"],
            }
            for row in preview["items"]
        ],
    }


def test_one_confirmation_groups_three_components_into_two_supplier_orders(
    purchase_app: FastAPI,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        preview_response = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert [row["suggested_purchase_quantity"] for row in preview["items"]] == [
            "408",
            "100",
            "101",
        ]
        confirmed = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json=_confirmation_payload(preview),
        )
        assert confirmed.status_code == 200, confirmed.text
        data = confirmed.json()
        assert data["created"] is True
        orders = data["confirmation"]["purchase_orders"]
        assert len(orders) == 2
        assert sorted(len(row["items"]) for row in orders) == [1, 2]
        assert all(
            row["purchase_number"].startswith(f"EP-{beijing_today():%Y%m%d}-")
            for row in orders
        )

        reread = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        assert reread["status"] == "confirmed"
        assert len(reread["confirmation"]["purchase_orders"]) == 2

    with purchase_app.state.session_factory() as db:
        facts = {
            table: db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in (
                "external_packaging_purchase_batches",
                "external_packaging_purchase_orders",
                "external_packaging_purchase_items",
                "supplier_requisition_orders",
                "material_requisitions",
                "finished_goods_inventory_details",
            )
        }
        assert facts == {
            "external_packaging_purchase_batches": 1,
            "external_packaging_purchase_orders": 2,
            "external_packaging_purchase_items": 3,
            "supplier_requisition_orders": 0,
            "material_requisitions": 0,
            "finished_goods_inventory_details": 0,
        }


def test_retry_is_idempotent_and_conflicting_retry_is_rejected(
    purchase_app: FastAPI,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        preview = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        payload = _confirmation_payload(preview, "purchase-idempotent")
        first = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm", json=payload
        )
        second = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm", json=payload
        )
        assert first.status_code == second.status_code == 200
        assert first.json()["created"] is True
        assert second.json()["created"] is False
        assert (
            first.json()["confirmation"]["purchase_orders"]
            == second.json()["confirmation"]["purchase_orders"]
        )

        changed = json.loads(json.dumps(payload))
        changed["lines"][0]["purchase_quantity"] = "410"
        conflict = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm", json=changed
        )
        assert conflict.status_code == 409
        assert "内容不同" in conflict.text


def test_permission_and_frozen_candidate_price_gates(purchase_app: FastAPI) -> None:
    from app.models.external_packaging_price import ExternalPackagingPriceVersion
    from app.models.order_external_packaging import SalesOrderItemExternalComponentCandidate

    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-sales")
        assert client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).status_code == 403
        _login(client, "purchase-boss")
        assert client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).status_code == 403
        _login(client, "purchase-admin")
        preview = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        payload = _confirmation_payload(preview, "purchase-gates")
        payload["lines"][0]["candidate_id"] = 999999
        response = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm", json=payload
        )
        assert response.status_code == 409
        assert "冻结" in response.text

    with purchase_app.state.session_factory() as db:
        candidate = db.scalars(
            select(SalesOrderItemExternalComponentCandidate).order_by(
                SalesOrderItemExternalComponentCandidate.id
            )
        ).first()
        price = db.scalar(
            select(ExternalPackagingPriceVersion).where(
                ExternalPackagingPriceVersion.external_product_id
                == candidate.external_product_id_snapshot
            )
        )
        db.delete(price)
        db.commit()
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        preview = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        first = preview["items"][0]["candidates"][0]
        assert first["price"] is None
        assert "有效价格" in first["blocked_reason"]
        response = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json=_confirmation_payload(preview, "purchase-missing-price"),
        )
        assert response.status_code == 409
        assert "有效价格" in response.text


def test_confirmed_price_snapshot_does_not_follow_later_price_change(
    purchase_app: FastAPI,
) -> None:
    from app.models.external_packaging_price import ExternalPackagingPriceVersion

    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        preview = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        confirmed = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json=_confirmation_payload(preview, "purchase-history"),
        ).json()["confirmation"]
        original_prices = sorted(
            item["unit_price"]
            for order in confirmed["purchase_orders"]
            for item in order["items"]
        )
    with purchase_app.state.session_factory() as db:
        existing = db.scalars(select(ExternalPackagingPriceVersion)).first()
        db.add(
            ExternalPackagingPriceVersion(
                external_product_id=existing.external_product_id,
                version_number=2,
                product_version=existing.product_version,
                specification_snapshot_json=existing.specification_snapshot_json,
                quote_unit=existing.quote_unit,
                currency=existing.currency,
                tax_mode=existing.tax_mode,
                tax_rate=existing.tax_rate,
                unit_price=Decimal("99.99"),
                effective_from=date(2026, 8, 9),
                tier_prices_json="[]",
                shipping_fee_mode="not_provided",
                evidence_reference="UAT-NEW-PRICE",
                quote_fingerprint="later-price",
            )
        )
        db.commit()
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        reread = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()["confirmation"]
    assert sorted(
        item["unit_price"]
        for order in reread["purchase_orders"]
        for item in order["items"]
    ) == original_prices


def test_frontend_has_one_click_supplier_split_confirmation() -> None:
    source = Path("static/index.html").read_text(encoding="utf-8")
    assert "核对并确认采购" in source
    assert "确认采购并按供应商拆单" in source
    assert "external-packaging-purchase/confirm" in source
    assert "不会自动收货或增加库存" in source
    assert "selectedExternalPurchaseCandidate" in source


def test_external_purchase_print_is_cost_protected_read_only_and_uses_frozen_facts(
    purchase_app: FastAPI,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        preview = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        confirmed = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json=_confirmation_payload(preview, "purchase-print"),
        ).json()["confirmation"]
        purchase_id = confirmed["purchase_orders"][0]["id"]

        with purchase_app.state.session_factory() as db:
            before = {
                table: db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in (
                    "external_packaging_purchase_orders",
                    "external_packaging_purchase_items",
                    "supplier_requisition_orders",
                    "material_requisitions",
                    "finished_goods_inventory_details",
                    "inventory_movements",
                )
            }

        response = client.get(
            f"/api/external-packaging-purchases/{purchase_id}/print"
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["id"] == purchase_id
        assert data["purchase_number"].startswith(f"EP-{beijing_today():%Y%m%d}-")
        assert data["supplier"]["name"] in {"供应商甲", "供应商乙"}
        assert data["source"] == {
            "order_id": order_id,
            "order_number": "TM20260809001",
            "customer_po": "PO-UAT-PURCHASE",
            "customer_name": "匿名采购客户",
            "order_date": "2026-08-09",
            "delivery_date": None,
        }
        assert data["items"]
        assert all(row["unit_price"] for row in data["items"])
        assert all(row["purchase_quantity"] for row in data["items"])
        assert all(row["price_evidence_reference"].startswith("UAT-") for row in data["items"])
        assert "未计入采购单合计" in data["terms_note"]

        reread = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        assert reread["confirmation"]["purchase_orders"] == confirmed["purchase_orders"]
        assert reread["status"] == "confirmed"
        assert all(row["id"] for row in reread["confirmation"]["purchase_orders"])

    with purchase_app.state.session_factory() as db:
        after = {
            table: db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in before
        }
    assert after == before


def test_external_purchase_print_rejects_non_admin_and_missing_purchase(
    purchase_app: FastAPI,
) -> None:
    with TestClient(purchase_app) as client:
        _login(client, "purchase-sales")
        assert client.get(
            "/api/external-packaging-purchases/1/print"
        ).status_code == 403
        _login(client, "purchase-boss")
        assert client.get(
            "/api/external-packaging-purchases/1/print"
        ).status_code == 403
        _login(client, "purchase-admin")
        missing = client.get("/api/external-packaging-purchases/999999/print")
        assert missing.status_code == 404
        assert "采购单不存在" in missing.text
