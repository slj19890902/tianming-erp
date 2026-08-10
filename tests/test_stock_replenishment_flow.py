from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import hashlib
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def stock_replenishment_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.incoming import router as incoming_router
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.supplier import Supplier, SupplierAlias
    from app.models.user import User
    from app.models.warehouse_inventory import (
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )
    from app.services.supplier_master import normalize_supplier_identity

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
        supplier = Supplier(
            standard_name="苏州佳丰",
            normalized_name=normalize_supplier_identity("苏州佳丰"),
            display_name="佳丰",
            sort_order=10,
            is_active=True,
            version=1,
        )
        session.add_all([user, customer, supplier])
        session.flush()
        session.add(
            SupplierAlias(
                supplier_id=supplier.id,
                alias_name="佳丰",
                normalized_alias=normalize_supplier_identity("佳丰"),
            )
        )
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
        floor1 = WarehouseFloor(
            floor_code="F1",
            floor_name="一楼",
            floor_number=1,
            construction_status="enabled",
        )
        session.add(floor1)
        session.flush()
        session.add_all(
            [
                WarehouseArea(
                    floor_id=floor1.id,
                    area_code="A1",
                    area_name="A1原料暂存区",
                    construction_status="enabled",
                ),
                WarehouseArea(
                    floor_id=floor1.id,
                    area_code="A2",
                    area_name="A2正式库存区",
                    construction_status="enabled",
                ),
            ]
        )
        locations = [
            WarehouseLocation(
                location_code="FG-A01",
                location_name="成品A01",
                warehouse_type="finished",
                warehouse_floor=1,
                area_code="A2",
                storage_type="ground",
                placement_status="placed",
                is_active=True,
            ),
            WarehouseLocation(
                location_code="SI-A01",
                location_name="半成品A01",
                warehouse_type="semi_finished",
                warehouse_floor=1,
                area_code="A2",
                storage_type="ground",
                placement_status="placed",
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
            WarehouseLocation(
                location_code="SI-UNPLACED",
                location_name="待布局半成品库位",
                warehouse_type="semi_finished",
                placement_status="unplaced",
                is_active=True,
            ),
            WarehouseLocation(
                location_code="1FA",
                location_name="一楼 A1 原料暂存区",
                warehouse_type="shared",
                warehouse_floor=1,
                area_code="A1",
                storage_type="ground",
                placement_status="placed",
                is_active=True,
            ),
        ]
        session.add_all([product, *locations])
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")
    app.include_router(incoming_router, prefix="/api/incoming")

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
        assert draft.json()["items"][0]["location_id"] is None


def test_formal_replenishment_rejects_v11_locations_and_policies(
    stock_replenishment_app,
) -> None:
    app, _session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)

        locations = client.get("/api/requisition/stock-replenishment/locations")
        assert locations.status_code == 200, locations.text
        assert {row["location_code"] for row in locations.json()["items"]} == {
            "1FA",
            "FG-A01",
            "SI-A01",
        }

        unplaced_policy_payload = _semi_policy_payload()
        unplaced_policy_payload["default_location_id"] = 4
        rejected_unplaced_policy = client.post(
            "/api/requisition/stock-policies",
            json=unplaced_policy_payload,
        )
        assert rejected_unplaced_policy.status_code == 409
        assert "尚未完成平面图布局" in rejected_unplaced_policy.json()["detail"]

        ignored_unplaced_item = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "semi_finished",
                        "product_id": 1,
                        "customer_id": 1,
                        "material_code": "A416D",
                        "layer_count": 5,
                        "flute_type": "AB",
                        "report_length_mm": 1865,
                        "report_width_mm": 830,
                        "quantity": 1,
                        "location_id": 4,
                    }
                ],
            },
        )
        assert ignored_unplaced_item.status_code == 201
        assert "location_id" not in ignored_unplaced_item.json()["items"][0]

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

        ignored_v11_item = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "semi_finished",
                        "product_id": 1,
                        "customer_id": 1,
                        "material_code": "A416D",
                        "layer_count": 5,
                        "flute_type": "AB",
                        "report_length_mm": 1865,
                        "report_width_mm": 830,
                        "quantity": 1,
                        "location_id": 3,
                    }
                ],
            },
        )
        assert ignored_v11_item.status_code == 201, ignored_v11_item.text
        assert "location_id" not in ignored_v11_item.json()["items"][0]


