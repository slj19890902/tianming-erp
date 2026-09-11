from __future__ import annotations

import json
from collections.abc import Generator
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.services.warehouse_twin_layout import (
    load_warehouse_twin_published_floor_identity,
)
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    FinishedGoodsInventoryDetail,
    InventoryLocationMovement,
    InventoryLot,
    InventoryLotTransfer,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseGroundOccupancy,
    WarehouseLocation,
)


MOVE_BATCH_URL = "/api/warehouse/twin-operations/move-batches"
FRONTEND = (
    Path(__file__).resolve().parents[1]
    / "factory_twin"
    / "frontend"
    / "src"
    / "WarehouseTwinApp.tsx"
)


def _published_area(
    floor: WarehouseFloor,
    *,
    area_code: str,
    inventory_types: tuple[str, ...] = ("finished",),
) -> WarehouseArea:
    area = WarehouseArea(
        floor=floor,
        area_code=area_code,
        area_name=f"{floor.floor_code}-{area_code}",
        construction_status="enabled",
    )
    published_identity = load_warehouse_twin_published_floor_identity(
        int(floor.floor_number)
    )
    current_feature_ids = tuple(
        published_identity.get("zone_ids_by_area", {}).get(area_code, ())
    )
    map_feature_id = f"zone-{floor.floor_code.lower()}-{area_code.lower()}"
    published_map_revision = "p1-47c-test-map"
    if len(current_feature_ids) == 1:
        map_feature_id = current_feature_ids[0]
        published_map_revision = str(published_identity["revision"])
    area.storage_policy = WarehouseAreaStoragePolicy(
        map_feature_id=map_feature_id,
        allowed_inventory_types_json=json.dumps(list(inventory_types)),
        storage_layout="pallet_ground",
        status="published",
        # Movement validates every published target against the live map.  Keep
        # the fixture aligned when a later map revision becomes the runtime map.
        published_map_revision=published_map_revision,
        version=1,
    )
    return area


def _location(
    *,
    code: str,
    floor: int,
    area: str,
    source_version: str,
    warehouse_type: str = "finished",
    storage_type: str = "ground",
    active: bool = True,
    placed: bool = True,
) -> WarehouseLocation:
    row = WarehouseLocation(
        location_code=code,
        location_name=code,
        warehouse_type=warehouse_type,
        warehouse_floor=floor,
        area_code=area,
        storage_type=storage_type,
        is_active=active,
        placement_status="placed" if placed else "unplaced",
        source_version=source_version,
    )
    if placed:
        row.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal("1"),
            top_pct=Decimal("1"),
            width_pct=Decimal("1"),
            height_pct=Decimal("1"),
            z_index=0,
            version=1,
            source_type="manual",
        )
    return row


def _finished_lot(
    *,
    number: str,
    location: WarehouseLocation,
    customer: Customer,
    product: Product,
    available: int,
    reserved: int,
    source_ref_id: int,
) -> InventoryLot:
    lot = InventoryLot(
        lot_number=number,
        inventory_type="finished",
        warehouse_location_id=location.id,
        quantity_available=available,
        quantity_reserved=reserved,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit="boxes",
        status="active",
        source_type="production_completion",
        source_ref_type="production_completion",
        source_ref_id=source_ref_id,
        stock_date=date(2026, 8, 1),
        stock_date_accuracy="exact",
        stock_date_original_text="2026-08-01",
        last_movement_at=datetime.now(timezone.utc).replace(tzinfo=None),
        version=1,
    )
    lot.finished_detail = FinishedGoodsInventoryDetail(
        owner_customer_id=customer.id,
        owner_customer_name_snapshot=customer.name,
        is_general=False,
        product_id=product.id,
        inventory_code_snapshot=product.product_code,
        product_name_snapshot=product.product_name,
        box_type_snapshot="A1",
        length_mm=500,
        width_mm=300,
        height_mm=200,
        material_code_snapshot="K=A",
        flute_type_snapshot="B",
    )
    return lot


