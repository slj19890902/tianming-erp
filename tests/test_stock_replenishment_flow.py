from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
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
def stock_replenishment_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
        Floor3LocationLayout,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseGroundLayoutPlan,
        WarehouseGroundLayoutSlot,
        WarehouseLocation,
    )
    from app.services.supplier_master import normalize_supplier_identity

    def published_floor_identity(floor_number):
        if int(floor_number) == 3:
            return {
                "revision": "stock-replenishment-map-floor3-v1",
                "zones_by_id": {
                    "stock-replenishment-zone-raw-001": "RAW-001",
                    "stock-replenishment-zone-fg-004": "FG-004",
                    "stock-replenishment-zone-f34": "F34",
                    "stock-replenishment-zone-f12": "F12",
                },
                "zone_ids_by_area": {
                    "RAW-001": ("stock-replenishment-zone-raw-001",),
                    "FG-004": ("stock-replenishment-zone-fg-004",),
                    "F34": ("stock-replenishment-zone-f34",),
                    "F12": ("stock-replenishment-zone-f12",),
                },
            }
        return {
            "revision": "stock-replenishment-map-v1",
            "zones_by_id": {"stock-replenishment-zone-a1": "A1"},
            "zone_ids_by_area": {"A1": ("stock-replenishment-zone-a1",)},
        }

    monkeypatch.setattr(
        "app.services.location_candidates.load_warehouse_twin_published_floor_identity",
        published_floor_identity,
    )

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
            quote_price=Decimal("2.80"),
            price_unit="元/㎡",
            purchase_currency="CNY",
            purchase_tax_included=True,
            purchase_tax_rate=Decimal("0.13"),
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
        liner_product = Product(
            customer_id=customer.id,
            product_code="LINER-001",
            customer_material_code="TH-LINER-001",
            product_name="天华测试衬板",
            material_id=material.id,
            legacy_material_text="A416D/AB",
            length_mm=Decimal("778"),
            width_mm=Decimal("1137"),
            box_style="衬板",
            box_category="normal",
            flute_type="AB",
            layer_count=5,
            report_length_mm=1137,
            report_width_mm=778,
            crease_type="净料",
        )
        floor1 = WarehouseFloor(
            floor_code="F1",
            floor_name="一楼",
            floor_number=1,
            construction_status="enabled",
        )
        session.add(floor1)
        session.flush()
        area_a1 = WarehouseArea(
            floor_id=floor1.id,
            area_code="A1",
            area_name="A1原料暂存区",
            construction_status="enabled",
        )
        area_a2 = WarehouseArea(
            floor_id=floor1.id,
            area_code="A2",
            area_name="A2正式库存区",
            construction_status="enabled",
        )
        floor3 = WarehouseFloor(
            floor_code="F3",
            floor_name="三楼",
            floor_number=3,
            construction_status="enabled",
        )
        session.add(floor3)
        session.flush()
        area_raw_001 = WarehouseArea(
            floor_id=floor3.id,
            area_code="RAW-001",
            area_name="三楼左区原料备料区",
            construction_status="enabled",
        )
        area_fg_004 = WarehouseArea(
            floor_id=floor3.id,
            area_code="FG-004",
            area_name="三楼左区成品区",
            construction_status="enabled",
        )
        area_f34 = WarehouseArea(
            floor_id=floor3.id,
            area_code="F34",
            area_name="F3/F4之间临时周转区",
            planned_location_count=3,
            planned_pallet_capacity=3,
            construction_status="enabled",
            capacity_review_status="confirmed",
            capacity_eligible=True,
            confirmed_pallet_capacity=3,
            capacity_reviewed_by="admin",
            capacity_reviewed_at=datetime(2026, 9, 1, 8, 0, 0),
        )
        area_f12 = WarehouseArea(
            floor_id=floor3.id,
            area_code="F12",
            area_name="F1/F2之间临时周转区",
            planned_location_count=8,
            planned_pallet_capacity=8,
            construction_status="enabled",
            capacity_review_status="confirmed",
            capacity_eligible=True,
            confirmed_pallet_capacity=8,
            capacity_reviewed_by="admin",
            capacity_reviewed_at=datetime(2026, 9, 1, 8, 0, 0),
        )
        session.add_all(
            [area_a1, area_a2, area_raw_001, area_fg_004, area_f34, area_f12]
        )
        session.flush()
        session.add(
            WarehouseAreaStoragePolicy(
                area_id=area_a1.id,
                map_feature_id="stock-replenishment-zone-a1",
                allowed_inventory_types_json='["semi_finished","shared"]',
                storage_layout="pallet_ground",
                status="published",
                published_map_revision="stock-replenishment-map-v1",
                version=1,
            )
        )
        session.add_all(
            [
                WarehouseAreaStoragePolicy(
                    area_id=area_raw_001.id,
                    map_feature_id="stock-replenishment-zone-raw-001",
                    allowed_inventory_types_json='["raw_material","semi_finished","shared"]',
                    storage_layout="pallet_ground",
                    status="published",
                    published_map_revision="stock-replenishment-map-floor3-v1",
                    version=1,
                ),
                WarehouseAreaStoragePolicy(
                    area_id=area_fg_004.id,
                    map_feature_id="stock-replenishment-zone-fg-004",
                    allowed_inventory_types_json='["finished","shared"]',
                    storage_layout="pallet_ground",
                    status="published",
                    published_map_revision="stock-replenishment-map-floor3-v1",
                    version=1,
                ),
                WarehouseAreaStoragePolicy(
                    area_id=area_f34.id,
                    map_feature_id="stock-replenishment-zone-f34",
                    allowed_inventory_types_json='["finished"]',
                    storage_layout="pallet_ground",
                    status="published",
                    published_map_revision="stock-replenishment-map-floor3-v1",
                    version=1,
                ),
                WarehouseAreaStoragePolicy(
                    area_id=area_f12.id,
                    map_feature_id="stock-replenishment-zone-f12",
                    allowed_inventory_types_json='["finished"]',
                    storage_layout="pallet_ground",
                    status="published",
                    published_map_revision="stock-replenishment-map-floor3-v1",
                    version=1,
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
                source_version="TWIN_V1",
                is_active=True,
            ),
            WarehouseLocation(
                location_code="F3-RAW-001-R01-L01-G01",
                location_name="三楼左区原料备料位",
                warehouse_type="shared",
                warehouse_floor=3,
                area_code="RAW-001",
                storage_type="ground",
                placement_status="placed",
                source_version="CURRENT_MAP",
                is_active=True,
            ),
            WarehouseLocation(
                location_code="F3-FG-004-R01-L01-G01",
                location_name="三楼左区成品位",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="FG-004",
                storage_type="ground",
                placement_status="placed",
                source_version="CURRENT_MAP",
                is_active=True,
            ),
        ]
        floor3_raw = locations[-2]
        floor3_finished = locations[-1]
        temporary_locations = [
            WarehouseLocation(
                location_code=f"{area_code}-P{number:02d}",
                location_name=f"{area_code} 临放第{number}位",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code=area_code,
                storage_type="temporary_aisle",
                placement_status="placed",
                source_version="CURRENT_MAP",
                is_temporary=True,
                is_active=True,
                address_kind="functional",
                address_area_id=area.id,
                sort_order=1000 + offset + number,
            )
            for area_code, area, count, offset in (
                ("F34", area_f34, 3, 0),
                ("F12", area_f12, 8, 100),
            )
            for number in range(1, count + 1)
        ]
        locations.extend(temporary_locations)
        session.add_all([product, liner_product, *locations])
        session.flush()
        staging = locations[4]
        session.add(
            Floor3LocationLayout(
                location_id=staging.id,
                left_pct=Decimal("10"),
                top_pct=Decimal("10"),
                width_pct=Decimal("8"),
                height_pct=Decimal("8"),
                source_type="seeded",
            )
        )
        session.add_all(
            [
                Floor3LocationLayout(
                    location_id=floor3_raw.id,
                    left_pct=Decimal("15"),
                    top_pct=Decimal("15"),
                    width_pct=Decimal("8"),
                    height_pct=Decimal("8"),
                    source_type="seeded",
                ),
                Floor3LocationLayout(
                    location_id=floor3_finished.id,
                    left_pct=Decimal("25"),
                    top_pct=Decimal("15"),
                    width_pct=Decimal("8"),
                    height_pct=Decimal("8"),
                    source_type="seeded",
                ),
            ]
        )
        session.add_all(
            [
                Floor3LocationLayout(
                    location_id=location.id,
                    left_pct=Decimal("40") + Decimal(index % 4),
                    top_pct=Decimal("15") + Decimal(index // 4),
                    width_pct=Decimal("1"),
                    height_pct=Decimal("1"),
                    source_type="manual",
                    layout_kind="logical_anchor",
                )
                for index, location in enumerate(temporary_locations)
            ]
        )
        ground_plan = WarehouseGroundLayoutPlan(
            area_id=area_a1.id,
            status="published",
            target_slot_count=1,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision="stock-replenishment-map-v1",
            published_map_revision="stock-replenishment-map-v1",
            preview_fingerprint="a" * 64,
            version=1,
            publish_idempotency_key="stock-replenishment-ground-v1",
            publish_request_hash="b" * 64,
            updated_by=user.id,
            published_by=user.id,
            published_at=datetime.now(),
        )
        session.add(ground_plan)
        session.flush()
        session.add(
            WarehouseGroundLayoutSlot(
                plan_id=ground_plan.id,
                location_id=staging.id,
                route_sequence=1,
                row_no=1,
                slot_no=1,
                x_mm=Decimal("1000"),
                y_mm=Decimal("1000"),
                width_mm=1200,
                depth_mm=1000,
            )
        )
        for area, location, idempotency_key, slot_no in (
            (area_raw_001, floor3_raw, "stock-replenishment-floor3-raw-v1", 1),
            (area_fg_004, floor3_finished, "stock-replenishment-floor3-finished-v1", 2),
        ):
            plan = WarehouseGroundLayoutPlan(
                area_id=area.id,
                status="published",
                target_slot_count=1,
                numbering_origin="south",
                row_direction="from_aisle_inward",
                slot_direction="left_to_right",
                row_start_no=1,
                slot_start_no=1,
                draft_map_revision="stock-replenishment-map-floor3-v1",
                published_map_revision="stock-replenishment-map-floor3-v1",
                preview_fingerprint=str(slot_no) * 64,
                version=1,
                publish_idempotency_key=idempotency_key,
                publish_request_hash=str(slot_no + 2) * 64,
                updated_by=user.id,
                published_by=user.id,
                published_at=datetime.now(),
            )
            session.add(plan)
            session.flush()
            session.add(
                WarehouseGroundLayoutSlot(
                    plan_id=plan.id,
                    location_id=location.id,
                    route_sequence=1,
                    row_no=1,
                    slot_no=1,
                    x_mm=Decimal("1500") + Decimal(slot_no * 1000),
                    y_mm=Decimal("1500"),
                    width_mm=1200,
                    depth_mm=1000,
                )
            )
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
            *(f"F12-P{number:02d}" for number in range(1, 9)),
            *(f"F34-P{number:02d}" for number in range(1, 4)),
            "F3-FG-004-R01-L01-G01",
            "F3-RAW-001-R01-L01-G01",
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
        assert item["product_id"] is None
        assert item["reference_product_id"] == 1
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


def test_non_liner_replenishment_cannot_create_finished_inventory_directly(
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
    assert response.status_code == 409, response.text
    assert "只有正式识别为衬板" in response.text


def test_liner_replenishment_receives_directly_into_floor3_temporary_turnover(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        products = client.get(
            "/api/requisition/stock-replenishment/products",
            params={"customer_id": 1, "q": "LINER-001"},
        )
        assert products.status_code == 200, products.text
        liner = products.json()["items"][0]
        assert liner["box_type_code"] == "liner"

        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "supplier_name": "佳丰",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "finished",
                        "customer_id": 1,
                        "reference_product_id": liner["id"],
                        "quantity": 32,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        item = created.json()["items"][0]
        assert item["product_id"] == liner["id"]
        assert item["reference_product_id"] == liner["id"]

        # The already-created internal replenishment line is the stable routing
        # fact. A later common-box master change must not divert it into the
        # external-packaging receipt path.
        from app.models.product import Product

        with session_factory() as session:
            product = session.get(Product, liner["id"])
            assert product is not None
            product.box_style = "A1"
            product.supply_mode = "external_purchase"
            product.external_packaging_category_code = "other_packaging"
            product.external_packaging_specification_json = "{}"
            product.external_packaging_specification_summary = (
                "临时主档切换，仅验证既有内部采购路线不漂移"
            )
            product.external_packaging_purchase_unit = "片"
            product.external_packaging_candidate_snapshot_json = "[]"
            product.external_packaging_default_order_quantity_basis = None
            product.external_packaging_default_purchase_quantity_basis = None
            from app.models.stock_replenishment import StockReplenishmentOrderItem

            frozen_item = session.get(StockReplenishmentOrderItem, item["id"])
            assert frozen_item is not None
            assert frozen_item.procurement_route_snapshot == "paperboard"
            session.commit()

        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        assert any(row["item_id"] == f"sr{item['id']}" for row in pending.json()["items"])

        received = client.put(
            f"/api/incoming/receive/sr{item['id']}",
            json={
                "received_quantity": 32,
                "idempotency_key": "liner-direct-finished-receipt",
            },
        )
        assert received.status_code == 200, received.text
        repeated = client.put(
            f"/api/incoming/receive/sr{item['id']}",
            json={
                "received_quantity": 32,
                "idempotency_key": "liner-direct-finished-receipt",
            },
        )
        assert repeated.status_code == 200, repeated.text
        different_key = client.put(
            f"/api/incoming/receive/sr{item['id']}",
            json={
                "received_quantity": 32,
                "idempotency_key": "liner-direct-finished-receipt-second-key",
            },
        )
        assert different_key.status_code == 409, different_key.text

    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.material import Material
    from app.models.product import Product
    from app.models.supplier import Supplier
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        InventoryPallet,
        WarehouseGroundOccupancy,
        WarehouseLocation,
    )

    with session_factory() as session:
        row = session.execute(
            select(InventoryLot, FinishedGoodsInventoryDetail, WarehouseLocation)
            .join(FinishedGoodsInventoryDetail)
            .join(WarehouseLocation)
            .where(FinishedGoodsInventoryDetail.product_id == liner["id"])
        ).one()
        lot, detail, location = row
        assert lot.quantity_available == 32
        assert detail.inventory_code_snapshot == "LINER-001"
        assert location.warehouse_floor == 3
        assert location.area_code == "F34"
        assert location.location_code == "F34-P01"
        pallet = session.scalar(
            select(InventoryPallet).where(
                InventoryPallet.location_id == location.id,
                InventoryPallet.is_current.is_(True),
            )
        )
        assert pallet is not None
        assert pallet.needs_relocation is True
        assert session.scalar(select(func.count(WarehouseGroundOccupancy.id))) == 0
        assert session.scalar(select(func.count(InventoryLot.id))) == 1
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 1
        assert session.scalar(
            select(func.count(SupplierReceiptSettlementPriceFact.id))
        ) == 1

        from app.services.supplier_monthly_settlement import _scan_paperboard

        before, before_issues = _scan_paperboard(
            session,
            start_utc=datetime(2026, 1, 1),
            end_utc=datetime(2027, 1, 1),
        )
        assert before_issues == []
        assert len(before) == 1

        product = session.get(Product, liner["id"])
        assert product is not None and product.material_id is not None
        material = session.get(Material, product.material_id)
        supplier = session.scalar(
            select(Supplier).where(Supplier.standard_name == "苏州佳丰")
        )
        assert material is not None and supplier is not None
        product.box_style = "A1"
        material.quote_price = Decimal("999.0000")
        supplier.display_name = "主档后改供应商"
        session.commit()

        after, after_issues = _scan_paperboard(
            session,
            start_utc=datetime(2026, 1, 1),
            end_utc=datetime(2027, 1, 1),
        )
        assert after_issues == []
        assert len(after) == 1
        assert after[0].supplier_receipt_price_fact_id == before[0].supplier_receipt_price_fact_id
        assert after[0].supplier_name == before[0].supplier_name
        assert after[0].material_or_product_snapshot == before[0].material_or_product_snapshot
        assert after[0].erp_amount == before[0].erp_amount


def test_legacy_finished_replenishment_without_frozen_route_fails_before_receipt(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceipt
    from app.models.stock_replenishment import (
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.models.warehouse_inventory import InventoryLot

    with session_factory() as session:
        order = StockReplenishmentOrder(
            order_number="SR-LEGACY-FINISHED-NO-ROUTE",
            supplier_name="佳丰",
            customer_id=1,
            source_type="customer_request",
            status="confirmed",
            created_by=1,
            confirmed_by=1,
        )
        order.items = [
            StockReplenishmentOrderItem(
                target_inventory_type="finished",
                procurement_route_snapshot=None,
                product_id=1,
                reference_product_id=1,
                customer_id=1,
                product_code_snapshot="21301010",
                product_name_snapshot="旧非衬板成品补库",
                quantity=10,
                location_id=1,
            )
        ]
        session.add(order)
        session.commit()
        item_id = int(order.items[0].id)

    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        assert any(row["item_id"] == f"sr{item_id}" for row in pending.json()["items"])
        blocked = client.put(
            f"/api/incoming/receive/sr{item_id}",
            json={
                "received_quantity": 10,
                "idempotency_key": "legacy-finished-no-route-must-fail",
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["code"] == (
            "STOCK_REPLENISHMENT_PROCUREMENT_ROUTE_MISSING"
        )

    with session_factory() as session:
        assert session.scalar(select(func.count(IncomingReceipt.id))) == 0
        assert session.scalar(select(func.count(InventoryLot.id))) == 0
        assert session.scalar(
            select(func.count(SupplierReceiptSettlementPriceFact.id))
        ) == 0


def test_liner_stock_warning_can_create_draft_and_receive_as_finished(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        products = client.get(
            "/api/requisition/stock-replenishment/products",
            params={"customer_id": 1, "q": "LINER-001"},
        )
        assert products.status_code == 200, products.text
        liner = products.json()["items"][0]

        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "stock_warning",
                "idempotency_key": "liner-stock-warning-draft-v1",
                "supplier_name": "佳丰",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "finished",
                        "customer_id": 1,
                        "reference_product_id": liner["id"],
                        "quantity": 9,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        item = created.json()["items"][0]
        assert item["target_inventory_type"] == "finished"
        assert item["product_id"] == liner["id"]

        received = client.put(
            f"/api/incoming/receive/sr{item['id']}",
            json={
                "received_quantity": 9,
                "idempotency_key": "liner-stock-warning-receipt-v1",
            },
        )
        assert received.status_code == 200, received.text

    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        WarehouseLocation,
    )

    with session_factory() as session:
        lot, detail, location = session.execute(
            select(InventoryLot, FinishedGoodsInventoryDetail, WarehouseLocation)
            .join(FinishedGoodsInventoryDetail)
            .join(WarehouseLocation)
            .where(FinishedGoodsInventoryDetail.product_id == liner["id"])
        ).one()
        assert lot.quantity_available == 9
        assert detail.inventory_code_snapshot == "LINER-001"
        assert location.warehouse_floor == 3
        assert location.area_code == "F34"
        assert location.location_code == "F34-P01"


def test_liner_replenishment_skips_occupied_f34_anchor_in_order(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.warehouse_inventory import InventoryPallet, WarehouseLocation

    with session_factory() as session:
        first = session.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "F34-P01"
            )
        )
        assert first is not None
        session.add(
            InventoryPallet(
                pallet_code="P0-33-OCCUPIED-F34-01",
                location_id=first.id,
                status="active",
                is_current=True,
                needs_relocation=True,
                version=1,
            )
        )
        session.commit()

    with TestClient(app) as client:
        _login(client)
        products = client.get(
            "/api/requisition/stock-replenishment/products",
            params={"customer_id": 1, "q": "LINER-001"},
        )
        liner = products.json()["items"][0]
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "supplier_name": "佳丰",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "finished",
                        "customer_id": 1,
                        "reference_product_id": liner["id"],
                        "quantity": 12,
                    }
                ],
            },
        )
        item = created.json()["items"][0]
        received = client.put(
            f"/api/incoming/receive/sr{item['id']}",
            json={
                "received_quantity": 12,
                "idempotency_key": "liner-skip-occupied-f34",
            },
        )
        assert received.status_code == 200, received.text

    with session_factory() as session:
        location = session.scalar(
            select(WarehouseLocation)
            .join(InventoryPallet)
            .where(InventoryPallet.pallet_code != "P0-33-OCCUPIED-F34-01")
        )
        assert location is not None
        assert location.location_code == "F34-P02"


def test_liner_replenishment_uses_f12_after_all_f34_anchors_are_occupied(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.warehouse_inventory import InventoryPallet, WarehouseLocation

    with session_factory() as session:
        f34_locations = list(
            session.scalars(
                select(WarehouseLocation)
                .where(WarehouseLocation.area_code == "F34")
                .order_by(WarehouseLocation.location_code)
            ).all()
        )
        assert len(f34_locations) == 3
        session.add_all(
            [
                InventoryPallet(
                    pallet_code=f"P0-33-OCCUPIED-F34-{index}",
                    location_id=location.id,
                    status="active",
                    is_current=True,
                    needs_relocation=True,
                    version=1,
                )
                for index, location in enumerate(f34_locations, start=1)
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client)
        products = client.get(
            "/api/requisition/stock-replenishment/products",
            params={"customer_id": 1, "q": "LINER-001"},
        )
        liner = products.json()["items"][0]
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "supplier_name": "佳丰",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "finished",
                        "customer_id": 1,
                        "reference_product_id": liner["id"],
                        "quantity": 12,
                    }
                ],
            },
        )
        item = created.json()["items"][0]
        received = client.put(
            f"/api/incoming/receive/sr{item['id']}",
            json={
                "received_quantity": 12,
                "idempotency_key": "liner-f34-full-use-f12",
            },
        )
        assert received.status_code == 200, received.text

    with session_factory() as session:
        location = session.scalar(
            select(WarehouseLocation)
            .join(InventoryPallet)
            .where(InventoryPallet.pallet_code.not_like("P0-33-OCCUPIED-%"))
        )
        assert location is not None
        assert location.location_code == "F12-P01"


