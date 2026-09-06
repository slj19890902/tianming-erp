from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def twin_dashboard_app(tmp_path, monkeypatch):
    from app.services import location_candidates

    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        lambda floor_number: {
            "revision": f"p1-29-{int(floor_number)}f-map-v1",
            "zones_by_id": {"zone-a1": "A1"},
            "zone_ids_by_area": {"A1": ("zone-a1",)},
        },
    )
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        Floor3LocationLayout,
        InventoryLot,
        InventoryMovement,
        InventoryPallet,
        InventoryPalletItem,
        SemiFinishedInventoryDetail,
        WarehouseArea,
        WarehouseFloor,
        WarehouseGroundLayoutPlan,
        WarehouseGroundLayoutSlot,
        WarehouseLocation,
    )

    engine = create_sqlite_engine(tmp_path / "p1-29-dashboard.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    as_of = date.today()
    with factory() as db:
        admin = User(
            username="twin-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="智慧仓储管理员",
            must_change_password=False,
        )
        scoped = User(
            username="twin-scoped",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="授权客户员工",
            must_change_password=False,
            customer_access_mode="selected",
        )
        owner = Customer(name="苏州思迈尔包装有限公司", chinese_short_name="思迈尔", customer_code="SMILE")
        hidden_owner = Customer(name="昆山华诚电子有限公司", chinese_short_name="华诚", customer_code="HUACHENG")
        db.add_all([admin, scoped, owner, hidden_owner])
        db.flush()
        db.add(UserCustomerScope(user_id=scoped.id, customer_id=owner.id))
        product = Product(
            customer_id=owner.id,
            product_code="TM-FG-001",
            customer_material_code="TM-FG-001",
            product_name="五层加强纸箱",
            length_mm=Decimal("520"),
            width_mm=Decimal("350"),
            height_mm=Decimal("300"),
        )
        hidden_product = Product(
            customer_id=hidden_owner.id,
            product_code="HIDDEN-FG-001",
            customer_material_code="HIDDEN-FG-001",
            product_name="其他客户纸箱",
        )
        db.add_all([product, hidden_product])
        db.flush()

        floor1 = WarehouseFloor(
            floor_code="1F",
            floor_name="一楼生产与周转区",
            floor_number=1,
            construction_status="enabled",
        )
        floor3 = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼成品仓",
            floor_number=3,
            construction_status="enabled",
        )
        db.add_all([floor1, floor3])
        db.flush()
        raw_area = WarehouseArea(
            floor_id=floor1.id,
            area_code="RAW",
            area_name="原料区",
            planned_location_count=2,
            planned_pallet_capacity=10,
            construction_status="enabled",
        )
        finished_area = WarehouseArea(
            floor_id=floor3.id,
            area_code="A1",
            area_name="A1成品区",
            planned_location_count=4,
            planned_pallet_capacity=20,
            construction_status="enabled",
        )
        db.add_all([raw_area, finished_area])
        db.flush()
        locations = [
            WarehouseLocation(
                location_code="A1-L01",
                location_name="A1第一栈板位",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="A1",
                storage_type="ground",
                source_version="V11",
                placement_status="placed",
                sort_order=1,
            ),
            WarehouseLocation(
                location_code="A1-L02",
                location_name="A1第二栈板位",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="A1",
                storage_type="ground",
                source_version="V11",
                placement_status="placed",
                sort_order=2,
            ),
            WarehouseLocation(
                location_code="A1-L03",
                location_name="A1空栈板位",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="A1",
                storage_type="ground",
                source_version="V11",
                placement_status="placed",
                sort_order=3,
            ),
            WarehouseLocation(
                location_code="RAW-L01",
                location_name="一楼纸板区",
                warehouse_type="semi_finished",
                warehouse_floor=1,
                area_code="RAW",
                storage_type="ground",
                placement_status="placed",
                sort_order=4,
            ),
            WarehouseLocation(
                location_code="A1-PENDING",
                location_name="待布局成品位",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="A1",
                storage_type="ground",
                source_version="V11",
                placement_status="unplaced",
                sort_order=5,
            ),
        ]
        db.add_all(locations)
        db.flush()
        for index, location in enumerate(locations[:3]):
            db.add(
                Floor3LocationLayout(
                    location_id=location.id,
                    left_pct=Decimal(str(10 + index * 15)),
                    top_pct=Decimal("20"),
                    width_pct=Decimal("8"),
                    height_pct=Decimal("12"),
                    source_type="manual",
                )
            )
        ground_plan = WarehouseGroundLayoutPlan(
            area_id=finished_area.id,
            status="published",
            target_slot_count=3,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision="p1-29-3f-map-v1",
            published_map_revision="p1-29-3f-map-v1",
            preview_fingerprint="a" * 64,
            version=1,
            publish_idempotency_key="p1-29-ground-publish",
            publish_request_hash="b" * 64,
            updated_by=admin.id,
            published_by=admin.id,
            published_at=datetime.now(),
        )
        db.add(ground_plan)
        db.flush()
        for index, location in enumerate(locations[:3], start=1):
            db.add(
                WarehouseGroundLayoutSlot(
                    plan_id=ground_plan.id,
                    location_id=location.id,
                    route_sequence=index,
                    row_no=1,
                    slot_no=index,
                    x_mm=Decimal(str(1000 + (index - 1) * 1200)),
                    y_mm=Decimal("1000"),
                    width_mm=1200,
                    depth_mm=1000,
                )
            )

        def finished_lot(
            *,
            suffix: str,
            location: WarehouseLocation,
            customer: Customer,
            target_product: Product,
            quantity: int,
            age_days: int,
            code: str,
        ) -> InventoryLot:
            lot = InventoryLot(
                lot_number=f"FG-{suffix}",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=quantity,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="stocktake",
                stock_date=as_of - timedelta(days=age_days),
                stock_date_accuracy="exact",
                last_movement_at=datetime.utcnow(),
            )
            db.add(lot)
            db.flush()
            db.add(
                FinishedGoodsInventoryDetail(
                    inventory_lot_id=lot.id,
                    owner_customer_id=customer.id,
                    owner_customer_name_snapshot=customer.name,
                    is_general=False,
                    product_id=target_product.id,
                    inventory_code_snapshot=code,
                    product_name_snapshot=target_product.product_name,
                )
            )
            return lot

        owner_lot = finished_lot(
            suffix="OWNER",
            location=locations[0],
            customer=owner,
            target_product=product,
            quantity=120,
            age_days=120,
            code="TM-FG-001",
        )
        loose_owner_lot = finished_lot(
            suffix="OWNER-LOOSE",
            location=locations[0],
            customer=owner,
            target_product=product,
            quantity=5,
            age_days=2,
            code="TM-FG-001-LOOSE",
        )
        hidden_lot = finished_lot(
            suffix="HIDDEN",
            location=locations[1],
            customer=hidden_owner,
            target_product=hidden_product,
            quantity=80,
            age_days=10,
            code="HIDDEN-FG-001",
        )
        pending_lot = finished_lot(
            suffix="PENDING",
            location=locations[4],
            customer=owner,
            target_product=product,
            quantity=15,
            age_days=5,
            code="TM-FG-001-PENDING",
        )
        semi_lot = InventoryLot(
            lot_number="SI-OWNER",
            inventory_type="semi_finished",
            warehouse_location_id=locations[3].id,
            quantity_available=60,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="stocktake",
            stock_date=as_of - timedelta(days=20),
            stock_date_accuracy="exact",
            last_movement_at=datetime.utcnow(),
        )
        db.add(semi_lot)
        db.flush()
        db.add(
            SemiFinishedInventoryDetail(
                inventory_lot_id=semi_lot.id,
                owner_customer_id=owner.id,
                owner_customer_name_snapshot=owner.name,
                material_code_snapshot="K=A",
                normalized_material_code="K=A",
                layer_count=3,
                flute_type="B",
                board_length_mm=1200,
                board_width_mm=800,
                sheet_type="raw_board",
                component_type="whole",
                pieces_per_box=1,
                stock_yield_per_sheet=1,
            )
        )
        pallets = []
        for index, (location, lot, customer, target_product, code, quantity) in enumerate(
            [
                (locations[0], owner_lot, owner, product, "TM-FG-001", 120),
                (locations[1], hidden_lot, hidden_owner, hidden_product, "HIDDEN-FG-001", 80),
            ],
            start=1,
        ):
            pallet = InventoryPallet(
                pallet_code=f"PLT-3F-{index:03d}",
                location_id=location.id,
                status="active",
                is_current=True,
            )
            db.add(pallet)
            db.flush()
            db.add(
                InventoryPalletItem(
                    pallet_id=pallet.id,
                    inventory_lot_id=lot.id,
                    customer_id=customer.id,
                    product_id=target_product.id,
                    inventory_code=code,
                    customer_name_snapshot=customer.name,
                    product_name=target_product.product_name,
                    item_type="finished",
                    quantity=quantity,
                    unit="boxes",
                    match_status="matched",
                )
            )
            pallets.append(pallet)
        db.add(
            InventoryPallet(
                pallet_code="PLT-3F-EMPTY-RESIDUE",
                location_id=locations[2].id,
                status="active",
                is_current=True,
            )
        )
        db.flush()
        yesterday = datetime.utcnow() - timedelta(days=1)
        db.add_all(
            [
                InventoryMovement(
                    movement_number="MV-P1-29-IN",
                    inventory_lot_id=owner_lot.id,
                    movement_type="manual_in",
                    quantity=120,
                    unit="boxes",
                    before_available=0,
                    after_available=120,
                    before_reserved=0,
                    after_reserved=0,
                    before_consumed=0,
                    after_consumed=0,
                    before_damaged=0,
                    after_damaged=0,
                    before_scrapped=0,
                    after_scrapped=0,
                    created_at=yesterday,
                ),
                InventoryMovement(
                    movement_number="MV-P1-29-MOVE",
                    inventory_lot_id=owner_lot.id,
                    movement_type="location_transfer",
                    quantity=120,
                    unit="boxes",
                    before_available=120,
                    after_available=120,
                    before_reserved=0,
                    after_reserved=0,
                    before_consumed=0,
                    after_consumed=0,
                    before_damaged=0,
                    after_damaged=0,
                    before_scrapped=0,
                    after_scrapped=0,
                    created_at=datetime.utcnow(),
                ),
            ]
        )
        db.commit()
        ids = {"owner": owner.id, "hidden_owner": hidden_owner.id}

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def test_map_pallet_move_eligibility_includes_nonvisible_historical_items(
    twin_dashboard_app,
) -> None:
    from sqlalchemy import event, select
    from app.api.deps import get_db
    from app.api.warehouse import _twin_dashboard_source_rows
    from app.models.user import User
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail, InventoryLot, InventoryPallet,
        InventoryPalletItem,
    )
    from app.services.warehouse_movement_batch import (
        WarehouseMovementBatchError, load_movable_pallet, pallet_move_source_issue,
    )

    app, _ids = twin_dashboard_app
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    try:
        pallet = db.scalar(select(InventoryPallet).order_by(InventoryPallet.id))
        original = pallet.items[0]
        pallet_id = pallet.id
        with TestClient(app) as client:
            _login(client, "twin-admin")
            before = client.get("/api/warehouse/twin-dashboard/overview").json()
            shown_before = next(
                p for row in before["locations"] for p in row["pallets"]
                if p["pallet_id"] == pallet_id
            )
            assert shown_before["move_eligible"] is True
            spent = InventoryLot(
                lot_number="MAP-MOVE-SPENT-HISTORY",
                inventory_type="finished",
                warehouse_location_id=pallet.location_id,
                quantity_available=0, quantity_reserved=0,
                quantity_damaged=0, quantity_scrapped=10,
                status="active", unit="boxes", source_type="manual",
                stock_date=original.inventory_lot.stock_date,
                last_movement_at=original.inventory_lot.last_movement_at,
            )
            db.add(spent)
            db.flush()
            db.add(FinishedGoodsInventoryDetail(
                inventory_lot_id=spent.id,
                owner_customer_id=original.customer_id,
                product_id=original.product_id,
                inventory_code_snapshot=original.inventory_lot.finished_detail.inventory_code_snapshot,
                product_name_snapshot=original.inventory_lot.finished_detail.product_name_snapshot,
            ))
            db.add(InventoryPalletItem(
                pallet_id=pallet.id, inventory_lot_id=spent.id,
                customer_id=original.customer_id, product_id=original.product_id,
                item_type="finished", quantity=10, unit="boxes", match_status="matched",
            ))
            db.commit()
            db.expire_all()
            with pytest.raises(WarehouseMovementBatchError) as denied:
                load_movable_pallet(db, pallet_id)
            after = client.get("/api/warehouse/twin-dashboard/overview").json()
        shown_after = next(
            p for row in after["locations"] for p in row["pallets"]
            if p["pallet_id"] == pallet_id
        )
        assert shown_after["items"] == shown_before["items"]
        assert shown_after["item_count"] == shown_before["item_count"]
        assert shown_after["move_eligible"] is False
        assert shown_after["move_block_reason"] == str(denied.value)
        assert db.get(InventoryLot, spent.id).quantity_scrapped == 10
        admin = db.scalar(select(User).where(User.username == "twin-admin"))
        lots, locations, pallets, _floors, _scope = _twin_dashboard_source_rows(db, admin)
        lot_map = {row.id: row for row in lots}
        location_map = {row.id: row for row in locations}
        statements = []

        def capture_statement(*args):
            statements.append(args[2])

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            issues = {
                row.id: pallet_move_source_issue(
                    row, location_map.get(row.location_id), lots_by_id=lot_map,
                )
                for row in pallets
            }
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)
        assert issues[pallet_id] == str(denied.value)
        assert statements == [], "Map source eligibility must reuse the loaded dashboard facts"
    finally:
        dependency.close()