@pytest.fixture()
def move_batch_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router

    database = tmp_path / "p1-47c-move-batch.sqlite3"
    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as db:
        admin = User(
            username="p147c-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="P1-47C管理员",
            must_change_password=False,
        )
        operator = User(
            username="p147c-operator",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="P1-47C移货员工",
            must_change_password=False,
        )
        viewer = User(
            username="p147c-viewer",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="P1-47C只读员工",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=14700,
            customer_code="P147C",
            name="P1-47C测试客户",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([admin, operator, viewer, customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P147C-FG-001",
            customer_material_code="P147C-FG-001",
            product_name="五层加强纸箱",
            box_category="normal",
            box_style="A1",
            length_mm=500,
            width_mm=300,
            height_mm=200,
            default_material_code="K=A",
            flute_type="B",
        )
        floor1 = WarehouseFloor(
            floor_code="1F",
            floor_name="一楼",
            floor_number=1,
            construction_status="enabled",
        )
        floor3 = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼",
            floor_number=3,
            construction_status="enabled",
        )
        areas = [
            _published_area(floor1, area_code="TEMP-001"),
            _published_area(floor1, area_code="FIN-001"),
            _published_area(floor3, area_code="A1"),
            _published_area(floor3, area_code="BADTYPE", inventory_types=("semi_finished",)),
        ]
        draft_area = WarehouseArea(
            floor=floor3,
            area_code="DRAFT",
            area_name="未发布区域",
            construction_status="enabled",
        )
        draft_area.storage_policy = WarehouseAreaStoragePolicy(
            map_feature_id="zone-3f-draft",
            allowed_inventory_types_json='["finished"]',
            storage_layout="pallet_ground",
            status="draft",
            draft_map_revision="p1-47c-draft",
            version=1,
        )
        db.add_all([product, floor1, floor3, *areas, draft_area])
        db.flush()

        dispatch = _location(
            code="F1-DISPATCH-01",
            floor=1,
            area="TEMP-001",
            source_version="P1-25C",
            storage_type="temporary_aisle",
        )
        floor1_source = _location(
            code="1F-FG-SOURCE", floor=1, area="FIN-001", source_version="TWIN_V1"
        )
        floor1_target = _location(
            code="1F-FG-TARGET", floor=1, area="FIN-001", source_version="TWIN_V1"
        )
        floor1_target_2 = _location(
            code="1F-FG-TARGET-2", floor=1, area="FIN-001", source_version="TWIN_V1"
        )
        floor3_source = _location(
            code="3F-A1-SOURCE", floor=3, area="A1", source_version="V11"
        )
        floor3_target = _location(
            code="3F-A1-TARGET", floor=3, area="A1", source_version="V11"
        )
        floor3_target_2 = _location(
            code="3F-A1-TARGET-2", floor=3, area="A1", source_version="V11"
        )
        disabled_target = _location(
            code="3F-A1-DISABLED",
            floor=3,
            area="A1",
            source_version="V11",
            active=False,
        )
        unplaced_target = _location(
            code="3F-A1-UNPLACED",
            floor=3,
            area="A1",
            source_version="V11",
            placed=False,
        )
        rack_target = _location(
            code="3F-A1-RACK",
            floor=3,
            area="A1",
            source_version="V11",
            storage_type="rack",
        )
        wrong_type_target = _location(
            code="3F-BADTYPE-01",
            floor=3,
            area="BADTYPE",
            source_version="V11",
            warehouse_type="semi_finished",
        )
        draft_target = _location(
            code="3F-DRAFT-01", floor=3, area="DRAFT", source_version="TWIN_V1"
        )
        occupied_target = _location(
            code="3F-A1-OCCUPIED", floor=3, area="A1", source_version="V11"
        )
        db.add_all(
            [
                dispatch,
                floor1_source,
                floor1_target,
                floor1_target_2,
                floor3_source,
                floor3_target,
                floor3_target_2,
                disabled_target,
                unplaced_target,
                rack_target,
                wrong_type_target,
                draft_target,
                occupied_target,
            ]
        )
        db.flush()

        floor1_identity = load_warehouse_twin_published_floor_identity(1)
        floor1_plan = WarehouseGroundLayoutPlan(
            area_id=areas[1].id,
            status="published",
            target_slot_count=3,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision=str(floor1_identity["revision"]),
            published_map_revision=str(floor1_identity["revision"]),
            preview_fingerprint="a" * 64,
            version=1,
            publish_idempotency_key="p1-47c-fin-001-publish",
            publish_request_hash="b" * 64,
            updated_by=admin.id,
            published_by=admin.id,
            published_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(floor1_plan)
        db.flush()
        db.add_all(
            [
                WarehouseGroundLayoutSlot(
                    plan_id=floor1_plan.id,
                    location_id=location.id,
                    route_sequence=index,
                    row_no=1,
                    slot_no=index,
                    x_mm=Decimal(1000 + index * 1400),
                    y_mm=Decimal("1000"),
                    width_mm=1200,
                    depth_mm=1000,
                )
                for index, location in enumerate(
                    (floor1_source, floor1_target, floor1_target_2), start=1
                )
            ]
        )
        db.flush()

        system_lot = _finished_lot(
            number="FG-P147C-SYSTEM",
            location=dispatch,
            customer=customer,
            product=product,
            available=56,
            reserved=0,
            source_ref_id=14701,
        )
        loose_lot = _finished_lot(
            number="FG-P147C-LOOSE",
            location=dispatch,
            customer=customer,
            product=product,
            available=60,
            reserved=40,
            source_ref_id=14702,
        )
        normal_lot = _finished_lot(
            number="FG-P147C-NORMAL",
            location=floor1_source,
            customer=customer,
            product=product,
            available=20,
            reserved=0,
            source_ref_id=14703,
        )
        occupied_lot = _finished_lot(
            number="FG-P147C-OCCUPIED",
            location=occupied_target,
            customer=customer,
            product=product,
            available=1,
            reserved=0,
            source_ref_id=14704,
        )
        db.add_all([system_lot, loose_lot, normal_lot, occupied_lot])
        db.flush()

        system_pallet = InventoryPallet(
            pallet_code="PLT-F1-PC-14701",
            location_id=dispatch.id,
            location_occupancy_key="PRODUCTION_COMPLETION:14701",
            status="active",
            is_current=True,
            version=1,
            created_by=admin.id,
        )
        normal_pallet = InventoryPallet(
            pallet_code="PLT-P147C-NORMAL",
            location_id=floor1_source.id,
            location_occupancy_key="PRIMARY",
            status="active",
            is_current=True,
            version=1,
            created_by=admin.id,
        )
        occupied_pallet = InventoryPallet(
            pallet_code="PLT-P147C-OCCUPIED",
            location_id=occupied_target.id,
            location_occupancy_key="PRIMARY",
            status="active",
            is_current=True,
            version=1,
            created_by=admin.id,
        )
        db.add_all([system_pallet, normal_pallet, occupied_pallet])
        db.flush()
        db.add_all(
            [
                InventoryPalletItem(
                    pallet_id=system_pallet.id,
                    inventory_lot_id=system_lot.id,
                    customer_id=customer.id,
                    product_id=product.id,
                    inventory_code=product.product_code,
                    customer_name_snapshot=customer.name,
                    product_name=product.product_name,
                    item_type="finished",
                    quantity=Decimal("56"),
                    unit="boxes",
                    match_status="matched",
                ),
                InventoryPalletItem(
                    pallet_id=normal_pallet.id,
                    inventory_lot_id=normal_lot.id,
                    customer_id=customer.id,
                    product_id=product.id,
                    inventory_code=product.product_code,
                    customer_name_snapshot=customer.name,
                    product_name=product.product_name,
                    item_type="finished",
                    quantity=Decimal("20"),
                    unit="boxes",
                    match_status="matched",
                ),
                InventoryPalletItem(
                    pallet_id=occupied_pallet.id,
                    inventory_lot_id=occupied_lot.id,
                    customer_id=customer.id,
                    product_id=product.id,
                    inventory_code=product.product_code,
                    customer_name_snapshot=customer.name,
                    product_name=product.product_name,
                    item_type="finished",
                    quantity=Decimal("1"),
                    unit="boxes",
                    match_status="matched",
                ),
                InventoryReservation(
                    reservation_number="RS-P147C-LOOSE",
                    inventory_lot_id=loose_lot.id,
                    reservation_type="finished_order",
                    reserved_stock_quantity=40,
                    credited_requirement_quantity=40,
                    yield_factor=1,
                    status="active",
                    reserved_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    reservation_group_key="P147C-RESERVE-GROUP",
                    reservation_group_requested_quantity=40,
                    idempotency_key="p147c-reserve-loose",
                ),
            ]
        )
        db.commit()
        ids = {
            "system_pallet": system_pallet.id,
            "normal_pallet": normal_pallet.id,
            "system_lot": system_lot.id,
            "loose_lot": loose_lot.id,
            "normal_lot": normal_lot.id,
            "dispatch": dispatch.id,
            "floor1_source": floor1_source.id,
            "floor1_target": floor1_target.id,
            "floor1_target_2": floor1_target_2.id,
            "floor3_source": floor3_source.id,
            "floor3_target": floor3_target.id,
            "floor3_target_2": floor3_target_2.id,
            "disabled_target": disabled_target.id,
            "unplaced_target": unplaced_target.id,
            "rack_target": rack_target.id,
            "wrong_type_target": wrong_type_target.id,
            "draft_target": draft_target.id,
            "occupied_target": occupied_target.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids, database
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200, response.text


def _pallet_move(
    *, client_item_id: str, pallet_id: int, version: int, target: int,
    expected_target_layout_version: int = 1,
) -> dict[str, object]:
    return {
        "client_item_id": client_item_id,
        "operation": "pallet_move",
        "pallet_id": pallet_id,
        "expected_version": version,
        "target_location_id": target,
        "expected_target_layout_version": expected_target_layout_version,
    }


def _lot_transfer(
    *, client_item_id: str, lot_id: int, version: int, quantity: int, target: int,
    expected_target_layout_version: int = 1,
) -> dict[str, object]:
    return {
        "client_item_id": client_item_id,
        "operation": "lot_transfer",
        "lot_id": lot_id,
        "expected_version": version,
        "quantity": quantity,
        "target_location_id": target,
        "expected_target_layout_version": expected_target_layout_version,
    }


def _batch(key: str, *items: dict[str, object]) -> dict[str, object]:
    return {"idempotency_key": key, "confirmed": True, "items": list(items)}


def _remaining_reserved(db: Session) -> int:
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.status.in_(("active", "partial"))
        )
    ).all()
    return sum(
        int(row.reserved_stock_quantity or 0)
        - int(row.consumed_stock_quantity or 0)
        - int(row.released_stock_quantity or 0)
        for row in rows
    )


def _inventory_totals(db: Session) -> tuple[int, int, int, int, int]:
    return tuple(
        int(value or 0)
        for value in db.execute(
            select(
                func.sum(InventoryLot.quantity_available),
                func.sum(InventoryLot.quantity_reserved),
                func.sum(InventoryLot.quantity_consumed),
                func.sum(InventoryLot.quantity_damaged),
                func.sum(InventoryLot.quantity_scrapped),
            )
        ).one()
    )


def test_move_batch_requires_execute_permission_confirmation_and_disjoint_items(
    move_batch_app,
) -> None:
    app, _factory, ids, _database = move_batch_app
    valid_item = _pallet_move(
        client_item_id="row-1",
        pallet_id=ids["normal_pallet"],
        version=1,
        target=ids["floor3_target"],
    )
    with TestClient(app) as client:
        assert client.post(MOVE_BATCH_URL, json=_batch("not-logged-in", valid_item)).status_code == 401

        _login(client, "p147c-viewer")
        assert client.post(MOVE_BATCH_URL, json=_batch("no-execute", valid_item)).status_code == 403

        _login(client, "p147c-operator")
        unconfirmed = _batch("not-confirmed", valid_item)
        unconfirmed["confirmed"] = False
        assert client.post(MOVE_BATCH_URL, json=unconfirmed).status_code == 422

        mixed_fields = dict(valid_item)
        mixed_fields.update({"lot_id": ids["loose_lot"], "quantity": 1})
        assert client.post(MOVE_BATCH_URL, json=_batch("mixed-fields", mixed_fields)).status_code == 422

        duplicate_ids = _batch(
            "duplicate-client-item",
            valid_item,
            _lot_transfer(
                client_item_id="row-1",
                lot_id=ids["loose_lot"],
                version=1,
                quantity=1,
                target=ids["floor1_target"],
            ),
        )
        assert client.post(MOVE_BATCH_URL, json=duplicate_ids).status_code == 422


def test_transfer_claim_keeps_immutable_ground_occupancy_unchanged(move_batch_app):
    import importlib.util
    from sqlalchemy import text
    from app.services.warehouse_inventory import _claim_inventory_transfer_locations

    _app, factory, ids, _database = move_batch_app
    path = FRONTEND.parents[3] / "alembic/versions/jm71v8x9z60_delivery_ground_occupancy_restore.py"
    spec = importlib.util.spec_from_file_location("occupancy_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with factory() as db:
        lot = db.get(InventoryLot, ids["normal_lot"])
        detail = lot.finished_detail
        actor = db.scalar(select(User).where(User.username == "p147c-admin"))
        occupancy = WarehouseGroundOccupancy(pallet_id=ids["normal_pallet"],
            primary_location_id=ids["floor1_source"], customer_id=detail.owner_customer_id,
            product_id=detail.product_id, footprint_kind="single", capacity_quantity=999,
            created_by=actor.id)
        db.add(occupancy)
        db.commit()
        db.execute(text(migration._occupancy_guard_sql(allow_restore=True)))
        db.commit()
        before = db.execute(text("SELECT * FROM warehouse_ground_occupancies ORDER BY id")).all()
        _claim_inventory_transfer_locations(db, source_location_id=ids["floor1_source"],
            target_location_id=ids["floor1_target"], expected_source_layout_version=1,
            expected_target_layout_version=1)
        db.commit()
        assert db.execute(text("SELECT * FROM warehouse_ground_occupancies ORDER BY id")).all() == before


def test_pending_reset_rejects_pallet_stock_outside_the_confirmed_floor_scope(move_batch_app):
    app, factory, ids, _database = move_batch_app
    prefix = "/api/warehouse/twin-operations/pending-relocation"
    with factory() as db:
        db.get(WarehouseLocation, ids["floor1_target"]).warehouse_floor = 2
        db.get(InventoryLot, ids["normal_lot"]).warehouse_location_id = ids["floor1_target"]
        db.commit()
        totals = _inventory_totals(db)
    with TestClient(app) as client:
        _login(client, "p147c-admin")
        preview = client.get(prefix + "/preview").json()
        response = client.post(prefix + "/reset", json={
            "expected_fingerprint": preview["fingerprint"], "idempotency_key": "outside-scope", "confirmed": True,
        })
        assert response.status_code == 409, response.text
        assert "范围外" in response.json()["detail"]
    with factory() as db:
        assert _inventory_totals(db) == totals
        assert db.get(InventoryPallet, ids["normal_pallet"]).location_id == ids["floor1_source"]
        assert db.get(InventoryLot, ids["normal_lot"]).warehouse_location_id == ids["floor1_target"]
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 0
        assert db.scalar(select(WarehouseLocation.id).where(WarehouseLocation.location_code == "RECOUNT-PENDING")) is None


def test_pending_reset_and_partial_placement_keep_reservations_and_replay(move_batch_app):
    app, factory, ids, _database = move_batch_app
    import importlib.util
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    path = FRONTEND.parents[3] / "alembic/versions/rp06v8x9z65_recount_pending_location.py"
    spec = importlib.util.spec_from_file_location("recount_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with factory() as db:
        engine = db.get_bind()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
    prefix = "/api/warehouse/twin-operations/pending-relocation"
    with factory() as db:
        totals, reserved = _inventory_totals(db), _remaining_reserved(db)
    with TestClient(app) as client:
        _login(client, "p147c-viewer")
        assert client.get(prefix + "/preview").status_code == 403
        _login(client, "p147c-admin")
        preview = client.get(prefix + "/preview")
        assert preview.status_code == 200, preview.text
        reset = {"expected_fingerprint": preview.json()["fingerprint"],
                 "idempotency_key": "pending-reset", "confirmed": True}
        assert client.post(prefix + "/reset", json={**reset, "confirmed": False}).status_code == 422
        assert client.post(prefix + "/reset", json={**reset, "expected_fingerprint": "0" * 64}).status_code == 409
        response = client.post(prefix + "/reset", json=reset)
        assert response.status_code == 200, response.text
        assert client.post(prefix + "/reset", json=reset).json()["replayed"] is True
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="before downgrade"):
                    migration.downgrade()
        with factory() as db:
            # An ordinary unplaced location cannot inherit the recount exception.
            ordinary = WarehouseLocation(location_code="ORDINARY-UNPLACED", location_name="未完成",
                warehouse_type="shared", placement_status="unplaced", is_active=True)
            db.add(ordinary)
            db.commit()
            db.get(InventoryLot, ids["loose_lot"]).warehouse_location_id = ordinary.id
            with pytest.raises(IntegrityError, match="active placed location"):
                db.flush()
            db.rollback()
            pending_id = response.json()["pending_location_id"]
            with pytest.raises(IntegrityError, match="cannot represent"):
                db.execute(text("UPDATE warehouse_locations SET source_version='OTHER' WHERE id=:id"), {"id": pending_id})
            db.rollback()
        with factory() as db:
            lot = db.get(InventoryLot, ids["loose_lot"])
            amount = int(lot.quantity_available) + 1
            payload = {"location_id": ids["floor1_target"], "expected_layout_version": 1,
                       "expected_version": lot.version, "quantity": amount,
                       "idempotency_key": "pending-partial-place", "confirmed": True}
        url = f'/api/warehouse/twin-operations/pending-lots/{ids["loose_lot"]}/place'
        stale = client.post(url, json={**payload, "expected_address_version": 999999})
        assert stale.status_code == 409, stale.text
        bad = client.post(url, json={**payload, "quantity": 999999})
        assert bad.status_code == 409, bad.text
        placed = client.post(url, json=payload)
        assert placed.status_code == 200, placed.text
        assert client.post(url, json=payload).json()["replayed"] is True
        assert client.post(url, json={**payload, "quantity": amount + 1}).status_code == 409
        with factory() as db:
            whole = db.get(InventoryLot, ids["normal_lot"])
            whole_payload = {**payload, "location_id": ids["floor1_target_2"],
                             "expected_version": whole.version, "quantity": whole.quantity_available,
                             "idempotency_key": "pending-whole-place"}
        whole_response = client.post(f'/api/warehouse/twin-operations/pending-lots/{ids["normal_lot"]}/place', json=whole_payload)
        assert whole_response.status_code == 200, whole_response.text
    with factory() as db:
        assert _inventory_totals(db) == totals
        assert _remaining_reserved(db) == reserved
        assert db.get(InventoryLot, placed.json()["target_lot_id"]).warehouse_location_id == ids["floor1_target"]


def test_batch_moves_dispatch_system_pallet_and_partial_lot_with_full_conservation(
    move_batch_app,
) -> None:
    app, factory, ids, _database = move_batch_app
    with factory() as db:
        before_totals = _inventory_totals(db)
        before_reserved = _remaining_reserved(db)
        loose = db.get(InventoryLot, ids["loose_lot"])
        assert loose is not None
        source_date = loose.stock_date
        source_ref = (loose.source_ref_type, loose.source_ref_id)

    payload = _batch(
        "move-batch-success-001",
        _pallet_move(
            client_item_id="dispatch-system-pallet",
            pallet_id=ids["system_pallet"],
            version=1,
            target=ids["floor3_target"],
        ),
        _lot_transfer(
            client_item_id="partial-dispatch-lot",
            lot_id=ids["loose_lot"],
            version=1,
            quantity=80,
            target=ids["floor1_target"],
        ),
    )
    with TestClient(app) as client:
        _login(client, "p147c-operator")
        response = client.post(MOVE_BATCH_URL, json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["idempotent_replay"] is False
        assert body["batch_id"]
        assert len(body["request_hash"]) == 64
        assert {item["client_item_id"] for item in body["items"]} == {
            "dispatch-system-pallet",
            "partial-dispatch-lot",
        }
        assert {item["operation"] for item in body["items"]} == {
            "pallet_move",
            "lot_transfer",
        }

    with factory() as db:
        pallet = db.get(InventoryPallet, ids["system_pallet"])
        system_lot = db.get(InventoryLot, ids["system_lot"])
        loose = db.get(InventoryLot, ids["loose_lot"])
        transfer = db.scalar(
            select(InventoryLotTransfer).where(
                InventoryLotTransfer.source_lot_id == ids["loose_lot"]
            )
        )
        assert pallet is not None and system_lot is not None and loose is not None
        assert pallet.location_id == ids["floor3_target"]
        assert pallet.location_occupancy_key == "PRIMARY"
        assert system_lot.warehouse_location_id == ids["floor3_target"]
        assert transfer is not None
        target = db.get(InventoryLot, transfer.target_lot_id)
        assert target is not None
        assert (loose.quantity_available, loose.quantity_reserved) == (0, 20)
        assert (target.quantity_available, target.quantity_reserved) == (60, 20)
        assert target.warehouse_location_id == ids["floor1_target"]
        assert target.stock_date == source_date
        assert (target.source_ref_type, target.source_ref_id) == source_ref
        assert _inventory_totals(db) == before_totals
        assert _remaining_reserved(db) == before_reserved == 40
        location_movements = list(
            db.scalars(
                select(InventoryLocationMovement).order_by(
                    InventoryLocationMovement.id
                )
            )
        )
        assert len(location_movements) == 2
        whole_pallet_moves = [
            movement
            for movement in location_movements
            if movement.movement_type == "move"
        ]
        target_pallet_creations = [
            movement
            for movement in location_movements
            if movement.movement_type == "create"
        ]
        assert len(whole_pallet_moves) == 1
        assert (
            whole_pallet_moves[0].pallet_id,
            whole_pallet_moves[0].from_location_id,
            whole_pallet_moves[0].to_location_id,
        ) == (
            ids["system_pallet"],
            ids["dispatch"],
            ids["floor3_target"],
        )
        assert len(target_pallet_creations) == 1
        target_pallet_item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.inventory_lot_id == target.id
            )
        )
        assert target_pallet_item is not None
        assert target_pallet_creations[0].pallet_id == target_pallet_item.pallet_id
        assert target_pallet_creations[0].from_location_id is None
        assert target_pallet_creations[0].to_location_id == ids["floor1_target"]
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 1


def test_exact_batch_replay_is_stable_and_same_key_different_payload_conflicts(
    move_batch_app,
) -> None:
    app, factory, ids, _database = move_batch_app
    payload = _batch(
        "move-batch-replay-001",
        _pallet_move(
            client_item_id="row-1",
            pallet_id=ids["normal_pallet"],
            version=1,
            target=ids["floor3_target"],
        ),
    )
    with TestClient(app) as client:
        _login(client, "p147c-operator")
        first = client.post(MOVE_BATCH_URL, json=payload)
        assert first.status_code == 200, first.text
        replay = client.post(MOVE_BATCH_URL, json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["batch_id"] == first.json()["batch_id"]
        assert replay.json()["request_hash"] == first.json()["request_hash"]

        changed = _batch(
            "move-batch-replay-001",
            _pallet_move(
                client_item_id="row-1",
                pallet_id=ids["normal_pallet"],
                version=1,
                target=ids["floor3_target_2"],
            ),
        )
        conflict = client.post(MOVE_BATCH_URL, json=changed)
        assert conflict.status_code == 409, conflict.text

    with factory() as db:
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 1
        pallet = db.get(InventoryPallet, ids["normal_pallet"])
        assert pallet is not None
        assert (pallet.location_id, pallet.version) == (ids["floor3_target"], 2)


def test_same_batch_key_cannot_be_replayed_by_another_operator_or_leak_result(
    move_batch_app,
) -> None:
    app, factory, ids, _database = move_batch_app
    payload = _batch(
        "move-batch-cross-operator-001",
        _pallet_move(
            client_item_id="cross-operator-row",
            pallet_id=ids["normal_pallet"],
            version=1,
            target=ids["floor3_target"],
        ),
    )
    with TestClient(app) as client:
        _login(client, "p147c-operator")
        first = client.post(MOVE_BATCH_URL, json=payload)
        assert first.status_code == 200, first.text
        first_result = first.json()

        _login(client, "p147c-admin")
        denied = client.post(MOVE_BATCH_URL, json=payload)
        assert denied.status_code == 409, denied.text
        assert set(denied.json()) == {"detail"}
        assert "items" not in denied.json()
        assert first_result["request_hash"] not in denied.text
        assert first_result["batch_id"] not in denied.text

    with factory() as db:
        pallet = db.get(InventoryPallet, ids["normal_pallet"])
        assert pallet is not None
        assert (pallet.location_id, pallet.version) == (ids["floor3_target"], 2)
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 1
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0
        assert (
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action_code
                    == "warehouse.movement_batch.confirmed"
                )
            )
            == 1
        )