def test_liner_replenishment_fails_atomically_when_f34_and_f12_are_full(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryPallet,
        WarehouseLocation,
    )

    with session_factory() as session:
        temporary_locations = list(
            session.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.area_code.in_(("F34", "F12"))
                )
            ).all()
        )
        assert len(temporary_locations) == 11
        session.add_all(
            [
                InventoryPallet(
                    pallet_code=f"P0-33-FULL-{index:02d}",
                    location_id=location.id,
                    status="active",
                    is_current=True,
                    needs_relocation=True,
                    version=1,
                )
                for index, location in enumerate(temporary_locations, start=1)
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client)
        products = client.get(
            "/api/requisition/stock-replenishment/products",
            params={"customer_id": 1, "q": "LINER-001"},
        )
        liner = products.json()["items"][0]
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "supplier_name": "佳丰",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "finished",
                        "customer_id": 1,
                        "reference_product_id": liner["id"],
                        "quantity": 12,
                    }
                ],
            },
        )
        item = created.json()["items"][0]
        received = client.put(
            f"/api/incoming/receive/sr{item['id']}",
            json={
                "received_quantity": 12,
                "idempotency_key": "liner-temporary-turnover-full",
            },
        )
        assert received.status_code == 409, received.text
        assert "F34/F12" in received.text
        assert "归位" in received.text

    with session_factory() as session:
        assert session.scalar(select(func.count(InventoryLot.id))) == 0
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 0