def test_historical_replenishment_is_read_only_but_existing_order_can_close(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        policy = client.post(
            "/api/requisition/stock-policies",
            json=_semi_policy_payload(),
        ).json()
        retired_create = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "manual_history",
                "supplier_name": "佳丰",
                "customer_id": 1,
                "stock_now": False,
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
        assert retired_create.status_code == 409
        assert "历史采购检索" in retired_create.text

        direct_stock = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
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
        assert direct_stock.status_code == 400
        assert "不能保存后直接写入库存" in direct_stock.text

    from app.models.stock_replenishment import (
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )

    with session_factory() as session:
        legacy_order = StockReplenishmentOrder(
            order_number="REP-LEGACY-0001",
            supplier_name="佳丰",
            customer_id=1,
            source_type="manual_history",
            status="confirmed",
            created_by=1,
            confirmed_by=1,
        )
        legacy_order.items = [
            StockReplenishmentOrderItem(
                stock_policy_id=policy["id"],
                target_inventory_type="semi_finished",
                customer_id=1,
                product_name_snapshot="21301010 历史纸板",
                material_code_snapshot="A416D",
                normalized_material_code="A416D",
                layer_count=5,
                flute_type="AB",
                report_length_mm=1865,
                report_width_mm=830,
                quantity=30,
                location_id=2,
                historical_workbook="2025年采购单.xlsx",
                historical_sheet="2020.1-2026",
                historical_row=333,
                historical_search_text="21301010 116*66.5*16",
            )
        ]
        session.add(legacy_order)
        session.commit()
        legacy_order_id = legacy_order.id

    with TestClient(app) as client:
        _login(client)
        response = client.get(
            f"/api/requisition/stock-replenishment/orders/{legacy_order_id}"
        )
        assert response.status_code == 200, response.text
        order = response.json()
        assert order["status"] == "confirmed"
        assert order["stocked_quantity"] == 0
        assert order["items"][0]["inventory_lot"] is None
        assert order["items"][0]["historical_source"]["row"] == 333
        stocked = client.post(
            f"/api/requisition/stock-replenishment/orders/{legacy_order_id}/stock"
        )
        assert stocked.status_code == 200
        assert stocked.json()["status"] == "stocked"
        assert stocked.json()["stocked_quantity"] == 30
        lot_id = stocked.json()["items"][0]["inventory_lot"]["id"]

        repeated = client.post(
            f"/api/requisition/stock-replenishment/orders/{legacy_order_id}/stock"
        )
        assert repeated.status_code == 200
        assert repeated.json()["items"][0]["inventory_lot"]["id"] == lot_id

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


def test_stock_warning_finished_replenishment_cannot_write_inventory_directly(
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
        assert order_response.status_code == 400, order_response.text
        assert "不能保存后直接写入库存" in order_response.text


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
                "supplier_name": "佳丰",
                "stock_now": False,
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
        assert created_payload["status"] == "confirmed"
        stocked = client.post(
            f"/api/requisition/stock-replenishment/orders/{created_payload['id']}/stock"
        )
        assert stocked.status_code == 409, stocked.text
        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        received = client.put(
            f"/api/incoming/receive/sr{item['id']}",
            json={
                "received_quantity": 30,
                "idempotency_key": "test-common-box-incoming",
            },
        )
        assert received.status_code == 200, received.text

    from app.models.warehouse_inventory import SemiFinishedInventoryDetail

    with session_factory() as session:
        detail = session.scalar(select(SemiFinishedInventoryDetail))
        assert detail is not None
        assert detail.material_id == 1
        assert detail.material_code_snapshot == "A416D"


def test_new_replenishment_cannot_create_finished_inventory_directly(
    stock_replenishment_app,
) -> None:
    app, _session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "finished",
                        "customer_id": 1,
                        "product_id": 1,
                        "quantity": 30,
                        "location_id": 1,
                    }
                ],
            },
        )
    assert response.status_code == 400, response.text
    assert "只能进入客户专用纸板备料" in response.text


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