def test_selected_customer_scope_denies_other_customer_batch_without_writes(
    move_batch_app,
) -> None:
    app, factory, ids, _database = move_batch_app
    with factory() as db:
        operator = db.scalar(
            select(User).where(User.username == "p147c-operator")
        )
        assert operator is not None
        operator.customer_access_mode = "selected"
        db.commit()

    payload = _batch(
        "move-batch-customer-scope-denied",
        _pallet_move(
            client_item_id="out-of-scope-pallet",
            pallet_id=ids["normal_pallet"],
            version=1,
            target=ids["floor3_target"],
        ),
    )
    with TestClient(app) as client:
        _login(client, "p147c-operator")
        denied = client.post(MOVE_BATCH_URL, json=payload)
        assert denied.status_code == 403, denied.text

    with factory() as db:
        pallet = db.get(InventoryPallet, ids["normal_pallet"])
        lot = db.get(InventoryLot, ids["normal_lot"])
        assert pallet is not None and lot is not None
        assert (pallet.location_id, pallet.version) == (ids["floor1_source"], 1)
        assert (lot.warehouse_location_id, lot.version) == (
            ids["floor1_source"],
            1,
        )
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 0
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0
        assert (
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action_code
                    == "warehouse.movement_batch.confirmed"
                )
            )
            == 0
        )