def test_dashboard_keeps_native_units_and_hides_unconfirmed_capacity_metrics(
    twin_dashboard_app,
) -> None:
    app, _ids = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-admin")
        response = client.get("/api/warehouse/twin-dashboard/overview?days=30")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["standard_pallet"] == {
        "contract_version": "standard-pallet-v1",
        "width_mm": 1200,
        "depth_mm": 1000,
        "height_mm": 150,
    }
    quantities = {row["key"]: row for row in payload["summary"]["quantities"]}
    assert quantities["finished:boxes"]["available"] == 220
    assert quantities["semi_finished:sheets"]["available"] == 60
    assert payload["summary"]["occupied_pallets"] == 2
    assert payload["summary"]["empty_mapped_locations"] == 1
    assert payload["summary"]["long_age_lots"] == 1
    assert payload["summary"]["unlocated_lots"] == 1
    assert payload["summary"]["finished_map_coverage"] == {
        "total_lots": 4,
        "mapped_lots": 3,
        "unlocated_lots": 1,
        "total_quantity": 220,
        "mapped_quantity": 205,
        "unlocated_quantity": 15,
        "total_physical_quantity": 220,
        "mapped_physical_quantity": 205,
        "unlocated_physical_quantity": 15,
        "all_located": False,
    }
    assert payload["summary"]["finished_pallet_coverage"] == {
        "total_lots": 4,
        "current_same_location_pallet_lots": 2,
        "without_current_same_location_pallet_lots": 2,
        "total_quantity": 220,
        "current_same_location_pallet_quantity": 200,
        "without_current_same_location_pallet_quantity": 20,
        "total_physical_quantity": 220,
        "current_same_location_pallet_physical_quantity": 200,
        "without_current_same_location_pallet_physical_quantity": 20,
        "all_on_current_same_location_pallet": False,
        "conservation": {
            "lot_count": True,
            "quantity": True,
            "physical_quantity": True,
        },
    }
    assert payload["summary"]["mapped_location_without_current_pallet"] == {
        "lot_count": 1,
        "quantity": 5,
        "physical_quantity": 5,
    }
    assert payload["summary"]["empty_current_pallet_residue_count"] == 1
    assert payload["summary"]["finished_projection_complete"] is False
    assert len(payload["unlocated_inventory"]) == 1
    assert payload["unlocated_inventory"][0]["inventory_code"] == "TM-FG-001-PENDING"
    assert payload["unlocated_inventory"][0]["position_status"] == "unplaced"
    assert payload["unlocated_inventory"][0]["map_position"] is None
    assert payload["floors"][1]["capacity"]["confirmed"] is False
    assert payload["floors"][1]["capacity"]["safe_pallet_capacity"] is None
    assert payload["floors"][1]["capacity"]["basis"] == "planning_reference"
    assert payload["floors"][1]["capacity"]["planned_pallet_capacity"] == 20
    assert payload["floors"][1]["capacity"]["reference_pallet_capacity"] is None
    assert payload["floors"][1]["capacity"]["empty_pallet_slots"] is None
    assert payload["floors"][1]["capacity"]["utilization_percent"] is None
    assert payload["floors"][1]["capacity"]["thresholds"] is None
    assert payload["alerts"][0]["code"] == "capacity_field_confirmation_pending"
    assert payload["read_only"] is True
    assert payload["generated_at"].endswith("Z")
    mapped = next(row for row in payload["locations"] if row["location_code"] == "A1-L01")
    assert mapped["map_position"]["version"] == 1
    assert mapped["map_position"]["z_index"] == 0
    assert mapped["current_address_name"] == "三楼 右区A1·成品区·A1-1"
    assert mapped["employee_location_name"] == "三楼 右区A1·成品区·A1-1"
    assert mapped["pallets"][0]["items"][0]["customer_short_name"] == "思迈尔"
    assert [row["inventory_code"] for row in mapped["loose_items"]] == [
        "TM-FG-001-LOOSE"
    ]
    assert mapped["loose_items"][0]["pallet_projection_status"] == (
        "missing_current_pallet"
    )
    empty = next(row for row in payload["locations"] if row["location_code"] == "A1-L03")
    assert empty["occupancy_status"] == "empty"
    assert empty["pallets"] == []
    assert {
        row["code"] for row in payload["alerts"]
    } >= {
        "mapped_location_without_current_pallet",
        "empty_current_pallet_residue",
        "unlocated_inventory",
    }