def test_manual_replenishment_idempotency_replays_the_same_draft_once(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    payload = {
        "source_type": "customer_request",
        "idempotency_key": "manual-replenishment-same-draft",
        "supplier_name": "佳丰",
        "customer_id": 1,
        "stock_now": False,
        "items": [
            {
                "target_inventory_type": "semi_finished",
                "product_id": 1,
                "customer_id": 1,
                "product_code": "21301010",
                "product_name": "天华测试外箱",
                "material_id": 1,
                "material_code": "A416D",
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
    }
    with TestClient(app) as client:
        _login(client)
        first = client.post(
            "/api/requisition/stock-replenishment/orders", json=payload
        )
        replay = client.post(
            "/api/requisition/stock-replenishment/orders", json=payload
        )

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["order_number"] == first.json()["order_number"]

    from app.models.stock_replenishment import StockReplenishmentOrder

    with session_factory() as session:
        assert session.scalar(select(func.count(StockReplenishmentOrder.id))) == 1


def test_manual_replenishment_unique_conflict_returns_concurrent_draft(
    stock_replenishment_app,
) -> None:
    from app.api.deps import get_db
    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.core.time_contract import beijing_today

    app, session_factory = stock_replenishment_app
    key = "manual-replenishment-concurrent-draft"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20].upper()
    expected_number = f"CBR-{beijing_today():%Y%m%d}-{digest}"
    request_session = session_factory()
    original_flush = request_session.flush
    injected = False

    def flush_with_concurrent_winner(objects=None):
        nonlocal injected
        has_replenishment = any(
            isinstance(row, StockReplenishmentOrder) for row in request_session.new
        )
        if has_replenishment and not injected:
            injected = True
            with session_factory() as concurrent:
                concurrent.add(
                    StockReplenishmentOrder(
                        order_number=expected_number,
                        supplier_name="苏州佳丰",
                        customer_id=1,
                        source_type="customer_request",
                        status="confirmed",
                        created_by=1,
                        confirmed_by=1,
                    )
                )
                concurrent.commit()
            raise IntegrityError(
                "INSERT stock_replenishment_orders",
                {},
                RuntimeError("unique order_number"),
            )
        return original_flush(objects)

    request_session.flush = flush_with_concurrent_winner  # type: ignore[method-assign]

    def override_get_db():
        yield request_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            _login(client)
            response = client.post(
                "/api/requisition/stock-replenishment/orders",
                json={
                    "source_type": "customer_request",
                    "idempotency_key": key,
                    "supplier_name": "佳丰",
                    "customer_id": 1,
                    "stock_now": False,
                    "items": [
                        {
                            "target_inventory_type": "semi_finished",
                            "product_id": 1,
                            "customer_id": 1,
                            "material_id": 1,
                            "material_code": "A416D",
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
    finally:
        request_session.close()

    assert injected is True
    assert response.status_code == 201, response.text
    assert response.json()["order_number"] == expected_number
    with session_factory() as session:
        assert session.scalar(select(func.count(StockReplenishmentOrder.id))) == 1


def _customer_replenishment_payload(quantity: int = 30) -> dict:
    return {
        "source_type": "customer_request",
        "supplier_name": "苏州佳丰",
        "customer_id": 1,
        "stock_now": False,
        "items": [
            {
                "target_inventory_type": "semi_finished",
                "customer_id": 1,
                "product_id": 1,
                "material_id": 1,
                "material_code": "A416D",
                "layer_count": 5,
                "flute_type": "AB",
                "report_length_mm": 1865,
                "report_width_mm": 830,
                "crease_type": "压线",
                "crease_left_mm": 335,
                "crease_middle_mm": 160,
                "crease_right_mm": 335,
                "sheet_type": "creased_sheet",
                "quantity": quantity,
                "location_id": 2,
            }
        ],
    }


def test_replenishment_auto_stages_material_without_location_choice(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryMovement,
        WarehouseLocation,
    )

    payload = _customer_replenishment_payload(quantity=100)
    payload["items"][0]["location_id"] = None

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=payload,
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]

        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        row = next(
            item
            for item in pending.json()["items"]
            if item["item_id"] == f"sr{item_id}"
        )
        assert "default_location_id" not in row
        assert "target_inventory_type" not in row

        locations = client.get("/api/incoming/replenishment-locations")
        assert locations.status_code == 404, locations.text

        received = client.put(
            f"/api/incoming/receive/sr{item_id}",
            json={
                "received_quantity": 100,
                "idempotency_key": "replenishment-arrival-auto-staging",
            },
        )
        assert received.status_code == 200, received.text
        repeated = client.put(
            f"/api/incoming/receive/sr{item_id}",
            json={
                "received_quantity": 100,
                "idempotency_key": "replenishment-arrival-auto-staging",
            },
        )
        assert repeated.status_code == 200, repeated.text

    with session_factory() as session:
        lot = session.scalar(select(InventoryLot))
        assert lot is not None
        staging = session.scalar(
            select(WarehouseLocation).where(WarehouseLocation.location_code == "1FA")
        )
        assert staging is not None
        assert lot.warehouse_location_id == staging.id
        assert session.scalar(select(func.count(InventoryLot.id))) == 1
        assert session.scalar(select(func.count(InventoryMovement.id))) == 1
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 1


def test_replenishment_receive_fails_closed_without_floor1_a1_staging(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation

    with session_factory() as session:
        staging = session.scalar(
            select(WarehouseLocation).where(WarehouseLocation.location_code == "1FA")
        )
        assert staging is not None
        staging.is_active = False
        session.commit()

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_customer_replenishment_payload(quantity=10),
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]
        received = client.put(
            f"/api/incoming/receive/sr{item_id}",
            json={
                "received_quantity": 10,
                "idempotency_key": "replenishment-no-floor1-a1-staging",
            },
        )
        assert received.status_code == 409, received.text
        assert "一楼 A1 原料暂存" in received.json()["detail"]
        assert "待送区" in received.json()["detail"]

    with session_factory() as session:
        assert session.scalar(select(func.count(InventoryLot.id))) == 0
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 0


def test_replenishment_stays_reported_routes_to_incoming_and_voids_only_before_receipt(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        InventoryMovement,
        SemiFinishedInventoryDetail,
    )

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_customer_replenishment_payload(),
        )
        assert created.status_code == 201, created.text
        order = created.json()
        item = order["items"][0]

        reported = client.get("/api/requisition/reported-documents")
        reported_row = next(
            row
            for row in reported.json()["items"]
            if row["document_number"] == order["order_number"]
        )
        assert reported_row["incoming_status"] == "待入库"
        assert reported_row["can_void"] is True

        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        pending_row = next(
            row
            for row in pending.json()["items"]
            if row["item_id"] == f"sr{item['id']}"
        )
        assert pending_row["source_type"] == "stock_replenishment"
        assert pending_row["incoming_quantity"] == 30
        assert pending_row["can_revert_receipt"] is False

        with session_factory() as session:
            assert session.scalar(select(func.count(InventoryLot.id))) == 0
            assert session.scalar(select(func.count(InventoryMovement.id))) == 0
            assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 0

        direct_stock = client.post(
            f"/api/requisition/stock-replenishment/orders/{order['id']}/stock"
        )
        assert direct_stock.status_code == 409
        assert "来料入库" in direct_stock.json()["detail"]

        received = client.put(
            f"/api/incoming/receive/sr{item['id']}",
            json={
                "received_quantity": 30,
                "idempotency_key": "test-replenishment-incoming-1",
            },
        )
        assert received.status_code == 200, received.text
        assert received.json()["received_inventory_lot_id"] is not None
        repeated = client.put(
            f"/api/incoming/receive/sr{item['id']}",
            json={
                "received_quantity": 30,
                "idempotency_key": "test-replenishment-incoming-1",
            },
        )
        assert repeated.status_code == 200, repeated.text
        assert (
            repeated.json()["received_inventory_lot_id"]
            == received.json()["received_inventory_lot_id"]
        )

        after_pending = client.get("/api/incoming/pending").json()["items"]
        assert all(row["item_id"] != f"sr{item['id']}" for row in after_pending)
        after_reported = client.get("/api/requisition/reported-documents").json()[
            "items"
        ]
        after_row = next(
            row
            for row in after_reported
            if row["document_number"] == order["order_number"]
        )
        assert after_row["incoming_status"] == "已入库"
        assert after_row["can_void"] is False
        blocked_void = client.put(
            f"/api/requisition/stock-replenishment/orders/{order['id']}/void"
        )
        assert blocked_void.status_code == 409

        second = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_customer_replenishment_payload(quantity=20),
        )
        assert second.status_code == 201, second.text
        second_order = second.json()
        second_item_id = second_order["items"][0]["id"]
        voided = client.put(
            f"/api/requisition/stock-replenishment/orders/{second_order['id']}/void"
        )
        assert voided.status_code == 200, voided.text
        assert voided.json()["status"] == "voided"
        after_void_pending = client.get("/api/incoming/pending").json()["items"]
        assert all(
            row["item_id"] != f"sr{second_item_id}" for row in after_void_pending
        )
        blocked_receipt = client.put(
            f"/api/incoming/receive/sr{second_item_id}",
            json={
                "received_quantity": 20,
                "idempotency_key": "test-replenishment-void-before-receipt",
            },
        )
        assert blocked_receipt.status_code == 409, blocked_receipt.text
        assert "已作废" in blocked_receipt.json()["detail"]

    with session_factory() as session:
        from app.models.audit import OperationLog

        assert session.scalar(select(func.count(InventoryLot.id))) == 1
        assert session.scalar(select(func.count(InventoryMovement.id))) == 1
        assert (
            session.scalar(
                select(func.count()).select_from(SemiFinishedInventoryDetail)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(FinishedGoodsInventoryDetail)
            )
            == 0
        )
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 1
        lot = session.scalar(select(InventoryLot))
        assert lot is not None
        assert lot.inventory_type == "semi_finished"
        assert lot.source_ref_type == "stock_replenishment_receipt"
        assert (
            session.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "VOID_STOCK_REPLENISHMENT"
                )
            )
            == 1
        )


def test_repeated_replenishment_void_is_idempotent_with_one_audit_log(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_customer_replenishment_payload(quantity=20),
        )
        assert created.status_code == 201, created.text
        order = created.json()
        first = client.put(
            f"/api/requisition/stock-replenishment/orders/{order['id']}/void"
        )
        replay = client.put(
            f"/api/requisition/stock-replenishment/orders/{order['id']}/void"
        )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["status"] == "voided"

    from app.models.audit import OperationLog

    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "VOID_STOCK_REPLENISHMENT"
                )
            )
            == 1
        )