def test_frozen_lot_transfer_is_rejected_without_writes(move_batch_app) -> None:
    app, factory, ids, _database = move_batch_app
    with factory() as db:
        lot = db.get(InventoryLot, ids["loose_lot"])
        assert lot is not None
        lot.status = "frozen"
        db.commit()

    payload = _batch(
        "move-batch-frozen-lot",
        _lot_transfer(
            client_item_id="frozen-lot",
            lot_id=ids["loose_lot"],
            version=1,
            quantity=10,
            target=ids["floor1_target"],
        ),
    )
    with TestClient(app) as client:
        _login(client, "p147c-operator")
        denied = client.post(MOVE_BATCH_URL, json=payload)
        assert denied.status_code == 409, denied.text

    with factory() as db:
        lot = db.get(InventoryLot, ids["loose_lot"])
        assert lot is not None
        assert lot.status == "frozen"
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (
            60,
            40,
            1,
        )
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 0
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0
        assert (
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action_code
                    == "warehouse.movement_batch.confirmed"
                )
            )
            == 0
        )


@pytest.mark.parametrize(
    "invalid_state",
    ["closed_pallet", "non_finished_item", "lot_location_drift"],
)
def test_invalid_official_pallet_state_is_rejected_without_writes(
    move_batch_app,
    invalid_state: str,
) -> None:
    app, factory, ids, _database = move_batch_app
    with factory() as db:
        pallet = db.get(InventoryPallet, ids["normal_pallet"])
        lot = db.get(InventoryLot, ids["normal_lot"])
        item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.pallet_id == ids["normal_pallet"]
            )
        )
        assert pallet is not None and lot is not None and item is not None

        if invalid_state == "closed_pallet":
            pallet.status = "closed"
        elif invalid_state == "non_finished_item":
            item.item_type = "semi_finished"
        else:
            lot.warehouse_location_id = ids["floor3_source"]
        db.commit()

        expected_pallet_state = (
            pallet.location_id,
            pallet.version,
            pallet.status,
        )
        expected_lot_state = (
            lot.warehouse_location_id,
            lot.version,
            lot.quantity_available,
            lot.quantity_reserved,
        )
        expected_item_type = item.item_type

    payload = _batch(
        f"move-batch-invalid-official-{invalid_state}",
        _pallet_move(
            client_item_id=f"invalid-official-{invalid_state}",
            pallet_id=ids["normal_pallet"],
            version=1,
            target=ids["floor3_target"],
        ),
    )
    with TestClient(app) as client:
        _login(client, "p147c-operator")
        denied = client.post(MOVE_BATCH_URL, json=payload)
        assert denied.status_code == 409, denied.text

    with factory() as db:
        pallet = db.get(InventoryPallet, ids["normal_pallet"])
        lot = db.get(InventoryLot, ids["normal_lot"])
        item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.pallet_id == ids["normal_pallet"]
            )
        )
        assert pallet is not None and lot is not None and item is not None
        assert (pallet.location_id, pallet.version, pallet.status) == (
            expected_pallet_state
        )
        assert (
            lot.warehouse_location_id,
            lot.version,
            lot.quantity_available,
            lot.quantity_reserved,
        ) == expected_lot_state
        assert item.item_type == expected_item_type
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 0
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0
        assert (
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action_code
                    == "warehouse.movement_batch.confirmed"
                )
            )
            == 0
        )