def test_replenishment_rejects_incomplete_or_mismatched_crease(
    stock_replenishment_app,
) -> None:
    app, _session_factory = stock_replenishment_app
    base = {
        "target_inventory_type": "semi_finished",
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
                        "customer_id": 1,
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
                        "customer_id": 1,
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


def test_manual_replenishment_accepts_reference_only_and_freezes_internal_name(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.stock_replenishment import StockReplenishmentOrderItem

    payload = _customer_replenishment_payload(quantity=12)
    line = payload["items"][0]
    line["reference_product_id"] = line.pop("product_id")
    line["internal_name"] = "TH 1865x830 customer generic board"

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=payload,
        )
    assert created.status_code == 201, created.text
    row = created.json()["items"][0]
    assert row["product_id"] is None
    assert row["reference_product_id"] == 1
    assert row["internal_name"] == "TH 1865x830 customer generic board"

    with session_factory() as session:
        item = session.scalar(select(StockReplenishmentOrderItem))
        assert item is not None
        assert item.product_id is None
        assert item.reference_product_id == 1
        assert item.internal_name == "TH 1865x830 customer generic board"


def test_replenishment_auto_stages_material_without_location_choice(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
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
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "F3-RAW-001-R01-L01-G01"
            )
        )
        assert staging is not None
        assert lot.warehouse_location_id == staging.id
        assert staging.warehouse_floor == 3
        assert session.scalar(select(func.count(InventoryLot.id))) == 1
        assert session.scalar(select(func.count(InventoryMovement.id))) == 1
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 1
        assert session.scalar(
            select(func.count(SupplierReceiptSettlementPriceFact.id))
        ) == 1


