from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


def _component(
    *,
    component_set,
    candidate_product,
    display_order: int,
    purpose: str,
    quantity: str,
    waste: str,
    consumption_unit: str,
    units_per_purchase_unit: str | None = None,
    conversion_basis: str | None = None,
):
    from app.models.external_packaging_component import (
        ProductExternalComponent,
        ProductExternalComponentCandidate,
    )

    row = ProductExternalComponent(
        display_order=display_order,
        purpose=purpose,
        quantity_per_finished_unit=quantity,
        waste_rate=waste,
        consumption_unit=consumption_unit,
        units_per_purchase_unit=units_per_purchase_unit,
        conversion_basis=conversion_basis,
        is_required=True,
        category_code=candidate_product.category_code,
        specification_json=candidate_product.specification_json,
        specification_summary=candidate_product.specification_summary,
    )
    row.candidates.append(
        ProductExternalComponentCandidate(
            external_product_id=candidate_product.id,
            is_default=True,
            supplier_id_snapshot=candidate_product.supplier_id,
            supplier_name_snapshot=candidate_product.supplier.display_name,
            supplier_product_code_snapshot=candidate_product.supplier_product_code,
            product_name_snapshot=candidate_product.product_name,
            purchase_unit_snapshot=candidate_product.purchase_unit,
            customer_scope_id_snapshot=candidate_product.customer_scope_id,
            external_product_version_snapshot=candidate_product.version,
        )
    )
    return row