def test_snapshot_mixed_and_non_finished_pallets_are_rejected_without_writes(
    move_batch_app,
) -> None:
    app, factory, ids, _database = move_batch_app
    with factory() as db:
        customer = db.scalar(
            select(Customer).where(Customer.customer_code == "P147C")
        )
        product = db.scalar(
            select(Product).where(Product.product_code == "P147C-FG-001")
        )
        admin = db.scalar(select(User).where(User.username == "p147c-admin"))
        assert customer is not None and product is not None and admin is not None

        snapshot_source = _location(
            code="1F-FG-SNAPSHOT-SOURCE",
            floor=1,
            area="FG",
            source_version="TWIN_V1",
        )
        mixed_source = _location(
            code="1F-FG-MIXED-SOURCE",
            floor=1,
            area="FG",
            source_version="TWIN_V1",
        )
        non_finished_source = _location(
            code="1F-FG-NONFINISHED-SOURCE",
            floor=1,
            area="FG",
            source_version="TWIN_V1",
        )
        db.add_all([snapshot_source, mixed_source, non_finished_source])
        db.flush()

        mixed_lot = _finished_lot(
            number="FG-P147C-MIXED-OFFICIAL",
            location=mixed_source,
            customer=customer,
            product=product,
            available=5,
            reserved=0,
            source_ref_id=14705,
        )
        non_finished_lot = InventoryLot(
            lot_number="SF-P147C-NONFINISHED",
            inventory_type="semi_finished",
            warehouse_location_id=non_finished_source.id,
            quantity_available=5,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            source_ref_type="manual",
            source_ref_id=14706,
            stock_date=date(2026, 8, 1),
            stock_date_accuracy="exact",
            stock_date_original_text="2026-08-01",
            last_movement_at=datetime.now(timezone.utc).replace(tzinfo=None),
            version=1,
        )
        db.add_all([mixed_lot, non_finished_lot])
        db.flush()

        snapshot_pallet = InventoryPallet(
            pallet_code="PLT-P147C-SNAPSHOT",
            location_id=snapshot_source.id,
            location_occupancy_key="PRIMARY",
            status="active",
            is_current=True,
            version=1,
            created_by=admin.id,
        )
        mixed_pallet = InventoryPallet(
            pallet_code="PLT-P147C-MIXED",
            location_id=mixed_source.id,
            location_occupancy_key="PRIMARY",
            status="active",
            is_current=True,
            version=1,
            created_by=admin.id,
        )
        non_finished_pallet = InventoryPallet(
            pallet_code="PLT-P147C-NONFINISHED",
            location_id=non_finished_source.id,
            location_occupancy_key="PRIMARY",
            status="active",
            is_current=True,
            version=1,
            created_by=admin.id,
        )
        db.add_all([snapshot_pallet, mixed_pallet, non_finished_pallet])
        db.flush()

        def pallet_item(
            *,
            pallet: InventoryPallet,
            item_type: str,
            inventory_lot_id: int | None,
            code_suffix: str,
        ) -> InventoryPalletItem:
            return InventoryPalletItem(
                pallet_id=pallet.id,
                inventory_lot_id=inventory_lot_id,
                customer_id=customer.id,
                product_id=product.id,
                inventory_code=f"P147C-{code_suffix}",
                customer_name_snapshot=customer.name,
                product_name=product.product_name,
                item_type=item_type,
                quantity=Decimal("5"),
                unit="boxes" if item_type == "finished" else "sheets",
                match_status="matched",
            )

        db.add_all(
            [
                pallet_item(
                    pallet=snapshot_pallet,
                    item_type="finished",
                    inventory_lot_id=None,
                    code_suffix="SNAPSHOT",
                ),
                pallet_item(
                    pallet=mixed_pallet,
                    item_type="finished",
                    inventory_lot_id=mixed_lot.id,
                    code_suffix="MIXED-OFFICIAL",
                ),
                pallet_item(
                    pallet=mixed_pallet,
                    item_type="finished",
                    inventory_lot_id=None,
                    code_suffix="MIXED-SNAPSHOT",
                ),
                pallet_item(
                    pallet=non_finished_pallet,
                    item_type="semi_finished",
                    inventory_lot_id=non_finished_lot.id,
                    code_suffix="NONFINISHED",
                ),
            ]
        )
        db.commit()
        pallet_ids = {
            "snapshot": snapshot_pallet.id,
            "mixed": mixed_pallet.id,
            "non_finished": non_finished_pallet.id,
        }
        source_ids = {
            "snapshot": snapshot_source.id,
            "mixed": mixed_source.id,
            "non_finished": non_finished_source.id,
        }
        official_lot_locations = {
            mixed_lot.id: mixed_source.id,
            non_finished_lot.id: non_finished_source.id,
        }

    with TestClient(app) as client:
        _login(client, "p147c-operator")
        for kind, pallet_id in pallet_ids.items():
            response = client.post(
                MOVE_BATCH_URL,
                json=_batch(
                    f"move-batch-invalid-{kind}",
                    _pallet_move(
                        client_item_id=f"invalid-{kind}",
                        pallet_id=pallet_id,
                        version=1,
                        target=ids["floor3_target"],
                    ),
                ),
            )
            assert response.status_code == 409, (kind, response.text)

    with factory() as db:
        for kind, pallet_id in pallet_ids.items():
            pallet = db.get(InventoryPallet, pallet_id)
            assert pallet is not None
            assert (pallet.location_id, pallet.version) == (source_ids[kind], 1)
        for lot_id, source_id in official_lot_locations.items():
            lot = db.get(InventoryLot, lot_id)
            assert lot is not None
            assert (lot.warehouse_location_id, lot.version) == (source_id, 1)
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 0
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0
        assert (
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action_code
                    == "warehouse.movement_batch.confirmed"
                )
            )
            == 0
        )


