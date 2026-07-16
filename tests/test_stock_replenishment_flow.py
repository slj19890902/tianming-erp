from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def stock_replenishment_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseLocation

    engine = create_sqlite_engine(tmp_path / "stock-replenishment.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        user = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="admin",
            display_name="admin",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="TH",
            name="天华测试客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add_all([user, customer])
        session.flush()
        material = Material(
            code="A416D",
            layer_count=5,
            supplier_name="苏州佳丰",
            is_active=True,
        )
        session.add(material)
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="21301010",
            customer_material_code="TH-21301010",
            product_name="天华测试外箱",
            material_id=material.id,
            legacy_material_text="A416D/AB",
            length_mm=Decimal("1160"),
            width_mm=Decimal("665"),
            height_mm=Decimal("160"),
            box_category="normal",
            flute_type="AB",
            layer_count=5,
            report_length_mm=1865,
            report_width_mm=830,
            crease_type="压线",
            crease_left_mm=335,
            crease_middle_mm=160,
            crease_right_mm=335,
        )
        locations = [
            WarehouseLocation(
                location_code="FG-A01",
                location_name="成品A01",
                warehouse_type="finished",
                is_active=True,
            ),
            WarehouseLocation(
                location_code="SI-A01",
                location_name="半成品A01",
                warehouse_type="semi_finished",
                is_active=True,
            ),
            WarehouseLocation(
                location_code="V11-FG-A01",
                location_name="三楼 V11 成品货位",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=3,
                source_version="V11",
            ),
        ]
        session.add_all([product, *locations])
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "RolePass123!"},
    )
    assert response.status_code == 200


def _semi_policy_payload() -> dict:
    return {
        "policy_name": "天华共享纸板 1865x830",
        "target_inventory_type": "semi_finished",
        "customer_id": 1,
        "material_code": "A416D",
        "layer_count": 5,
        "flute_type": "AB",
        "report_length_mm": 1865,
        "report_width_mm": 830,
        "sheet_type": "raw_board",
        "component_type": "whole",
        "pieces_per_box": 1,
        "stock_yield_per_sheet": 1,
        "warning_quantity": 10,
        "target_quantity": 50,
        "default_location_id": 2,
        "supplier_name": "佳丰",
        "active": True,
    }


def test_historical_purchase_endpoint_reads_imported_database_rows(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.historical_purchase import HistoricalPurchaseEntry

    with session_factory() as db:
        db.add(
            HistoricalPurchaseEntry(
                source_workbook="2025年采购单.xlsx",
                source_sheet="2020.1-2026",
                source_row=22054,
                source_file_sha256="a" * 64,
                source_fingerprint="b" * 64,
                supplier_name="佳丰",
                record_date=date(2026, 6, 24),
                product_reference="21302053美国衬板26*45",
                search_text="21302053美国衬板26*45",
                normalized_search_text="21302053美国衬板2645B4CB",
                material_code="B4C/B",
                historical_quantity=270,
                report_length_mm=1120,
                report_width_mm=635,
                crease_type="净料",
                product_id=1,
                customer_id=1,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client)
        response = client.get(
            "/api/requisition/historical-purchases/search",
            params={"q": "21302053"},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["indexed_records"] == 1
    assert payload["items"][0]["source_row"] == 22054
    assert payload["items"][0]["report_width_mm"] == 635


def test_warning_policy_creates_prefilled_replenishment_draft(
    stock_replenishment_app,
) -> None:
    app, _session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/stock-policies",
            json=_semi_policy_payload(),
        )
        assert created.status_code == 201, created.text
        policy = created.json()
        assert policy["warning_triggered"] is True
        assert policy["available_quantity"] == 0
        assert policy["suggested_replenishment_quantity"] == 50

        draft = client.get(
            f"/api/requisition/stock-policies/{policy['id']}/replenishment-draft"
        )
        assert draft.status_code == 200
        assert draft.json()["items"][0]["quantity"] == 50
        assert draft.json()["items"][0]["location_id"] == 2


def test_formal_replenishment_rejects_v11_locations_and_policies(
    stock_replenishment_app,
) -> None:
    app, _session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)

        locations = client.get("/api/requisition/stock-replenishment/locations")
        assert locations.status_code == 200, locations.text
        assert {row["location_code"] for row in locations.json()["items"]} == {
            "FG-A01",
            "SI-A01",
        }

        policy_payload = _semi_policy_payload()
        policy_payload["default_location_id"] = 3
        rejected_policy = client.post(
            "/api/requisition/stock-policies", json=policy_payload
        )
        assert rejected_policy.status_code == 409, rejected_policy.text
        assert "V11 三楼 Phase A" in rejected_policy.json()["detail"]

        valid_policy = client.post(
            "/api/requisition/stock-policies", json=_semi_policy_payload()
        )
        assert valid_policy.status_code == 201, valid_policy.text
        update_payload = _semi_policy_payload()
        update_payload["default_location_id"] = 3
        rejected_update = client.put(
            f"/api/requisition/stock-policies/{valid_policy.json()['id']}",
            json=update_payload,
        )
        assert rejected_update.status_code == 409, rejected_update.text
        assert "V11 三楼 Phase A" in rejected_update.json()["detail"]

        rejected_item = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "finished",
                        "product_id": 1,
                        "quantity": 1,
                        "location_id": 3,
                    }
                ],
            },
        )
        assert rejected_item.status_code == 409, rejected_item.text
        assert "V11 三楼 Phase A" in rejected_item.json()["detail"]