def test_replenishment_missing_price_rolls_back_every_fact_and_same_key_can_retry(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.audit import OperationLog
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.material import Material
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_customer_replenishment_payload(quantity=11),
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]

        with session_factory() as session:
            material = session.get(Material, 1)
            assert material is not None
            material.quote_price = None
            session.commit()
            counts_before = {
                model: int(session.scalar(select(func.count(model.id))) or 0)
                for model in (
                    IncomingReceipt,
                    IncomingReceiptItem,
                    InventoryLot,
                    InventoryMovement,
                    SupplierReceiptSettlementPriceFact,
                    OperationLog,
                )
            }

        payload = {
            "received_quantity": 11,
            "idempotency_key": "p0-39-replenishment-price-retry",
        }
        blocked = client.put(
            f"/api/incoming/receive/sr{item_id}",
            json=payload,
        )
        assert blocked.status_code == 422, blocked.text
        assert blocked.json()["detail"]["code"] == (
            "SUPPLIER_RECEIPT_MASTER_PRICE_INVALID"
        )

        with session_factory() as session:
            for model, expected in counts_before.items():
                assert int(session.scalar(select(func.count(model.id))) or 0) == expected
            material = session.get(Material, 1)
            assert material is not None
            material.quote_price = Decimal("2.80")
            session.commit()

        received = client.put(
            f"/api/incoming/receive/sr{item_id}",
            json=payload,
        )
        assert received.status_code == 200, received.text

    with session_factory() as session:
        assert int(
            session.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id)))
            or 0
        ) == 1
        assert int(session.scalar(select(func.count(IncomingReceiptItem.id))) or 0) == 1
        assert int(session.scalar(select(func.count(InventoryLot.id))) or 0) == 1