@pytest.mark.parametrize(
    "target_key",
    [
        "disabled_target",
        "unplaced_target",
        "wrong_type_target",
        "draft_target",
    ],
)
def test_invalid_second_target_rolls_back_the_entire_batch(
    move_batch_app, target_key: str
) -> None:
    app, factory, ids, _database = move_batch_app
    payload = _batch(
        f"move-batch-rollback-{target_key}",
        _pallet_move(
            client_item_id="valid-first",
            pallet_id=ids["normal_pallet"],
            version=1,
            target=ids["floor3_target"],
        ),
        _lot_transfer(
            client_item_id="invalid-second",
            lot_id=ids["loose_lot"],
            version=1,
            quantity=10,
            target=ids[target_key],
        ),
    )
    with TestClient(app) as client:
        _login(client, "p147c-operator")
        response = client.post(MOVE_BATCH_URL, json=payload)
        assert response.status_code == 409, response.text

    with factory() as db:
        pallet = db.get(InventoryPallet, ids["normal_pallet"])
        lot = db.get(InventoryLot, ids["normal_lot"])
        loose = db.get(InventoryLot, ids["loose_lot"])
        assert pallet is not None and lot is not None and loose is not None
        assert (pallet.location_id, pallet.version) == (ids["floor1_source"], 1)
        assert (lot.warehouse_location_id, lot.version) == (ids["floor1_source"], 1)
        assert (loose.quantity_available, loose.quantity_reserved, loose.version) == (60, 40, 1)
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 0
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0