def test_homepage_capacity_summary_waits_for_all_field_confirmations(
    twin_dashboard_app,
) -> None:
    app, _ids = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-admin")
        response = client.get("/api/warehouse/capacity/summary")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["visible"] is True
    assert payload["confirmed"] is False
    assert payload["planned_pallet_capacity"] == 30
    assert payload["reference_pallet_capacity"] is None
    assert payload["occupied_pallets"] == 2
    assert payload["empty_pallet_slots"] is None
    assert payload["utilization_percent"] is None
    assert payload["tightest_floor_code"] is None
    assert payload["tightest_floor_utilization_percent"] is None
    assert payload["alert_count"] == 0
    assert payload["forecast_7d_peak_floor_code"] is None
    assert payload["forecast_7d_peak_utilization_percent"] is None
    assert payload["forecast_7d_action_count"] == 0
    assert "不参与满载率、空位、阈值或预测" in payload["notice"]
    assert {row["floor_code"] for row in payload["floors"]} == {"1F", "3F"}


def test_homepage_capacity_summary_is_hidden_for_customer_scoped_accounts(
    twin_dashboard_app,
) -> None:
    app, _ids = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-scoped")
        response = client.get("/api/warehouse/capacity/summary")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "visible": False,
        "notice": "当前账号按客户范围查看库存，不显示全仓容量。",
        "floors": [],
    }


