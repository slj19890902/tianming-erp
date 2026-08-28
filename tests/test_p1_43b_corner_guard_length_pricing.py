from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_p1_40a_packaging_masterdata import (
    _external_payload,
    _login as login_master,
    p1_40a_app,
)
from test_p1_40b_external_packaging_routing import (
    _seed_price,
    routing_app,
)
from test_p1_33c2_order_external_component_snapshots import (
    _login as login_snapshot,
    _order_payload,
    snapshot_app,
)
from test_p1_33b_external_packaging_prices import (
    _login as login_price,
    _product_ids as price_product_ids,
    _price_payload,
    price_app,
)


def _customer_specification(length_mm: int, *, side_a_mm: int = 50) -> dict:
    return {
        "shape": "L",
        "length_mm": length_mm,
        "side_a_mm": side_a_mm,
        "side_b_mm": 50,
        "thickness_mm": 5,
    }


def _product_payload(
    ids: dict[str, int],
    *,
    customer_id: int,
    code: str,
    external_product_id: int,
    length_mm: int,
    sale_unit_price: str,
) -> dict:
    payload = _external_payload(
        ids,
        candidates=[
            {"external_product_id": external_product_id, "is_default": True}
        ],
    )
    payload.update(
        customer_id=customer_id,
        product_code=code,
        customer_material_code=code,
        product_name=f"匿名客户定长护角 {length_mm}",
        unit="根",
        sale_unit_price=sale_unit_price,
        external_packaging_default_order_quantity_basis="1",
        external_packaging_default_purchase_quantity_basis="1",
    )
    payload["external_supply"]["customer_specification"] = (
        _customer_specification(length_mm)
    )
    return payload


def test_exact_meter_to_root_cost_formula() -> None:
    from app.services.corner_guard_pricing import calculate_corner_guard_cost

    result = calculate_corner_guard_cost(
        customer_specification=_customer_specification(780),
        root_quantity=Decimal("1000"),
        price=SimpleNamespace(
            quote_unit="米",
            unit_price=Decimal("1.10"),
            tier_prices_json="[]",
        ),
    )
    assert result["specification_summary"] == "780×50×50×5mm"
    assert result["length_m_per_root"] == Decimal("0.780000")
    assert result["pricing_quantity_m"] == Decimal("780.000000")
    assert result["unit_cost_per_root"] == Decimal("0.858000")
    assert result["total_cost"] == Decimal("858.00")


def test_corner_guard_formal_price_rejects_non_meter_unit(
    price_app: FastAPI,
) -> None:
    from app.models.supplier import ExternalPackagingProduct

    product_id = price_product_ids(price_app)[0]
    with price_app.state.session_factory() as db:
        product = db.get(ExternalPackagingProduct, product_id)
        product.category_code = "paper_corner_guard"
        db.commit()

    with TestClient(price_app) as client:
        login_price(client, "price-admin")
        root_price = _price_payload("1.10")
        blocked = client.post(
            f"/api/master/external-packaging/products/{product_id}/prices",
            json=root_price,
        )
        assert blocked.status_code == 422
        assert "报价单位必须为“米”" in blocked.json()["detail"]

        meter_price = _price_payload("1.10")
        meter_price.update(
            quote_unit="米",
            moq_unit="米",
            unit_conversion_basis="客户单根长度mm÷1000换算",
        )
        saved = client.post(
            f"/api/master/external-packaging/products/{product_id}/prices",
            json=meter_price,
        )
        assert saved.status_code == 201, saved.text
        assert saved.json()["item"]["quote_unit"] == "米"


def test_customer_lengths_and_sale_prices_are_independent_from_supplier_master(
    p1_40a_app: FastAPI,
) -> None:
    from app.models.supplier import ExternalPackagingProduct

    ids = p1_40a_app.state.fixture
    supplier_product_id = ids["CG-870-G"]
    with p1_40a_app.state.factory() as db:
        supplier_before = db.get(ExternalPackagingProduct, supplier_product_id)
        before = (
            supplier_before.version,
            supplier_before.specification_json,
        )

    with TestClient(p1_40a_app) as client:
        login_master(client)
        first = client.post(
            "/api/master/products",
            json=_product_payload(
                ids,
                customer_id=ids["customer_a"],
                code="P143B-CG-780",
                external_product_id=supplier_product_id,
                length_mm=780,
                sale_unit_price="3.80",
            ),
        )
        second = client.post(
            "/api/master/products",
            json=_product_payload(
                ids,
                customer_id=ids["customer_b"],
                code="P143B-CG-870",
                external_product_id=supplier_product_id,
                length_mm=870,
                sale_unit_price="4.20",
            ),
        )
        assert first.status_code == second.status_code == 201
        assert first.json()["external_supply"]["specification"]["length_mm"] == 780
        assert second.json()["external_supply"]["specification"]["length_mm"] == 870
        assert first.json()["sale_unit_price"] == "3.8000"
        assert second.json()["sale_unit_price"] == "4.2000"

    with p1_40a_app.state.factory() as db:
        supplier_after = db.get(ExternalPackagingProduct, supplier_product_id)
        assert (supplier_after.version, supplier_after.specification_json) == before