def test_rack_target_accepts_lot_transfer_but_rejects_whole_pallet(
    move_batch_app,
) -> None:
    app, factory, ids, _database = move_batch_app
    with TestClient(app) as client:
        _login(client, "p147c-operator")
        blocked = client.post(
            MOVE_BATCH_URL,
            json=_batch(
                "p1126-whole-pallet-rack-blocked",
                _pallet_move(
                    client_item_id="whole-pallet-rack",
                    pallet_id=ids["normal_pallet"],
                    version=1,
                    target=ids["rack_target"],
                ),
            ),
        )
        assert blocked.status_code == 409, blocked.text
        moved = client.post(
            MOVE_BATCH_URL,
            json=_batch(
                "p1126-loose-lot-rack-supported",
                _lot_transfer(
                    client_item_id="loose-lot-rack",
                    lot_id=ids["loose_lot"],
                    version=1,
                    quantity=10,
                    target=ids["rack_target"],
                ),
            ),
        )
        assert moved.status_code == 200, moved.text

    result = moved.json()["items"][0]
    with factory() as db:
        pallet = db.get(InventoryPallet, ids["normal_pallet"])
        target_lot = db.get(InventoryLot, result["target_lot_id"])
        assert pallet is not None and target_lot is not None
        assert (pallet.location_id, pallet.version) == (ids["floor1_source"], 1)
        assert target_lot.warehouse_location_id == ids["rack_target"]
        assert target_lot.pallet_item is None


def _publish_mixed_target_plan(db, ids):
    area = db.scalar(select(WarehouseArea).join(WarehouseFloor).where(WarehouseFloor.floor_number == 3, WarehouseArea.area_code == "A1"))
    plan = WarehouseGroundLayoutPlan(area_id=area.id, status="published", target_slot_count=2,
        numbering_origin="south", row_direction="from_aisle_inward", slot_direction="left_to_right",
        row_start_no=1, slot_start_no=1, draft_map_revision=area.storage_policy.published_map_revision,
        published_map_revision=area.storage_policy.published_map_revision, preview_fingerprint="c"*64,
        version=1, publish_idempotency_key="mixed-target-plan", publish_request_hash="d"*64,
        updated_by=db.scalar(select(User.id).where(User.username == "p147c-admin")),
        published_by=db.scalar(select(User.id).where(User.username == "p147c-admin")),
        published_at=datetime.now(timezone.utc).replace(tzinfo=None))
    db.add(plan)
    db.flush()
    for index, name in enumerate(("occupied_target", "floor3_target"), 1):
        db.add(WarehouseGroundLayoutSlot(plan_id=plan.id, location_id=ids[name], route_sequence=index,
            row_no=1, slot_no=index, x_mm=Decimal(1000+index*1400), y_mm=Decimal(1000), width_mm=1200, depth_mm=1000))
    db.flush()


def test_single_product_joins_occupied_pallet_then_all_products_move_once(move_batch_app):
    app, factory, ids, _ = move_batch_app
    with factory() as db:
        source = db.get(InventoryLot, ids["normal_lot"])
        _publish_mixed_target_plan(db, ids)
        product = Product(customer_id=source.finished_detail.owner_customer_id, customer_material_code="MIX-NEW",
                          product_code="MIX-NEW", product_name="另一款产品", box_category="normal",
                          box_style="A1", length_mm=500, width_mm=300, height_mm=200,
                          default_material_code="K=A", flute_type="B")
        db.add(product)
        db.flush()
        source.finished_detail.product_id = product.id
        source.finished_detail.inventory_code_snapshot = product.product_code
        source.pallet_item.product_id = product.id
        source.estimated_unit_cost_snapshot = Decimal("3.2100")
        source.cost_snapshot_source = "material_quote_area"
        source.cost_snapshot_detail_json = '{"basis":"test frozen cost"}'
        totals = _inventory_totals(db)
        db.commit()
    first = _batch("single-into-occupied", _lot_transfer(
        client_item_id="one", lot_id=ids["normal_lot"], version=1,
        quantity=20, target=ids["occupied_target"]))
    with TestClient(app) as client:
        _login(client, "p147c-admin")
        response = client.post(MOVE_BATCH_URL, json=first)
        assert response.status_code == 200, response.text
        moved_lot_id = response.json()["items"][0]["target_lot_id"]
        assert client.post(MOVE_BATCH_URL, json=first).json()["items"] == response.json()["items"]
        with factory() as db:
            target = db.scalar(select(InventoryPallet).where(
                InventoryPallet.location_id == ids["occupied_target"], InventoryPallet.is_current.is_(True)))
            lot_ids = [item.inventory_lot_id for item in target.items]
            assert len(lot_ids) == 2
            assert len({item.product_id for item in target.items}) == 2
            assert _inventory_totals(db) == totals
            pallet_id, version = target.id, target.version
        moved = client.post(MOVE_BATCH_URL, json=_batch("mixed-whole-to-empty", _pallet_move(
            client_item_id="whole", pallet_id=pallet_id, version=version, target=ids["floor3_target"])))
        assert moved.status_code == 200, moved.text
    with factory() as db:
        assert db.get(InventoryPallet, pallet_id).location_id == ids["floor3_target"]
        assert all(db.get(InventoryLot, lot_id).warehouse_location_id == ids["floor3_target"] for lot_id in lot_ids)
        assert _inventory_totals(db) == totals
        moved_lot = db.get(InventoryLot, moved_lot_id)
        assert moved_lot.estimated_unit_cost_snapshot == Decimal("3.2100")
        assert moved_lot.cost_snapshot_detail_json == '{"basis":"test frozen cost"}'