def test_historical_replenishment_can_stock_one_traceable_semi_finished_lot(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        policy = client.post(
            "/api/requisition/stock-policies",
            json=_semi_policy_payload(),
        ).json()
        response = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "manual_history",
                "supplier_name": "佳丰",
                "customer_id": 1,
                "stock_now": True,
                "items": [
                    {
                        "stock_policy_id": policy["id"],
                        "target_inventory_type": "semi_finished",
                        "product_name": "21301010 历史纸板",
                        "quantity": 30,
                        "location_id": 2,
                        "historical_workbook": "2025年采购单.xlsx",
                        "historical_sheet": "2020.1-2026",
                        "historical_row": 333,
                        "historical_search_text": "21301010 116*66.5*16",
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        order = response.json()
        assert order["status"] == "stocked"
        assert order["stocked_quantity"] == 30
        assert order["items"][0]["inventory_lot"]["quantity_available"] == 30
        assert order["items"][0]["historical_source"]["row"] == 333

        repeated = client.post(
            f"/api/requisition/stock-replenishment/orders/{order['id']}/stock"
        )
        assert repeated.status_code == 200
        assert repeated.json()["items"][0]["inventory_lot"]["id"] == order["items"][0]["inventory_lot"]["id"]

        policies = client.get(
            "/api/requisition/stock-policies?warning_only=true"
        ).json()
        assert policies["items"] == []

    from app.models.warehouse_inventory import InventoryLot

    with session_factory() as session:
        assert session.scalar(select(func.count(InventoryLot.id))) == 1
        lot = session.scalar(select(InventoryLot))
        assert lot is not None
        assert lot.source_type == "replenishment"
        assert lot.source_ref_type == "stock_replenishment_item"
        assert lot.quantity_available == 30


def test_finished_replenishment_uses_product_and_finished_location(
    stock_replenishment_app,
) -> None:
    app, _session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        policy_response = client.post(
            "/api/requisition/stock-policies",
            json={
                "policy_name": "21301010 成品安全库存",
                "target_inventory_type": "finished",
                "product_id": 1,
                "warning_quantity": 5,
                "target_quantity": 20,
                "default_location_id": 1,
            },
        )
        assert policy_response.status_code == 201, policy_response.text
        policy = policy_response.json()
        order_response = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "stock_warning",
                "stock_now": True,
                "items": [
                    {
                        "stock_policy_id": policy["id"],
                        "target_inventory_type": "finished",
                        "product_id": 1,
                        "quantity": 12,
                        "location_id": 1,
                    }
                ],
            },
        )
        assert order_response.status_code == 201, order_response.text
        item = order_response.json()["items"][0]
        assert item["target_inventory_type"] == "finished"
        assert item["product_code"] == "21301010"
        assert item["inventory_lot"]["quantity_available"] == 12


def test_common_box_and_material_master_prefill_traceable_semi_stock(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        products = client.get(
            "/api/requisition/stock-replenishment/products",
            params={"customer_id": 1},
        )
        assert products.status_code == 200, products.text
        product = products.json()["items"][0]
        assert product["customer_material_code"] == "TH-21301010"
        assert [
            float(product["length_mm"]),
            float(product["width_mm"]),
            float(product["height_mm"]),
        ] == [1160, 665, 160]
        assert product["material_id"] == 1
        assert product["material_supplier_name"] == "苏州佳丰"
        assert product["report_length_mm"] == 1865
        assert product["report_width_mm"] == 830
        assert product["crease_type"] == "压线"
        assert [
            product["crease_left_mm"],
            product["crease_middle_mm"],
            product["crease_right_mm"],
        ] == [335, 160, 335]

        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "supplier_name": "错误供应商",
                "stock_now": True,
                "items": [
                    {
                        "target_inventory_type": "semi_finished",
                        "customer_id": 1,
                        "product_id": 1,
                        "material_id": 1,
                        "material_code": "WRONG-TEXT-IS-NOT-USED",
                        "layer_count": 5,
                        "flute_type": "AB",
                        "report_length_mm": 1865,
                        "report_width_mm": 830,
                        "crease_type": "压线",
                        "crease_left_mm": 335,
                        "crease_middle_mm": 160,
                        "crease_right_mm": 335,
                        "quantity": 30,
                        "location_id": 2,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        created_payload = created.json()
        assert created_payload["supplier_name"] == "苏州佳丰"
        item = created_payload["items"][0]
        assert item["product_id"] == 1
        assert item["material_id"] == 1
        assert item["material_code"] == "A416D"

    from app.models.warehouse_inventory import SemiFinishedInventoryDetail

    with session_factory() as session:
        detail = session.scalar(select(SemiFinishedInventoryDetail))
        assert detail is not None
        assert detail.material_id == 1
        assert detail.material_code_snapshot == "A416D"


def test_replenishment_rejects_incomplete_or_mismatched_crease(
    stock_replenishment_app,
) -> None:
    app, _session_factory = stock_replenishment_app
    base = {
        "target_inventory_type": "semi_finished",
        "material_id": 1,
        "material_code": "A416D",
        "layer_count": 5,
        "flute_type": "AB",
        "report_length_mm": 1865,
        "report_width_mm": 830,
        "crease_type": "压线",
        "crease_left_mm": 335,
        "crease_middle_mm": 160,
        "quantity": 1,
        "location_id": 2,
    }
    with TestClient(app) as client:
        _login(client)
        incomplete = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={"source_type": "customer_request", "stock_now": False, "items": [base]},
        )
        assert incomplete.status_code == 422

        mismatched = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "stock_now": False,
                "items": [{**base, "crease_right_mm": 330}],
            },
        )
        assert mismatched.status_code == 400
        assert "必须等于报料宽" in mismatched.json()["detail"]


def test_replenishment_order_can_save_multiple_lines_before_stocking(
    stock_replenishment_app,
) -> None:
    app, _session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "supplier_name": "佳丰",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "semi_finished",
                        "product_name": "库存纸板A",
                        "material_code": "A416D",
                        "layer_count": 5,
                        "flute_type": "AB",
                        "report_length_mm": 1865,
                        "report_width_mm": 830,
                        "quantity": 30,
                        "location_id": 2,
                    },
                    {
                        "target_inventory_type": "semi_finished",
                        "product_name": "库存纸板B",
                        "material_code": "K9C7J",
                        "layer_count": 5,
                        "flute_type": "AB",
                        "report_length_mm": 1580,
                        "report_width_mm": 550,
                        "quantity": 50,
                        "location_id": 2,
                    },
                ],
            },
        )
        assert response.status_code == 201, response.text
        order = response.json()
        assert order["status"] == "confirmed"
        assert order["total_quantity"] == 80
        assert len(order["items"]) == 2
        printable = client.get(
            f"/api/requisition/stock-replenishment/orders/{order['id']}/print"
        )
        assert printable.status_code == 200
        assert len(printable.json()["items"]) == 2
        reported = client.get("/api/requisition/reported-documents")
        assert reported.status_code == 200
        row = next(
            item
            for item in reported.json()["items"]
            if item["document_number"] == order["order_number"]
        )
        assert row["source_type"] == "stock_replenishment"
        assert row["incoming_status"] == "待入库"
        assert row["requisition_qty"] == 80