def test_concurrent_voids_and_receipt_leave_one_legal_final_state(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_customer_replenishment_payload(quantity=20),
        )
        assert created.status_code == 201, created.text
        order = created.json()
        item_id = order["items"][0]["id"]

    barrier = Barrier(3)

    def void_once() -> tuple[int, dict]:
        with TestClient(app) as client:
            _login(client)
            barrier.wait(timeout=10)
            response = client.put(
                f"/api/requisition/stock-replenishment/orders/{order['id']}/void"
            )
            return response.status_code, response.json()

    def receive_once() -> tuple[int, dict]:
        with TestClient(app) as client:
            _login(client)
            barrier.wait(timeout=10)
            response = client.put(
                f"/api/incoming/receive/sr{item_id}",
                json={
                    "received_quantity": 20,
                    "idempotency_key": "test-replenishment-concurrent-receipt",
                },
            )
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=3) as executor:
        first_void = executor.submit(void_once)
        second_void = executor.submit(void_once)
        receipt = executor.submit(receive_once)
        void_results = [first_void.result(timeout=30), second_void.result(timeout=30)]
        receipt_result = receipt.result(timeout=30)

    from app.models.audit import OperationLog
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement

    with session_factory() as session:
        stored = session.get(StockReplenishmentOrder, order["id"])
        assert stored is not None
        void_log_count = int(
            session.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "VOID_STOCK_REPLENISHMENT",
                    OperationLog.entity_id == order["id"],
                )
            )
            or 0
        )
        receipt_count = int(
            session.scalar(
                select(func.count(IncomingReceiptItem.id)).where(
                    IncomingReceiptItem.stock_replenishment_item_id == item_id
                )
            )
            or 0
        )
        lot_count = int(session.scalar(select(func.count(InventoryLot.id))) or 0)
        movement_count = int(
            session.scalar(select(func.count(InventoryMovement.id))) or 0
        )

    if receipt_result[0] == 200:
        assert [status for status, _payload in void_results] == [409, 409]
        assert stored.status == "stocked"
        assert void_log_count == 0
        assert (receipt_count, lot_count, movement_count) == (1, 1, 1)
    else:
        assert receipt_result[0] == 409, receipt_result
        assert [status for status, _payload in void_results] == [200, 200]
        assert stored.status == "voided"
        assert void_log_count == 1
        assert (receipt_count, lot_count, movement_count) == (0, 0, 0)