def test_replenishment_rejects_floor3_left_marker_until_map_is_published(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import (
        InventoryLot,
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    with session_factory() as session:
        staging = session.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "F3-RAW-001-R01-L01-G01"
            )
        )
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 3)
        )
        area = session.scalar(
            select(WarehouseArea).where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code == "RAW-001",
            )
        )
        assert staging is not None and area is not None
        staging.placement_status = "unplaced"
        area.construction_status = "ledger_building"
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
                "idempotency_key": "replenishment-transitional-floor3-left-staging",
            },
        )
        assert received.status_code == 409, received.text
        assert "三楼左区" in received.json()["detail"]
        assert "一楼" in received.json()["detail"]

    with session_factory() as session:
        assert session.scalar(select(func.count(InventoryLot.id))) == 0
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 0


def test_replenishment_does_not_broaden_transition_to_other_unplaced_markers(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import (
        InventoryLot,
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    with session_factory() as session:
        staging = session.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "F3-RAW-001-R01-L01-G01"
            )
        )
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 3)
        )
        area = session.scalar(
            select(WarehouseArea).where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code == "RAW-001",
            )
        )
        assert staging is not None and area is not None
        staging.location_code = "RAW-001-UNPLACED"
        staging.placement_status = "unplaced"
        area.construction_status = "ledger_building"
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
                "idempotency_key": "replenishment-other-unplaced-staging-blocked",
            },
        )
        assert received.status_code == 409, received.text

    with session_factory() as session:
        assert session.scalar(select(func.count(InventoryLot.id))) == 0
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 0