def test_partial_confirmation_stays_hidden_until_all_floors_are_confirmed(
    twin_dashboard_app,
) -> None:
    app, _ids = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-admin")
        floors = client.get("/api/warehouse/space/floors").json()["items"]
        floor = next(row for row in floors if row["floor_code"] == "3F")
        area = floor["areas"][0]
        response = client.put(
            f"/api/warehouse/space/areas/{area['id']}",
            json={
                "floor_id": floor["id"],
                "area_code": area["area_code"],
                "area_name": area["area_name"],
                "planned_location_count": area["planned_location_count"],
                "planned_pallet_capacity": area["planned_pallet_capacity"],
                "capacity_review_status": "confirmed",
                "capacity_eligible": True,
                "confirmed_pallet_capacity": 20,
                "construction_status": "enabled",
                "remarks": "现场已复核",
            },
        )
        assert response.status_code == 200, response.text
        confirmed = response.json()
        assert confirmed["capacity_review_status"] == "confirmed"
        assert confirmed["confirmed_pallet_capacity"] == 20
        assert confirmed["capacity_reviewed_by"] == "智慧仓储管理员"
        assert confirmed["capacity_reviewed_at"].endswith("+08:00")

        partial = client.get("/api/warehouse/twin-dashboard/overview").json()
        floor3 = next(row for row in partial["floors"] if row["floor_code"] == "3F")
        assert floor3["capacity"]["confirmed"] is False
        assert floor3["capacity"]["reference_pallet_capacity"] is None
        assert floor3["capacity"]["utilization_percent"] is None
        assert floor3["capacity"]["thresholds"] is None

        floors = client.get("/api/warehouse/space/floors").json()["items"]
        floor1 = next(row for row in floors if row["floor_code"] == "1F")
        area1 = floor1["areas"][0]
        response = client.put(
            f"/api/warehouse/space/areas/{area1['id']}",
            json={
                "floor_id": floor1["id"],
                "area_code": area1["area_code"],
                "area_name": area1["area_name"],
                "planned_location_count": area1["planned_location_count"],
                "planned_pallet_capacity": area1["planned_pallet_capacity"],
                "capacity_review_status": "confirmed",
                "capacity_eligible": True,
                "confirmed_pallet_capacity": 10,
                "construction_status": "enabled",
                "remarks": "现场已复核",
            },
        )
        assert response.status_code == 200, response.text
        overview = client.get("/api/warehouse/twin-dashboard/overview").json()
        home = client.get("/api/warehouse/capacity/summary").json()

    floor3 = next(row for row in overview["floors"] if row["floor_code"] == "3F")
    assert floor3["capacity"]["confirmed"] is True
    assert floor3["capacity"]["basis"] == "confirmed"
    assert floor3["capacity"]["safe_pallet_capacity"] == 20
    assert floor3["capacity"]["coverage_percent"] == 100.0
    assert floor3["capacity"]["thresholds"] == {
        "attention": 16,
        "warning": 18,
        "critical": 19,
    }
    assert home["confirmed"] is True
    assert home["reference_pallet_capacity"] == 30
    assert home["empty_pallet_slots"] == 28
    assert home["utilization_percent"] == 6.7