def test_pure_external_order_freezes_formal_cost_without_overwriting_sale_price(
    routing_app: FastAPI,
) -> None:
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
    from app.models.order import OrderItem
    from app.models.order_material_cost_snapshot import (
        SalesOrderItemMaterialCostSnapshot,
    )
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.requisition import RequisitionItem

    ids = routing_app.state.fixture
    with TestClient(routing_app) as client:
        login_master(client)
        product_response = client.post(
            "/api/master/products",
            json=_product_payload(
                ids,
                customer_id=ids["customer_a"],
                code="P143B-PURE-780",
                external_product_id=ids["CG-870-A"],
                length_mm=780,
                sale_unit_price="3.80",
            ),
        )
        assert product_response.status_code == 201, product_response.text
        product_id = product_response.json()["id"]
        _seed_price(routing_app, ids["CG-870-A"])

        order_response = client.post(
            "/api/orders",
            json={
                "customer_id": ids["customer_a"],
                "customer_po": "P143B-PURE-1000",
                "order_date": "2026-08-11",
                "delivery_date": "2026-08-20",
                "items": [
                    {
                        "product_id": product_id,
                        "quantity": 1000,
                        "unit_price": "5.00",
                    }
                ],
            },
        )
        assert order_response.status_code == 201, order_response.text
        order_id = order_response.json()["id"]
        preview = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        )
        assert preview.status_code == 200, preview.text
        line = preview.json()["items"][0]
        candidate = line["candidates"][0]
        assert line["suggested_purchase_quantity"] == "1000"
        assert candidate["price"]["quote_unit"] == "米"
        assert candidate["price"]["unit_price"] == "1.1"
        assert candidate["price"]["purchase_unit"] == "根"
        assert candidate["price"]["purchase_unit_price"] == "0.858"
        assert candidate["price"]["pricing_quantity_m"] == "780"

        confirmation = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json={
                "idempotency_key": "p1-43b-pure-780",
                "lines": [
                    {
                        "order_component_id": line["order_component_id"],
                        "candidate_id": line["default_candidate_id"],
                        "purchase_quantity": "1000",
                    }
                ],
            },
        )
        assert confirmation.status_code == 200, confirmation.text

    with routing_app.state.factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        product = db.get(Product, product_id)
        material_snapshot = db.scalar(
            select(SalesOrderItemMaterialCostSnapshot).where(
                SalesOrderItemMaterialCostSnapshot.sales_order_item_id == item.id
            )
        )
        purchase_item = db.scalar(
            select(ExternalPackagingPurchaseItem).where(
                ExternalPackagingPurchaseItem.sales_order_item_id == item.id
            )
        )
        assert Decimal(product.sale_unit_price) == Decimal("3.8000")
        assert Decimal(item.unit_price) == Decimal("5.0000")
        assert material_snapshot.calculation_status == "calculated"
        assert material_snapshot.estimated_material_unit_cost == Decimal("0.8580")
        assert material_snapshot.estimated_material_total_cost == Decimal("858.0000")
        component = json.loads(material_snapshot.components_json)[0]
        assert component["quote_unit"] == "米"
        assert component["required_piece_quantity"] == 1000
        assert Decimal(purchase_item.unit_price) == Decimal("0.858000")
        assert Decimal(purchase_item.line_amount) == Decimal("858.00")
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