def test_unpublished_floor3_left_marker_rejects_all_inventory_service_calls(
    stock_replenishment_app,
) -> None:
    _app, session_factory = stock_replenishment_app
    from app.models.warehouse_inventory import (
        InventoryLot,
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )
    from app.services.warehouse_inventory import (
        WarehouseInventoryError,
        manual_semi_finished_in,
    )

    with session_factory() as session:
        staging = session.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "F3-RAW-001-R01-L01-G01"
            )
        )
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 3)
        )
        area = session.scalar(
            select(WarehouseArea).where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code == "RAW-001",
            )
        )
        assert staging is not None and area is not None
        staging.placement_status = "unplaced"
        area.construction_status = "ledger_building"
        session.commit()

        with pytest.raises(
            WarehouseInventoryError,
            match="目标库位.*刷新后重试",
        ):
            manual_semi_finished_in(
                session,
                location_id=staging.id,
                quantity=10,
                stock_date=date.today(),
                source_type="manual",
                material_code="A416D",
                layer_count=5,
                flute_type="AB",
                board_length_mm=1865,
                board_width_mm=830,
                sheet_type="raw_board",
                supplier_name="苏州佳丰",
                customer_id=None,
                crease_type=None,
                crease_left_mm=None,
                crease_middle_mm=None,
                crease_right_mm=None,
                cutting_note=None,
                remarks=None,
                operator_id=None,
                idempotency_key="transitional-a1-manual-service-blocked",
                allow_raw_material_staging=True,
            )
        session.rollback()
        assert session.scalar(select(func.count(InventoryLot.id))) == 0


def test_replenishment_receive_fails_closed_without_floor3_left_staging(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation

    with session_factory() as session:
        staging = session.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "F3-RAW-001-R01-L01-G01"
            )
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
                "idempotency_key": "replenishment-no-floor3-left-staging",
            },
        )
        assert received.status_code == 409, received.text
        assert "三楼左区" in received.json()["detail"]
        assert "一楼" in received.json()["detail"]

    with session_factory() as session:
        assert session.scalar(select(func.count(InventoryLot.id))) == 0
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 0


def test_replenishment_never_falls_back_to_published_floor1_raw_material_area(
    stock_replenishment_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        InventoryLot,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseGroundLayoutPlan,
        WarehouseGroundLayoutSlot,
        WarehouseLocation,
    )

    monkeypatch.setattr(
        "app.services.location_candidates.load_warehouse_twin_published_floor_identity",
        lambda floor_number: {
            "revision": "stock-replenishment-map-v2",
            "zones_by_id": {"stock-replenishment-zone-raw": "RAW-006"},
            "zone_ids_by_area": {"RAW-006": ("stock-replenishment-zone-raw",)},
        }
        if int(floor_number) == 1
        else None,
    )

    with session_factory() as session:
        legacy = session.scalar(
            select(WarehouseLocation).where(WarehouseLocation.location_code == "1FA")
        )
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 1)
        )
        assert legacy is not None and floor is not None
        legacy.is_active = False
        raw_area = WarehouseArea(
            floor_id=floor.id,
            area_code="RAW-006",
            area_name="一楼原料区",
            construction_status="enabled",
        )
        session.add(raw_area)
        session.flush()
        session.add(
            WarehouseAreaStoragePolicy(
                area_id=raw_area.id,
                map_feature_id="stock-replenishment-zone-raw",
                allowed_inventory_types_json='["raw_material"]',
                storage_layout="pallet_ground",
                status="published",
                published_map_revision="stock-replenishment-map-v2",
                version=1,
            )
        )
        staging = WarehouseLocation(
            location_code="F1-RAW-006-L001",
            location_name="一楼原料区第1位",
            warehouse_type="shared",
            warehouse_floor=1,
            area_code="RAW-006",
            storage_type="ground",
            placement_status="placed",
            source_version="TWIN_V1",
            is_active=True,
        )
        session.add(staging)
        session.flush()
        session.add(
            Floor3LocationLayout(
                location_id=staging.id,
                left_pct=0,
                top_pct=0,
                width_pct=50,
                height_pct=100,
                source_type="seeded",
            )
        )
        plan = WarehouseGroundLayoutPlan(
            area_id=raw_area.id,
            status="published",
            target_slot_count=1,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision="stock-replenishment-map-v2",
            published_map_revision="stock-replenishment-map-v2",
            preview_fingerprint="c" * 64,
            version=1,
            publish_idempotency_key="stock-replenishment-raw-ground-v2",
            publish_request_hash="d" * 64,
            updated_by=1,
            published_by=1,
            published_at=datetime.now(),
        )
        session.add(plan)
        session.flush()
        session.add(
            WarehouseGroundLayoutSlot(
                plan_id=plan.id,
                location_id=staging.id,
                route_sequence=1,
                row_no=1,
                slot_no=1,
                x_mm=0,
                y_mm=0,
                width_mm=1200,
                depth_mm=1000,
            )
        )
        session.commit()
        staging_id = int(staging.id)

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
                "idempotency_key": "replenishment-current-raw-area",
            },
        )
        assert received.status_code == 409, received.text
        assert "三楼左区" in received.text

    with session_factory() as session:
        assert session.scalar(select(func.count(InventoryLot.id))) == 0
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 0