def test_scoped_dashboard_hides_other_customer_capacity_and_empty_locations(
    twin_dashboard_app,
) -> None:
    app, _ids = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-scoped")
        response = client.get("/api/warehouse/twin-dashboard/overview")
    assert response.status_code == 200, response.text
    payload = response.json()
    serialized = response.text
    assert payload["scope"]["customer_restricted"] is True
    assert payload["summary"]["empty_mapped_locations"] is None
    assert all(floor["capacity"]["visible"] is False for floor in payload["floors"])
    assert "HIDDEN-FG-001" not in serialized
    assert "昆山华诚电子有限公司" not in serialized
    assert "TM-FG-001" in serialized


def test_inventory_code_search_returns_all_real_and_unplaced_matches(
    twin_dashboard_app,
) -> None:
    app, _ids = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-admin")
        response = client.get(
            "/api/warehouse/twin-dashboard/search",
            params={"inventory_code": "TM-FG-001"},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["result_count"] == 3
    assert {row["position_status"] for row in payload["items"]} == {"mapped", "unplaced"}
    assert {row["location_code"] for row in payload["items"]} == {"A1-L01", "A1-PENDING"}
    assert payload["floor_summaries"] == [
        {
            "floor_code": "3F",
            "lot_count": 3,
            "quantities": {"finished:boxes": 140},
            "location_count": 2,
        }
    ]
    assert "不生成虚假地图点" in payload["notice"]


def test_inventory_code_search_is_customer_scoped(twin_dashboard_app) -> None:
    app, _ids = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-scoped")
        response = client.get(
            "/api/warehouse/twin-dashboard/search",
            params={"inventory_code": "HIDDEN"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["result_count"] == 0


def test_full_warehouse_search_matches_product_customer_lot_and_location(
    twin_dashboard_app,
) -> None:
    app, _ids = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-admin")
        product = client.get(
            "/api/warehouse/twin-dashboard/search",
            params={"keyword": "五层加强纸箱"},
        )
        customer = client.get(
            "/api/warehouse/twin-dashboard/search",
            params={"keyword": "苏州思迈尔"},
        )
        lot = client.get(
            "/api/warehouse/twin-dashboard/search",
            params={"keyword": "FG-OWNER"},
        )
        location = client.get(
            "/api/warehouse/twin-dashboard/search",
            params={"keyword": "A1第一栈板位"},
        )
    for response in (product, customer, lot, location):
        assert response.status_code == 200, response.text
        assert response.json()["result_count"] >= 1
    assert all(
        "其他客户纸箱" not in str(response.json()["items"])
        for response in (product, customer, lot, location)
    )


def test_internal_location_transfer_does_not_create_throughput(
    twin_dashboard_app,
) -> None:
    app, _ids = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-admin")
        response = client.get("/api/warehouse/twin-dashboard/overview?days=7")
    assert response.status_code == 200, response.text
    throughput = response.json()["throughput"]
    assert sum(row["inbound"] for row in throughput) == 120
    assert sum(row["outbound"] for row in throughput) == 0
    assert sum(row["adjustment"] for row in throughput) == 0