def test_mixed_bom_uses_customer_root_count_and_keeps_paper_production(
    snapshot_app: FastAPI,
) -> None:
    from app.models.external_packaging_component import ProductExternalComponent
    from app.models.external_packaging_price import ExternalPackagingPriceVersion
    from app.models.order import OrderItem
    from app.models.order_material_cost_snapshot import SalesOrderItemMaterialCostSnapshot
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.supplier import ExternalPackagingProduct
    from app.models.user import User

    ids = snapshot_app.state.fixture
    with snapshot_app.state.session_factory() as db:
        product = db.get(Product, ids["product_one"])
        product.supply_mode = "mixed_bom"
        supplier_product = db.get(ExternalPackagingProduct, ids["corner_product"])
        supplier_product.specification_json = json.dumps(
            _customer_specification(870), ensure_ascii=False, sort_keys=True
        )
        component = db.scalar(
            select(ProductExternalComponent).where(
                ProductExternalComponent.purpose == "四角防护"
            )
        )
        component.specification_json = json.dumps(
            _customer_specification(780), ensure_ascii=False, sort_keys=True
        )
        component.specification_summary = "780×50×50×5mm"
        component.waste_rate = Decimal("0")
        admin = db.scalar(select(User).where(User.username == "snapshot-admin"))
        db.add(
            ExternalPackagingPriceVersion(
                external_product_id=supplier_product.id,
                version_number=1,
                product_version=supplier_product.version,
                specification_snapshot_json=supplier_product.specification_json,
                quote_unit="米",
                unit_conversion_basis="客户单根长度mm÷1000换算",
                currency="CNY",
                tax_mode="tax_inclusive",
                tax_rate=Decimal("0.13"),
                unit_price=Decimal("1.10"),
                effective_from=date(2026, 1, 1),
                moq_quantity=Decimal("1"),
                moq_unit="米",
                tier_prices_json="[]",
                shipping_fee_mode="not_provided",
                evidence_reference="P1-43B-MIXED",
                quote_fingerprint="p1-43b-mixed-price",
                created_by=admin.id,
            )
        )
        db.commit()

    with TestClient(snapshot_app) as client:
        login_snapshot(client, "snapshot-sales")
        response = client.post(
            "/api/orders",
            json=_order_payload(
                ids,
                product_key="product_one",
                customer_po="P143B-MIXED-100",
                quantity=100,
            ),
        )
        assert response.status_code == 201, response.text
        order_id = response.json()["id"]
        requirements = response.json()["items"][0]["external_packaging_requirements"]
        guard = next(row for row in requirements if row["purpose"] == "四角防护")
        assert guard["requirement_quantity"] == "400"
        assert guard["suggested_purchase_quantity"] == "400"
        assert guard["suggested_purchase_unit"] == "根"

    with snapshot_app.state.session_factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        snapshot = db.scalar(
            select(SalesOrderItemMaterialCostSnapshot).where(
                SalesOrderItemMaterialCostSnapshot.sales_order_item_id == item.id
            )
        )
        components = json.loads(snapshot.components_json)
        guard_cost = next(
            row for row in components if row["source_type"] == "external_corner_guard"
        )
        assert guard_cost["required_piece_quantity"] == 400
        assert Decimal(guard_cost["estimated_unit_cost_per_root"]) == Decimal("0.858000")
        assert Decimal(guard_cost["estimated_material_cost"]) == Decimal("343.20")
        assert db.scalar(
            select(func.count()).select_from(ProductionTask).where(
                ProductionTask.order_item_id == item.id
            )
        ) == 1


def test_missing_customer_length_mismatched_scope_and_inactive_candidate_fail_closed(
    p1_40a_app: FastAPI,
) -> None:
    from app.models.supplier import ExternalPackagingProduct

    ids = p1_40a_app.state.fixture
    with TestClient(p1_40a_app) as client:
        login_master(client)
        missing = _product_payload(
            ids,
            customer_id=ids["customer_a"],
            code="P143B-MISSING-LENGTH",
            external_product_id=ids["CG-870-A"],
            length_mm=780,
            sale_unit_price="3.80",
        )
        missing["external_supply"]["customer_specification"]["length_mm"] = None
        response = client.post("/api/master/products", json=missing)
        assert response.status_code == 422
        assert "长度" in response.text

        mismatched = _product_payload(
            ids,
            customer_id=ids["customer_a"],
            code="P143B-BAD-SECTION",
            external_product_id=ids["CG-870-A"],
            length_mm=780,
            sale_unit_price="3.80",
        )
        mismatched["external_supply"]["customer_specification"]["side_a_mm"] = 60
        response = client.post("/api/master/products", json=mismatched)
        assert response.status_code == 422
        assert "截面" in response.text

        cross_scope = _product_payload(
            ids,
            customer_id=ids["customer_a"],
            code="P143B-CROSS-SCOPE",
            external_product_id=ids["CG-B"],
            length_mm=780,
            sale_unit_price="3.80",
        )
        response = client.post("/api/master/products", json=cross_scope)
        assert response.status_code == 409
        assert "其他客户" in response.text

    with p1_40a_app.state.factory() as db:
        candidate = db.get(ExternalPackagingProduct, ids["CG-870-A"])
        candidate.is_active = False
        db.commit()
    with TestClient(p1_40a_app) as client:
        login_master(client)
        inactive = _product_payload(
            ids,
            customer_id=ids["customer_a"],
            code="P143B-INACTIVE",
            external_product_id=ids["CG-870-A"],
            length_mm=780,
            sale_unit_price="3.80",
        )
        response = client.post("/api/master/products", json=inactive)
        assert response.status_code == 409
        assert "停用" in response.text