def test_replenishment_uses_current_published_floor3_raw_material_rack(
    stock_replenishment_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        InventoryLot,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseLocation,
    )

    revision = "stock-replenishment-current-floor3"
    feature_id = "stock-replenishment-zone-raw-rack"
    monkeypatch.setattr(
        "app.services.location_candidates.load_warehouse_twin_published_floor_identity",
        lambda floor_number: {
            "revision": revision,
            "zones_by_id": {feature_id: "RAW-001"},
            "zone_ids_by_area": {"RAW-001": (feature_id,)},
        }
        if int(floor_number) == 3
        else {
            "revision": "stock-replenishment-map-v1",
            "zones_by_id": {"stock-replenishment-zone-a1": "A1"},
            "zone_ids_by_area": {"A1": ("stock-replenishment-zone-a1",)},
        },
    )

    with session_factory() as session:
        legacy = session.scalar(
            select(WarehouseLocation).where(WarehouseLocation.location_code == "1FA")
        )
        baseline = session.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "F3-RAW-001-R01-L01-G01"
            )
        )
        assert legacy is not None and baseline is not None
        legacy.is_active = False
        baseline.is_active = False
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 3)
        )
        raw_area = session.scalar(
            select(WarehouseArea).where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code == "RAW-001",
            )
        )
        assert floor is not None and raw_area is not None
        policy = session.scalar(
            select(WarehouseAreaStoragePolicy).where(
                WarehouseAreaStoragePolicy.area_id == raw_area.id
            )
        )
        assert policy is not None
        policy.map_feature_id = feature_id
        policy.allowed_inventory_types_json = '["raw_material"]'
        policy.storage_layout = "rack"
        policy.published_map_revision = revision
        staging = WarehouseLocation(
            location_code="3F-RAW-001-L001",
            location_name="当前地图原料货架第1位",
            warehouse_type="semi_finished",
            warehouse_floor=3,
            area_code="RAW-001",
            storage_type="rack",
            placement_status="placed",
            source_version="CURRENT_MAP",
            address_kind="rack_slot",
            address_area_id=raw_area.id,
            rack_code="A",
            level_no=1,
            slot_no=1,
            is_active=True,
        )
        session.add(staging)
        session.flush()
        session.add(
            Floor3LocationLayout(
                location_id=staging.id,
                left_pct=0,
                top_pct=0,
                width_pct=25,
                height_pct=100,
                source_type="seeded",
                layout_kind="physical_rack",
            )
        )
        session.commit()
        staging_id = int(staging.id)

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
                "idempotency_key": "replenishment-current-floor3-raw-rack",
            },
        )
        assert received.status_code == 200, received.text

    with session_factory() as session:
        lot = session.scalar(select(InventoryLot))
        assert lot is not None
        assert int(lot.warehouse_location_id) == staging_id
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 1