@pytest.fixture()
def snapshot_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.external_packaging_component import ProductExternalComponentSet
    from app.models.product import Product
    from app.models.supplier import (
        ExternalPackagingProduct,
        Supplier,
        SupplierSupplyCategory,
    )
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "order-external-snapshots.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username="snapshot-admin",
                    password_hash=hash_password("123456"),
                    role="admin",
                    real_name="管理员",
                    must_change_password=False,
                ),
                User(
                    username="snapshot-sales",
                    password_hash=hash_password("123456"),
                    role="sales",
                    real_name="销售",
                    must_change_password=False,
                ),
            ]
        )
        customer = Customer(name="匿名外购组件客户", customer_code="UAT-EXT")
        db.add(customer)
        db.flush()
        products = [
            Product(
                customer_id=customer.id,
                product_code="EXT-BOX-001",
                customer_material_code="EXT-BOX-001",
                product_name="匿名带外购件纸箱",
                unit="只",
                box_category="normal",
            ),
            Product(
                customer_id=customer.id,
                product_code="EXT-BOX-002",
                customer_material_code="EXT-BOX-002",
                product_name="匿名换算纸箱",
                unit="只",
                box_category="normal",
            ),
        ]
        db.add_all(products)
        db.flush()

        definitions = (
            ("护角供应商", "CORNER", "paper_corner_guard", "根"),
            ("EPE供应商", "EPE", "epe_cushion", "套"),
            ("彩盒供应商", "CARTON", "printed_folding_carton", "只"),
            ("整箱供应商", "PACK100", "other_packaging", "箱"),
            ("未知换算供应商", "NO-CONVERSION", "other_packaging", "箱"),
        )
        external_products = {}
        for index, (name, code, category, unit) in enumerate(definitions, start=1):
            supplier = Supplier(
                standard_name=name,
                normalized_name=normalize_supplier_identity(name),
                display_name=name,
                is_active=True,
                sort_order=index,
                version=1,
                supply_categories=[
                    SupplierSupplyCategory(category_code=category, is_active=True)
                ],
            )
            external = ExternalPackagingProduct(
                category_code=category,
                supplier_product_code=code,
                normalized_supplier_product_code=code,
                product_name=f"匿名{code}",
                purchase_unit=unit,
                specification_summary=f"匿名规格{code}",
                specification_json=json.dumps(
                    {"code": code}, ensure_ascii=False, sort_keys=True
                ),
                is_active=True,
                version=1,
            )
            supplier.packaging_products.append(external)
            db.add(supplier)
            db.flush()
            external_products[code] = external

        set_one = ProductExternalComponentSet(
            product_id=products[0].id, version=1, is_current=True
        )
        set_one.components.extend(
            [
                _component(
                    component_set=set_one,
                    candidate_product=external_products["CORNER"],
                    display_order=1,
                    purpose="四角防护",
                    quantity="4",
                    waste="0.02",
                    consumption_unit="根",
                ),
                _component(
                    component_set=set_one,
                    candidate_product=external_products["EPE"],
                    display_order=2,
                    purpose="缓冲内衬",
                    quantity="1",
                    waste="0",
                    consumption_unit="套",
                ),
                _component(
                    component_set=set_one,
                    candidate_product=external_products["CARTON"],
                    display_order=3,
                    purpose="彩印内盒",
                    quantity="1",
                    waste="0.01",
                    consumption_unit="只",
                ),
            ]
        )
        set_two = ProductExternalComponentSet(
            product_id=products[1].id, version=1, is_current=True
        )
        set_two.components.extend(
            [
                _component(
                    component_set=set_two,
                    candidate_product=external_products["PACK100"],
                    display_order=1,
                    purpose="整箱采购",
                    quantity="1",
                    waste="0",
                    consumption_unit="件",
                    units_per_purchase_unit="100",
                    conversion_basis="供应商包装：每箱100件",
                ),
                _component(
                    component_set=set_two,
                    candidate_product=external_products["NO-CONVERSION"],
                    display_order=2,
                    purpose="缺少换算",
                    quantity="1",
                    waste="0",
                    consumption_unit="件",
                ),
            ]
        )
        db.add_all([set_one, set_two])
        db.commit()
        fixture = {
            "customer_id": customer.id,
            "product_one": products[0].id,
            "product_two": products[1].id,
            "corner_product": external_products["CORNER"].id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = factory
    app.state.fixture = fixture
    yield app
    engine.dispose()


def _login(client: TestClient, username: str = "snapshot-admin") -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200


def _order_payload(ids: dict, *, product_key: str, customer_po: str, quantity: int) -> dict:
    return {
        "customer_id": ids["customer_id"],
        "customer_po": customer_po,
        "order_date": "2026-08-09",
        "delivery_date": "2026-08-16",
        "items": [
            {
                "product_id": ids[product_key],
                "quantity": quantity,
                "unit_price": "1.00",
            }
        ],
    }


def test_new_order_freezes_three_components_and_calculates_purchase_suggestions(
    snapshot_app: FastAPI,
) -> None:
    ids = snapshot_app.state.fixture
    with TestClient(snapshot_app) as client:
        _login(client, "snapshot-sales")
        response = client.post(
            "/api/orders",
            json=_order_payload(
                ids, product_key="product_one", customer_po="PO-EXT-001", quantity=100
            ),
        )
        assert response.status_code == 201, response.text
        order = response.json()
        requirements = order["items"][0]["external_packaging_requirements"]
        assert [row["suggested_purchase_quantity"] for row in requirements] == [
            "408",
            "100",
            "101",
        ]
        assert [row["suggested_purchase_unit"] for row in requirements] == [
            "根",
            "套",
            "只",
        ]
        assert all("price" not in key for row in requirements for key in row)
        reread = client.get(f"/api/orders/{order['id']}")
        assert reread.status_code == 200
        assert (
            reread.json()["items"][0]["external_packaging_requirements"]
            == requirements
        )


def test_template_change_only_affects_new_order_and_freeze_is_idempotent(
    snapshot_app: FastAPI,
) -> None:
    from app.models.external_packaging_component import (
        ProductExternalComponent,
        ProductExternalComponentCandidate,
        ProductExternalComponentSet,
    )
    from app.models.order import OrderItem
    from app.models.order_external_packaging import SalesOrderItemExternalComponent
    from app.models.supplier import ExternalPackagingProduct
    from app.services.order_external_packaging import freeze_order_item_external_components

    ids = snapshot_app.state.fixture
    factory = snapshot_app.state.session_factory
    with TestClient(snapshot_app) as client:
        _login(client)
        old = client.post(
            "/api/orders",
            json=_order_payload(
                ids, product_key="product_one", customer_po="PO-EXT-OLD", quantity=100
            ),
        ).json()
        old_item_id = old["items"][0]["id"]
        with factory() as db:
            current = db.scalar(
                select(ProductExternalComponentSet).where(
                    ProductExternalComponentSet.product_id == ids["product_one"],
                    ProductExternalComponentSet.is_current.is_(True),
                )
            )
            current.is_current = False
            corner_product = db.get(ExternalPackagingProduct, ids["corner_product"])
            new_set = ProductExternalComponentSet(
                product_id=ids["product_one"], version=2, is_current=True
            )
            new_set.components.append(
                _component(
                    component_set=new_set,
                    candidate_product=corner_product,
                    display_order=1,
                    purpose="四角防护",
                    quantity="5",
                    waste="0.02",
                    consumption_unit="根",
                )
            )
            db.add(new_set)
            db.commit()
        new = client.post(
            "/api/orders",
            json=_order_payload(
                ids, product_key="product_one", customer_po="PO-EXT-NEW", quantity=100
            ),
        ).json()
        old_again = client.get(f"/api/orders/{old['id']}").json()

    assert old_again["items"][0]["external_packaging_requirements"][0][
        "suggested_purchase_quantity"
    ] == "408"
    assert new["items"][0]["external_packaging_requirements"][0][
        "suggested_purchase_quantity"
    ] == "510"
    with factory() as db:
        old_item = db.get(OrderItem, old_item_id)
        before = db.scalar(
            select(func.count())
            .select_from(SalesOrderItemExternalComponent)
            .where(SalesOrderItemExternalComponent.sales_order_item_id == old_item_id)
        )
        freeze_order_item_external_components(db, order_item=old_item)
        after = db.scalar(
            select(func.count())
            .select_from(SalesOrderItemExternalComponent)
            .where(SalesOrderItemExternalComponent.sales_order_item_id == old_item_id)
        )
        assert before == after == 3


def test_cross_unit_conversion_rounds_up_and_missing_evidence_blocks_suggestion(
    snapshot_app: FastAPI,
) -> None:
    ids = snapshot_app.state.fixture
    with TestClient(snapshot_app) as client:
        _login(client)
        response = client.post(
            "/api/orders",
            json=_order_payload(
                ids, product_key="product_two", customer_po="PO-EXT-BOX", quantity=101
            ),
        )
        assert response.status_code == 201, response.text
        requirements = response.json()["items"][0]["external_packaging_requirements"]
    assert requirements[0]["requirement_quantity"] == "101"
    assert requirements[0]["suggested_purchase_quantity"] == "2"
    assert requirements[0]["suggested_purchase_unit"] == "箱"
    assert requirements[1]["suggested_purchase_quantity"] is None
    assert "缺少换算依据" in requirements[1]["suggestion_blocked_reason"]


def test_all_order_entry_routes_share_the_same_snapshot_hook() -> None:
    orders_source = Path("app/api/orders.py").read_text(encoding="utf-8")
    contracts_source = Path("app/api/contracts.py").read_text(encoding="utf-8")
    assert orders_source.count("freeze_order_item_external_components(") == 1
    assert "payload.pdf_import_confirmation" in orders_source
    assert "return _create_order_impl(" in orders_source
    assert "order_data = _create_order_impl(" in contracts_source


def test_frontend_guides_users_to_review_external_packaging_without_prices() -> None:
    source = Path("static/index.html").read_text(encoding="utf-8")
    assert "下一步：外购包装待确认" in source
    assert "不会自动选供应商或生成采购单" in source
    assert "suggested_purchase_quantity" in source
    assert "alternative_candidates" in source
    assert 'externalPackagingUnits:["根","米","件","张","令","kg","吨","只","个","套","片","卷","箱"]' in source