def test_whole_pallet_still_rejects_occupied_target(move_batch_app):
    app, factory, ids, _ = move_batch_app
    with TestClient(app) as client:
        _login(client, "p147c-admin")
        response = client.post(MOVE_BATCH_URL, json=_batch("whole-occupied", _pallet_move(
            client_item_id="whole", pallet_id=ids["normal_pallet"], version=1, target=ids["occupied_target"])))
        assert response.status_code == 409, response.text
    with factory() as db:
        assert db.get(InventoryPallet, ids["normal_pallet"]).location_id == ids["floor1_source"]
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 0


def test_multiple_single_products_can_share_target_in_one_batch(move_batch_app):
    app, factory, ids, _ = move_batch_app
    with factory() as db:
        _publish_mixed_target_plan(db, ids)
        db.commit()
    with TestClient(app) as client:
        _login(client, "p147c-admin")
        response = client.post(MOVE_BATCH_URL, json=_batch("two-into-one",
            _lot_transfer(client_item_id="first", lot_id=ids["normal_lot"], version=1, quantity=10, target=ids["occupied_target"]),
            _lot_transfer(client_item_id="second", lot_id=ids["loose_lot"], version=1, quantity=10, target=ids["occupied_target"])))
        assert response.status_code == 200, response.text
    with factory() as db:
        for item in response.json()["items"]:
            assert db.get(InventoryLot, item["target_lot_id"]).warehouse_location_id == ids["occupied_target"]


def test_warehouse_main_map_exposes_role_scoped_cost_entry():
    source = FRONTEND.read_text(encoding="utf-8")
    assert 'setCanViewInventoryCost(["admin", "boss"].includes(value.user.role))' in source
    assert '{canViewInventoryCost && <a' in source
    assert 'href="/factory-twin-assets/warehouse-costs.html"' in source
    assert '>库存成本</a>' in source


@pytest.mark.parametrize("target_frozen", [False, True])
def test_occupied_target_failure_preserves_permissions_quality_and_atomicity(move_batch_app, target_frozen):
    app, factory, ids, _ = move_batch_app
    with factory() as db:
        target_lot = db.scalar(select(InventoryLot).where(InventoryLot.warehouse_location_id == ids["occupied_target"]))
        if target_frozen:
            target_lot.status = "frozen"
        else:
            # Non-admin may co-locate the same SKU, but the existing different-product
            # mixing permission remains enforced by the transfer service.
            target_lot.finished_detail.inventory_code_snapshot = "DIFFERENT-SKU"
        before = _inventory_totals(db)
        db.commit()
    with TestClient(app) as client:
        _login(client, "p147c-admin" if target_frozen else "p147c-operator")
        response = client.post(MOVE_BATCH_URL, json=_batch("occupied-failure",
            _lot_transfer(client_item_id="valid-first", lot_id=ids["normal_lot"], version=1, quantity=10, target=ids["floor1_target"]),
            _lot_transfer(client_item_id="blocked-second", lot_id=ids["loose_lot"], version=1, quantity=10, target=ids["occupied_target"])))
        assert response.status_code == 409, response.text
    with factory() as db:
        assert _inventory_totals(db) == before
        assert db.get(InventoryLot, ids["normal_lot"]).version == 1
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0


def test_stale_same_target_and_duplicate_source_are_rejected_without_writes(
    move_batch_app,
) -> None:
    app, factory, ids, _database = move_batch_app
    cases = [
        _batch(
            "move-batch-stale",
            _pallet_move(
                client_item_id="stale",
                pallet_id=ids["normal_pallet"],
                version=99,
                target=ids["floor3_target"],
            ),
        ),
        _batch(
            "move-batch-same-target",
            _pallet_move(
                client_item_id="same-target",
                pallet_id=ids["normal_pallet"],
                version=1,
                target=ids["floor1_source"],
            ),
        ),
        _batch(
            "move-batch-duplicate-source",
            _pallet_move(
                client_item_id="duplicate-a",
                pallet_id=ids["normal_pallet"],
                version=1,
                target=ids["floor3_target"],
            ),
            _pallet_move(
                client_item_id="duplicate-b",
                pallet_id=ids["normal_pallet"],
                version=1,
                target=ids["floor3_target_2"],
            ),
        ),
    ]
    with TestClient(app) as client:
        _login(client, "p147c-operator")
        for payload in cases:
            response = client.post(MOVE_BATCH_URL, json=payload)
            assert response.status_code in {409, 422}, response.text

    with factory() as db:
        pallet = db.get(InventoryPallet, ids["normal_pallet"])
        assert pallet is not None
        assert (pallet.location_id, pallet.version) == (ids["floor1_source"], 1)
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == 0
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0


def test_p1_47c_fixture_never_uses_the_formal_database(move_batch_app) -> None:
    _app, factory, _ids, database = move_batch_app
    assert database.name == "p1-47c-move-batch.sqlite3"
    assert database.parent.name.startswith("test_")
    assert "carton_erp.sqlite3" not in str(database)
    with factory() as db:
        assert Path(str(db.get_bind().url.database)).resolve() == database.resolve()


def test_frontend_exposes_execute_scoped_three_level_move_draft_once_only() -> None:
    source = FRONTEND.read_text(encoding="utf-8")
    move_helper = FRONTEND.with_name("warehouseMoveDraft.mjs").read_text(
        encoding="utf-8"
    )
    assert 'value.permissions.includes("warehouse.execute")' in source
    assert 'setMapMode("move")' in source
    assert ">移货</button>" in source and ">盘点</button>" in source
    assert "楼层" in source and "区域" in source and "具体货位" in source
    assert source.count('"/api/warehouse/twin-operations/move-batches"') == 1
    assert "buildMoveBatchPayload(key, drafts)" in source
    assert "moveSubmitLock.current = true" in source
    assert "export function buildMoveBatchPayload" in move_helper
    assert "idempotency_key: idempotencyKey" in move_helper
    assert "client_item_id: draft.client_item_id" in move_helper
    assert "operation: \"pallet_move\"" in source
    assert "operation: \"lot_transfer\"" in source
    assert "撤销" in source and "一次确认" in source
    assert "P1_47D_ENABLED" not in source
    assert 'value.permissions.includes("warehouse.stocktake.submit")' in source
    assert source.count('"/api/warehouse/twin-operations/stocktake-batches"') == 1