def test_replenishment_does_not_repair_or_use_floor1_legacy_ground_plan(
    stock_replenishment_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.audit import OperationLog
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        InventoryLot,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseGroundLayoutPlan,
        WarehouseGroundLayoutSlot,
        WarehouseLocation,
    )
    from app.services.warehouse_inventory import (
        automatic_raw_material_staging_location,
    )

    revision = "stock-replenishment-legacy-map-v1"
    feature_id = "stock-replenishment-zone-legacy-raw"
    monkeypatch.setattr(
        "app.services.location_candidates.load_warehouse_twin_published_floor_identity",
        lambda floor_number: {
            "revision": revision,
            "zones_by_id": {feature_id: "RAW-006"},
            "zone_ids_by_area": {"RAW-006": (feature_id,)},
        }
        if int(floor_number) == 1
        else None,
    )
    monkeypatch.setattr(
        "app.services.warehouse_inventory.load_warehouse_twin_floor",
        lambda floor_code: {
            "floor_code": str(floor_code).upper(),
            "revision": revision,
            "bounds_mm": {
                "min_x": 0,
                "min_y": 0,
                "max_x": 1200,
                "max_y": 1000,
            },
            "features": [
                {
                    "id": feature_id,
                    "feature_kind": "zone",
                    "erp_area_code": "RAW-006",
                    "points": [[0, 0], [1200, 0], [1200, 1000], [0, 1000]],
                }
            ],
        },
    )

    with session_factory() as session:
        legacy = session.scalar(
            select(WarehouseLocation).where(WarehouseLocation.location_code == "1FA")
        )
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 1)
        )
        assert legacy is not None and floor is not None
        legacy.is_active = False
        raw_area = WarehouseArea(
            floor_id=floor.id,
            area_code="RAW-006",
            area_name="一楼原料区",
            construction_status="enabled",
        )
        session.add(raw_area)
        session.flush()
        session.add(
            WarehouseAreaStoragePolicy(
                area_id=raw_area.id,
                map_feature_id=feature_id,
                allowed_inventory_types_json='["raw_material"]',
                storage_layout="pallet_ground",
                status="published",
                published_map_revision=revision,
                version=1,
            )
        )
        staging = WarehouseLocation(
            location_code="F1-RAW-006-L001",
            location_name="一楼原料区第1位",
            warehouse_type="shared",
            warehouse_floor=1,
            area_code="RAW-006",
            storage_type="ground",
            placement_status="placed",
            source_version="TWIN_V1",
            is_active=True,
        )
        session.add(staging)
        session.flush()
        session.add(
            Floor3LocationLayout(
                location_id=staging.id,
                left_pct=0,
                top_pct=0,
                width_pct=100,
                height_pct=100,
                source_type="manual",
                layout_kind="unknown",
            )
        )
        session.commit()
        staging_id = int(staging.id)
        raw_area_id = int(raw_area.id)

    with session_factory() as session:
        projected = automatic_raw_material_staging_location(
            session,
            allow_repairable_legacy=True,
        )
        assert int(projected.id) == staging_id
        assert session.scalar(
            select(WarehouseGroundLayoutPlan).where(
                WarehouseGroundLayoutPlan.area_id == raw_area_id
            )
        ) is None

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
                "idempotency_key": "replenishment-repair-legacy-raw-plan",
            },
        )
        assert received.status_code == 409, received.text
        assert "三楼左区" in received.text

    with session_factory() as session:
        plan = session.scalar(
            select(WarehouseGroundLayoutPlan).where(
                WarehouseGroundLayoutPlan.area_id == raw_area_id
            )
        )
        assert plan is None
        layout = session.scalar(
            select(Floor3LocationLayout).where(
                Floor3LocationLayout.location_id == staging_id
            )
        )
        assert layout is not None
        assert layout.layout_kind == "unknown"
        assert int(layout.version) == 1
        audit = session.scalar(
            select(OperationLog).where(
                OperationLog.action_code
                == "warehouse.legacy_ground_plan.auto_repair"
            )
        )
        assert audit is None
        assert session.scalar(select(func.count(InventoryLot.id))) == 0
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 0


def test_replenishment_does_not_use_raw_area_without_published_ground_slot(
    stock_replenishment_app,
) -> None:
    app, session_factory = stock_replenishment_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import (
        InventoryLot,
        WarehouseArea,
        WarehouseGroundLayoutPlan,
    )

    with session_factory() as session:
        plan = session.scalar(
            select(WarehouseGroundLayoutPlan)
            .join(WarehouseArea)
            .where(WarehouseArea.area_code == "RAW-001")
        )
        assert plan is not None
        session.delete(plan)
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
                "idempotency_key": "replenishment-raw-area-no-ground-plan",
            },
        )
        assert received.status_code == 409, received.text
        assert "三楼左区" in received.json()["detail"]

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
        expected_requisition_date = (
            datetime.fromisoformat(order["confirmed_at"].replace("Z", "+00:00"))
            .astimezone(timezone(timedelta(hours=8)))
            .date()
            .isoformat()
        )

        reported = client.get("/api/requisition/reported-documents")
        reported_row = next(
            row
            for row in reported.json()["items"]
            if row["document_number"] == order["order_number"]
        )
        assert reported_row["incoming_status"] == "待入库"
        assert reported_row["can_void"] is True
        reported_items = client.get(
            "/api/requisition/reported-items",
            params={"source_type": "stock_replenishment", "status": "active"},
        )
        assert reported_items.status_code == 200, reported_items.text
        reported_item = next(
            row
            for row in reported_items.json()["items"]
            if row["document_number"] == order["order_number"]
        )
        assert reported_item["status"] == "active"
        assert reported_item["stock_replenishment_can_void"] is True

        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        pending_row = next(
            row
            for row in pending.json()["items"]
            if row["item_id"] == f"sr{item['id']}"
        )
        assert pending_row["source_type"] == "stock_replenishment"
        assert pending_row["incoming_quantity"] == 30
        assert pending_row["requisition_date"] == expected_requisition_date
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
        received_reported_items = client.get(
            "/api/requisition/reported-items",
            params={"source_type": "stock_replenishment", "status": "active"},
        )
        assert received_reported_items.status_code == 200
        assert any(
            row["document_number"] == order["order_number"]
            and row["status"] == "active"
            and row["stock_replenishment_can_void"] is False
            for row in received_reported_items.json()["items"]
        )
        receipt_history = client.get("/api/incoming/history")
        assert receipt_history.status_code == 200, receipt_history.text
        history_row = next(
            row
            for row in receipt_history.json()["items"]
            if row.get("stock_replenishment_item_id") == item["id"]
        )
        assert history_row["requisition_date"] == expected_requisition_date
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
        voided_reported_items = client.get(
            "/api/requisition/reported-items",
            params={"source_type": "stock_replenishment", "status": "voided"},
        )
        assert voided_reported_items.status_code == 200
        assert any(
            row["document_number"] == second_order["order_number"]
            and row["status"] == "voided"
            and row["stock_replenishment_can_void"] is False
            for row in voided_reported_items.json()["items"]
        )
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
