from __future__ import annotations

import json
import threading
from collections.abc import Generator
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.product import Product
from app.models.stocktake import StocktakeItem, StocktakeOrder
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    Floor3LocationLayout,
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    SemiFinishedLotAllowedProduct,
    SemiFinishedInventoryDetail,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)


URL = "/api/warehouse/twin-operations/stocktake-batches"
FRONTEND = (
    Path(__file__).resolve().parents[1]
    / "factory_twin"
    / "frontend"
    / "src"
    / "WarehouseTwinApp.tsx"
)
STOCKTAKE_DRAFT = FRONTEND.with_name("warehouseStocktakeDraft.mjs")


def _published_area(
    floor: WarehouseFloor,
    *,
    code: str,
    inventory_types: tuple[str, ...],
    status: str = "published",
) -> WarehouseArea:
    area = WarehouseArea(
        floor=floor,
        area_code=code,
        area_name=f"{floor.floor_code}-{code}",
        construction_status="enabled",
    )
    area.storage_policy = WarehouseAreaStoragePolicy(
        map_feature_id=f"zone-{floor.floor_code.lower()}-{code.lower()}",
        allowed_inventory_types_json=json.dumps(list(inventory_types)),
        storage_layout="pallet_ground",
        status=status,
        published_map_revision="p1-47d-formal-map" if status == "published" else None,
        draft_map_revision="p1-47d-draft-map" if status == "draft" else None,
        version=1,
    )
    return area


def _location(
    *,
    code: str,
    floor: int,
    area: str,
    warehouse_type: str,
    source_version: str,
    active: bool = True,
    placed: bool = True,
    mapped: bool = True,
) -> WarehouseLocation:
    row = WarehouseLocation(
        location_code=code,
        location_name=code,
        warehouse_type=warehouse_type,
        warehouse_floor=floor,
        area_code=area,
        storage_type="ground",
        is_active=active,
        placement_status="placed" if placed else "unplaced",
        source_version=source_version,
    )
    if mapped:
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
    available: int = 10,
    reserved: int = 0,
    status: str = "active",
    source_type: str = "stocktake",
    damaged: int = 0,
    scrapped: int = 0,
) -> InventoryLot:
    lot = InventoryLot(
        lot_number=number,
        inventory_type="finished",
        warehouse_location_id=location.id,
        quantity_available=available,
        quantity_reserved=reserved,
        quantity_consumed=0,
        quantity_damaged=damaged,
        quantity_scrapped=scrapped,
        unit="boxes",
        status=status,
        source_type=source_type,
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


def _semi_lot(
    *,
    number: str,
    location: WarehouseLocation,
    customer: Customer,
    available: int = 10,
) -> InventoryLot:
    lot = InventoryLot(
        lot_number=number,
        inventory_type="semi_finished",
        warehouse_location_id=location.id,
        quantity_available=available,
        quantity_reserved=0,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit="sheets",
        status="active",
        source_type="stocktake",
        stock_date=date(2026, 8, 1),
        stock_date_accuracy="exact",
        stock_date_original_text="2026-08-01",
        last_movement_at=datetime.now(timezone.utc).replace(tzinfo=None),
        version=1,
    )
    lot.semi_finished_detail = SemiFinishedInventoryDetail(
        owner_customer_id=customer.id,
        owner_customer_name_snapshot=customer.name,
        material_code_snapshot="K=A",
        normalized_material_code="K=A",
        layer_count=3,
        flute_type="B",
        board_length_mm=800,
        board_width_mm=600,
        component_type="whole",
        pieces_per_box=1,
        stock_yield_per_sheet=1,
        sheet_type="raw_board",
    )
    return lot


@pytest.fixture()
def stocktake_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router

    database = tmp_path / "p1-47d-stocktake-batch.sqlite3"
    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as db:
        admin = User(
            username="p147d-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="P1-47D 管理员",
            must_change_password=False,
        )
        operator = User(
            username="p147d-operator",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="P1-47D 盘点员",
            must_change_password=False,
        )
        other = User(
            username="p147d-other",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="另一盘点员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=14740,
            customer_code="P147D",
            name="P1-47D 客户",
            payment_term_days=0,
            credit_limit=0,
        )
        other_customer = Customer(
            customer_number=14741,
            customer_code="P147D-X",
            name="P1-47D 其他客户",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([admin, operator, other, customer, other_customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P147D-FG",
            customer_material_code="P147D-FG",
            product_name="五层加强纸箱",
            box_category="normal",
            box_style="A1",
            length_mm=500,
            width_mm=300,
            height_mm=200,
            default_material_code="K=A",
            flute_type="B",
            layer_count=3,
            report_length_mm=800,
            report_width_mm=600,
        )
        other_product = Product(
            customer_id=other_customer.id,
            product_code="P147D-X-FG",
            customer_material_code="P147D-X-FG",
            product_name="其他客户纸箱",
            box_category="normal",
            box_style="A1",
            default_material_code="A=B",
            flute_type="B",
        )
        floor1 = WarehouseFloor(
            floor_code="1F",
            floor_name="一楼",
            floor_number=1,
            construction_status="enabled",
        )
        floor2 = WarehouseFloor(
            floor_code="2F",
            floor_name="二楼",
            floor_number=2,
            construction_status="enabled",
        )
        floor3 = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼",
            floor_number=3,
            construction_status="enabled",
        )
        areas = [
            _published_area(floor1, code="FG", inventory_types=("finished",)),
            _published_area(floor1, code="SEMI", inventory_types=("semi_finished",)),
            _published_area(floor1, code="DISPATCH", inventory_types=("finished",)),
            _published_area(floor3, code="FG", inventory_types=("finished",)),
            _published_area(floor2, code="FG", inventory_types=("finished",)),
            _published_area(
                floor3,
                code="DRAFT",
                inventory_types=("finished",),
                status="draft",
            ),
        ]
        db.add_all([product, other_product, floor1, floor2, floor3, *areas])
        db.flush()

        locations = {
            "fg1": _location(
                code="1F-FG-01", floor=1, area="FG", warehouse_type="finished", source_version="TWIN_V1"
            ),
            "fg1_add": _location(
                code="1F-FG-02", floor=1, area="FG", warehouse_type="finished", source_version="TWIN_V1"
            ),
            "semi1": _location(
                code="1F-SEMI-01", floor=1, area="SEMI", warehouse_type="semi_finished", source_version="TWIN_V1"
            ),
            "semi1_add": _location(
                code="1F-SEMI-02", floor=1, area="SEMI", warehouse_type="semi_finished", source_version="TWIN_V1"
            ),
            "semi1_rack": _location(
                code="1F-SEMI-RACK-01", floor=1, area="SEMI", warehouse_type="semi_finished", source_version="TWIN_V1"
            ),
            "fg1_rack": _location(
                code="1F-FG-RACK-01", floor=1, area="FG", warehouse_type="finished", source_version="TWIN_V1"
            ),
            "dispatch": _location(
                code="1F-DISPATCH-01", floor=1, area="DISPATCH", warehouse_type="finished", source_version="TWIN_V1"
            ),
            "fg3": _location(
                code="3F-FG-01", floor=3, area="FG", warehouse_type="finished", source_version="V11"
            ),
            "fg3_add": _location(
                code="3F-FG-02", floor=3, area="FG", warehouse_type="finished", source_version="V11"
            ),
            "floor2": _location(
                code="2F-FG-01", floor=2, area="FG", warehouse_type="finished", source_version="TWIN_V1"
            ),
            "draft": _location(
                code="3F-DRAFT-01", floor=3, area="DRAFT", warehouse_type="finished", source_version="V11"
            ),
            "unmapped": _location(
                code="3F-FG-NOMAP", floor=3, area="FG", warehouse_type="finished", source_version="V11", mapped=False
            ),
            "unplaced": _location(
                code="3F-FG-UNPLACED", floor=3, area="FG", warehouse_type="finished", source_version="V11", placed=False
            ),
        }
        locations["semi1_rack"].storage_type = "rack"
        locations["fg1_rack"].storage_type = "rack"
        db.add_all(list(locations.values()))
        db.flush()

        lots = {
            "normal": _finished_lot(
                number="FG-P147D-NORMAL",
                location=locations["fg1"],
                customer=customer,
                product=product,
                available=10,
            ),
            "to_zero": _finished_lot(
                number="FG-P147D-ZERO",
                location=locations["fg3"],
                customer=customer,
                product=product,
                available=5,
            ),
            "reserved": _finished_lot(
                number="FG-P147D-RESERVED",
                location=locations["fg1"],
                customer=customer,
                product=product,
                available=8,
                reserved=2,
            ),
            "frozen": _finished_lot(
                number="FG-P147D-FROZEN",
                location=locations["fg1"],
                customer=customer,
                product=product,
                status="frozen",
            ),
            "production": _finished_lot(
                number="FG-P147D-PROD",
                location=locations["fg1"],
                customer=customer,
                product=product,
                source_type="production_completion",
            ),
            "dispatch": _finished_lot(
                number="FG-P147D-DISPATCH",
                location=locations["dispatch"],
                customer=customer,
                product=product,
            ),
            "damaged": _finished_lot(
                number="FG-P147D-DAMAGED",
                location=locations["fg1"],
                customer=customer,
                product=product,
                damaged=1,
            ),
            "scrapped": _finished_lot(
                number="FG-P147D-SCRAPPED",
                location=locations["fg1"],
                customer=customer,
                product=product,
                scrapped=1,
            ),
            "semi": _semi_lot(
                number="SI-P147D-NORMAL",
                location=locations["semi1"],
                customer=customer,
                available=12,
            ),
        }
        db.add_all(list(lots.values()))
        db.flush()
        db.add(
            SemiFinishedLotAllowedProduct(
                inventory_lot_id=lots["semi"].id,
                product_id=product.id,
                confirmed_by=admin.id,
                confirmed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )

        zero_pallet = InventoryPallet(
            pallet_code="PLT-P147D-ZERO",
            location_id=locations["fg3"].id,
            location_occupancy_key="PRIMARY",
            status="active",
            is_current=True,
            version=1,
            created_by=admin.id,
        )
        db.add(zero_pallet)
        db.flush()
        db.add(
            InventoryPalletItem(
                pallet_id=zero_pallet.id,
                inventory_lot_id=lots["to_zero"].id,
                customer_id=customer.id,
                product_id=product.id,
                inventory_code=product.product_code,
                customer_name_snapshot=customer.name,
                product_name=product.product_name,
                item_type="finished",
                quantity=Decimal("5"),
                unit="boxes",
                match_status="matched",
            )
        )
        db.add(
            InventoryReservation(
                reservation_number="RS-P147D-ACTIVE",
                inventory_lot_id=lots["reserved"].id,
                reservation_type="finished_order",
                reserved_stock_quantity=2,
                credited_requirement_quantity=2,
                yield_factor=1,
                status="active",
                reserved_by=operator.id,
                reserved_at=datetime.now(timezone.utc).replace(tzinfo=None),
                reservation_group_key="P147D-ACTIVE",
                reservation_group_requested_quantity=2,
                idempotency_key="p147d-active-reservation",
            )
        )
        db.commit()

        ids = {
            "admin": admin.id,
            "operator": operator.id,
            "other": other.id,
            "customer": customer.id,
            "other_customer": other_customer.id,
            "product": product.id,
            "other_product": other_product.id,
            "zero_pallet": zero_pallet.id,
            **{f"loc_{key}": value.id for key, value in locations.items()},
            **{f"lot_{key}": value.id for key, value in lots.items()},
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


def _login(client: TestClient, username: str = "p147d-operator") -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200, response.text


def _add(
    *,
    client_item_id: str,
    location_id: int,
    inventory_type: str,
    customer_id: int,
    product_id: int,
    quantity: int,
    expected_layout_version: int = 1,
    source_kind: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "client_item_id": client_item_id,
        "operation": "add",
        "location_id": location_id,
        "expected_layout_version": expected_layout_version,
        "inventory_type": inventory_type,
        "unit": "boxes" if inventory_type == "finished" else "sheets",
        "customer_id": customer_id,
        "product_id": product_id,
        "quantity": quantity,
        "stock_date": "2026-08-13",
    }
    if source_kind is not None:
        payload["source_kind"] = source_kind
    return payload


def _decrease(
    *, client_item_id: str, location_id: int, lot_id: int, quantity: int, version: int = 1,
    expected_layout_version: int = 1,
) -> dict[str, object]:
    return {
        "client_item_id": client_item_id,
        "operation": "decrease",
        "location_id": location_id,
        "expected_layout_version": expected_layout_version,
        "lot_id": lot_id,
        "expected_version": version,
        "quantity": quantity,
    }


def _batch(key: str, *items: dict[str, object]) -> dict[str, object]:
    return {"idempotency_key": key, "confirmed": True, "items": list(items)}


def _counts(db: Session) -> tuple[int, int, int]:
    return (
        int(db.scalar(select(func.count(InventoryLot.id))) or 0),
        int(db.scalar(select(func.count(InventoryMovement.id))) or 0),
        int(
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action_code == "warehouse.stocktake_batch.confirmed"
                )
            )
            or 0
        ),
    )


def test_stocktake_add_requires_admin_before_any_inventory_validation(
    stocktake_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import warehouse as warehouse_api

    app, factory, ids, _database = stocktake_app
    payload = _batch(
        "p147d-admin-only-add",
        _add(
            client_item_id="admin-only-add",
            location_id=ids["loc_fg1_add"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=3,
        ),
    )
    with factory() as db:
        before = _counts(db)
    with TestClient(app) as client:
        _login(client)
        denied = client.post(URL, json=payload)
        assert denied.status_code == 403, denied.text
        assert "只能由管理员确认" in denied.json()["detail"]
    with factory() as db:
        assert _counts(db) == before

    def fake_execute(*_args, **kwargs):
        return {
            "message": "盘点补录已确认",
            "batch_id": kwargs["batch_id"],
            "confirmed_at": "2026-08-28T12:00:00",
            "operator_id": kwargs["operator_id"],
            "items": [],
        }

    monkeypatch.setattr(
        warehouse_api,
        "execute_warehouse_stocktake_batch",
        fake_execute,
    )
    with TestClient(app) as client:
        _login(client, "p147d-admin")
        accepted = client.post(URL, json=payload)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["idempotent_replay"] is False
        _login(client)
        ordinary_decrease = client.post(
            URL,
            json=_batch(
                "p147d-operator-decrease-still-allowed",
                _decrease(
                    client_item_id="operator-decrease",
                    location_id=ids["loc_fg1"],
                    lot_id=ids["lot_normal"],
                    quantity=1,
                ),
            ),
        )
        assert ordinary_decrease.status_code == 200, ordinary_decrease.text


def test_multi_item_add_finished_and_semi_plus_decrease_records_formal_facts(
    stocktake_app,
) -> None:
    app, factory, ids, _database = stocktake_app
    payload = _batch(
        "p147d-success",
        _add(
            client_item_id="add-finished",
            location_id=ids["loc_fg1_add"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=7,
        ),
        _add(
            client_item_id="add-semi",
            location_id=ids["loc_semi1_add"],
            inventory_type="semi_finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=9,
        ),
        _decrease(
            client_item_id="decrease-existing",
            location_id=ids["loc_fg1"],
            lot_id=ids["lot_normal"],
            quantity=3,
        ),
    )
    with TestClient(app) as client:
        _login(client, "p147d-admin")
        response = client.post(URL, json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["idempotent_replay"] is False
        assert len(body["request_hash"]) == 64
        assert {row["client_item_id"] for row in body["items"]} == {
            "add-finished",
            "add-semi",
            "decrease-existing",
        }

    with factory() as db:
        rows = {
            row["client_item_id"]: row for row in body["items"]
        }
        add_finished = db.get(InventoryLot, rows["add-finished"]["lot_id"])
        add_semi = db.get(InventoryLot, rows["add-semi"]["lot_id"])
        decreased = db.get(InventoryLot, ids["lot_normal"])
        assert add_finished is not None and add_semi is not None and decreased is not None
        assert (add_finished.source_type, add_finished.inventory_type, add_finished.unit) == (
            "stocktake",
            "finished",
            "boxes",
        )
        assert (add_semi.source_type, add_semi.inventory_type, add_semi.unit) == (
            "stocktake",
            "semi_finished",
            "sheets",
        )
        movements = list(
            db.scalars(
                select(InventoryMovement)
                .where(InventoryMovement.id.in_([row["movement_id"] for row in body["items"]]))
                .order_by(InventoryMovement.id)
            )
        )
        assert [row.movement_type for row in movements] == ["manual_in", "manual_in", "adjust"]
        assert all(row.operator_id == ids["operator"] for row in movements)
        assert [(row.before_available, row.after_available) for row in movements] == [
            (0, 7),
            (0, 9),
            (10, 7),
        ]
        assert all(row.reason for row in movements)
        assert decreased.quantity_available == 7
        assert rows["add-finished"]["customer_id"] == ids["customer"]
        assert rows["add-finished"]["product_id"] == ids["product"]
        assert rows["decrease-existing"]["quantity_before"] == 10
        assert rows["decrease-existing"]["quantity_after"] == 7
        audit = db.scalar(
            select(OperationLog).where(
                OperationLog.action_code == "warehouse.stocktake_batch.confirmed"
            )
        )
        assert audit is not None
        details = json.loads(audit.details or "{}")
        assert details["request_hash"] == body["request_hash"]
        assert details["result"]["items"] == body["items"]
        assert audit.actor_user_id_snapshot == ids["operator"]
        assert audit.created_at is not None


def test_decrease_to_zero_keeps_lot_history_hides_map_card_and_releases_empty_pallet(
    stocktake_app,
) -> None:
    app, factory, ids, _database = stocktake_app
    payload = _batch(
        "p147d-to-zero",
        _decrease(
            client_item_id="to-zero",
            location_id=ids["loc_fg3"],
            lot_id=ids["lot_to_zero"],
            quantity=5,
        ),
    )
    with TestClient(app) as client:
        _login(client)
        response = client.post(URL, json=payload)
        assert response.status_code == 200, response.text
        result = response.json()["items"][0]
        assert result["quantity_after"] == 0
        assert result["released_pallet_id"] == ids["zero_pallet"]
        map_row = client.get(f"/api/warehouse/floor3/locations/{ids['loc_fg3']}")
        assert map_row.status_code == 200, map_row.text
        pallet = map_row.json().get("pallet")
        assert pallet is None or not pallet.get("items")

    with factory() as db:
        lot = db.get(InventoryLot, ids["lot_to_zero"])
        pallet = db.get(InventoryPallet, ids["zero_pallet"])
        assert lot is not None and pallet is not None
        assert (lot.quantity_available, lot.status, lot.version) == (0, "closed", 2)
        assert (pallet.is_current, pallet.location_id, pallet.status) == (False, None, "closed")
        movement = db.get(InventoryMovement, result["movement_id"])
        assert movement is not None
        assert (movement.before_available, movement.after_available) == (5, 0)


def test_decrease_to_zero_keeps_current_pallet_when_positive_snapshot_remains(
    stocktake_app,
) -> None:
    app, factory, ids, _database = stocktake_app
    with factory() as db:
        db.add(
            InventoryPalletItem(
                pallet_id=ids["zero_pallet"],
                inventory_lot_id=None,
                customer_id=ids["customer"],
                product_id=ids["product"],
                inventory_code="P147D-SNAPSHOT-REMAINS",
                customer_name_snapshot="P1-47D 客户",
                product_name="待核成正式库存快照",
                item_type="finished",
                quantity=Decimal("2"),
                unit="boxes",
                match_status="matched",
            )
        )
        db.commit()

    payload = _batch(
        "p147d-to-zero-snapshot-remains",
        _decrease(
            client_item_id="to-zero-snapshot-remains",
            location_id=ids["loc_fg3"],
            lot_id=ids["lot_to_zero"],
            quantity=5,
        ),
    )
    with TestClient(app) as client:
        _login(client)
        response = client.post(URL, json=payload)
        assert response.status_code == 200, response.text
        result = response.json()["items"][0]
        assert result["quantity_after"] == 0
        assert result["released_pallet_id"] is None

    with factory() as db:
        lot = db.get(InventoryLot, ids["lot_to_zero"])
        pallet = db.get(InventoryPallet, ids["zero_pallet"])
        snapshot = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.pallet_id == ids["zero_pallet"],
                InventoryPalletItem.inventory_lot_id.is_(None),
                InventoryPalletItem.inventory_code == "P147D-SNAPSHOT-REMAINS",
            )
        )
        movement = db.get(InventoryMovement, result["movement_id"])
        audit = db.scalar(
            select(OperationLog).where(
                OperationLog.batch_id == "p147d-to-zero-snapshot-remains",
                OperationLog.action_code == "warehouse.stocktake_batch.confirmed",
                OperationLog.result == "success",
            )
        )
        assert lot is not None and pallet is not None and snapshot is not None
        assert (lot.quantity_available, lot.status, lot.version) == (0, "closed", 2)
        assert (
            pallet.is_current,
            pallet.location_id,
            pallet.status,
        ) == (True, ids["loc_fg3"], "active")
        assert snapshot.quantity == Decimal("2")
        assert movement is not None
        assert (movement.before_available, movement.after_available) == (5, 0)
        assert audit is not None


def test_replay_conflicts_cross_actor_and_revoked_permission_are_rechecked(
    stocktake_app,
) -> None:
    app, factory, ids, _database = stocktake_app
    payload = _batch(
        "p147d-replay",
        _decrease(
            client_item_id="replay-row",
            location_id=ids["loc_fg1"],
            lot_id=ids["lot_normal"],
            quantity=1,
        ),
    )
    with TestClient(app) as client:
        _login(client)
        first = client.post(URL, json=payload)
        assert first.status_code == 200, first.text
        replay = client.post(URL, json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["batch_id"] == first.json()["batch_id"]

        changed = _batch(
            "p147d-replay",
            _decrease(
                client_item_id="replay-row",
                location_id=ids["loc_fg1"],
                lot_id=ids["lot_normal"],
                quantity=2,
            ),
        )
        assert client.post(URL, json=changed).status_code == 409

        _login(client, "p147d-other")
        cross_actor = client.post(URL, json=payload)
        assert cross_actor.status_code == 409, cross_actor.text
        assert set(cross_actor.json()) == {"detail"}

    with factory() as db:
        operator = db.get(User, ids["operator"])
        assert operator is not None
        db.add(
            UserPermissionOverride(
                user_id=operator.id,
                permission_code="warehouse.stocktake.submit",
                is_allowed=False,
                granted_by=ids["admin"],
            )
        )
        db.commit()
    with TestClient(app) as client:
        _login(client)
        denied = client.post(URL, json=payload)
        assert denied.status_code == 403, denied.text

    with factory() as db:
        lot = db.get(InventoryLot, ids["lot_normal"])
        assert lot is not None and lot.quantity_available == 9
        assert _counts(db)[1:] == (1, 1)


def test_exact_replay_rechecks_current_customer_scope_without_leaking_result(
    stocktake_app,
) -> None:
    app, factory, ids, _database = stocktake_app
    payload = _batch(
        "p147d-scope-replay",
        _decrease(
            client_item_id="scope-replay-row",
            location_id=ids["loc_fg1"],
            lot_id=ids["lot_normal"],
            quantity=1,
        ),
    )
    with factory() as db:
        operator = db.get(User, ids["operator"])
        assert operator is not None
        operator.customer_access_mode = "selected"
        scope = UserCustomerScope(
            user_id=operator.id,
            customer_id=ids["customer"],
            assigned_by=ids["admin"],
        )
        db.add(scope)
        db.commit()
        scope_id = scope.id

    with TestClient(app) as client:
        _login(client)
        first = client.post(URL, json=payload)
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["idempotent_replay"] is False

    with factory() as db:
        scope = db.get(UserCustomerScope, scope_id)
        assert scope is not None
        db.delete(scope)
        db.commit()
        after_success = _counts(db)
        lot = db.get(InventoryLot, ids["lot_normal"])
        assert lot is not None
        lot_state = (lot.quantity_available, lot.version, lot.status)

    with TestClient(app) as client:
        _login(client)
        denied = client.post(URL, json=payload)
        assert denied.status_code == 403, denied.text
        assert set(denied.json()) == {"detail"}
        assert all(
            result_field not in denied.text
            for result_field in ("batch_id", "items", "lot_id", "movement_id")
        )

    with factory() as db:
        lot = db.get(InventoryLot, ids["lot_normal"])
        assert lot is not None
        assert (lot.quantity_available, lot.version, lot.status) == lot_state
        assert _counts(db) == after_success


def test_stale_last_item_and_runtime_failure_roll_back_entire_batch(
    stocktake_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import warehouse as warehouse_api

    app, factory, ids, _database = stocktake_app
    stale = _batch(
        "p147d-stale-last",
        _add(
            client_item_id="valid-first",
            location_id=ids["loc_fg1_add"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=2,
        ),
        _decrease(
            client_item_id="stale-last",
            location_id=ids["loc_fg1"],
            lot_id=ids["lot_normal"],
            quantity=1,
            version=99,
        ),
    )
    with factory() as db:
        before = _counts(db)
    with TestClient(app) as client:
        _login(client, "p147d-admin")
        response = client.post(URL, json=stale)
        assert response.status_code == 409, response.text
    with factory() as db:
        assert _counts(db) == before
        assert db.get(InventoryLot, ids["lot_normal"]).quantity_available == 10

    original = warehouse_api.execute_warehouse_stocktake_batch

    def fail_after_execute(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("p147d injected failure")

    monkeypatch.setattr(warehouse_api, "execute_warehouse_stocktake_batch", fail_after_execute)
    payload = _batch(
        "p147d-injected",
        _decrease(
            client_item_id="failure-row",
            location_id=ids["loc_fg1"],
            lot_id=ids["lot_normal"],
            quantity=1,
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        failed = client.post(URL, json=payload)
        assert failed.status_code == 500
    with factory() as db:
        assert _counts(db) == before
        assert db.get(InventoryLot, ids["lot_normal"]).quantity_available == 10


def test_customer_scope_and_add_location_product_type_unit_gates(stocktake_app) -> None:
    app, factory, ids, _database = stocktake_app
    with factory() as db:
        operator = db.get(User, ids["operator"])
        assert operator is not None
        operator.customer_access_mode = "selected"
        db.add(
            UserCustomerScope(
                user_id=operator.id,
                customer_id=ids["customer"],
                assigned_by=ids["admin"],
            )
        )
        db.commit()

    cases = [
        _add(
            client_item_id="other-customer",
            location_id=ids["loc_fg1_add"],
            inventory_type="finished",
            customer_id=ids["other_customer"],
            product_id=ids["other_product"],
            quantity=1,
        ),
        _add(
            client_item_id="wrong-product-owner",
            location_id=ids["loc_fg1_add"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["other_product"],
            quantity=1,
        ),
        _add(
            client_item_id="draft-area",
            location_id=ids["loc_draft"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=1,
        ),
        _add(
            client_item_id="unmapped",
            location_id=ids["loc_unmapped"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=1,
        ),
        _add(
            client_item_id="unplaced",
            location_id=ids["loc_unplaced"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=1,
        ),
        _add(
            client_item_id="floor-two",
            location_id=ids["loc_floor2"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=1,
        ),
    ]
    wrong_type = _add(
        client_item_id="wrong-type",
        location_id=ids["loc_semi1"],
        inventory_type="finished",
        customer_id=ids["customer"],
        product_id=ids["product"],
        quantity=1,
    )
    wrong_unit = _add(
        client_item_id="wrong-unit",
        location_id=ids["loc_fg1_add"],
        inventory_type="finished",
        customer_id=ids["customer"],
        product_id=ids["product"],
        quantity=1,
    )
    wrong_unit["unit"] = "sheets"
    cases.extend([wrong_type, wrong_unit])

    with factory() as db:
        before = _counts(db)
    with TestClient(app) as client:
        _login(client)
        for index, item in enumerate(cases):
            response = client.post(URL, json=_batch(f"p147d-add-gate-{index}", item))
            assert response.status_code in {403, 409, 422}, (item, response.text)
    with factory() as db:
        assert _counts(db) == before


@pytest.mark.parametrize(
    "lot_key,location_key,quantity",
    [
        ("frozen", "fg1", 1),
        ("reserved", "fg1", 1),
        ("dispatch", "dispatch", 1),
        ("production", "fg1", 1),
        ("damaged", "fg1", 1),
        ("scrapped", "fg1", 1),
        ("normal", "fg1", 11),
        ("normal", "fg3", 1),
    ],
)
def test_decrease_rejects_protected_or_drifted_inventory(
    stocktake_app, lot_key: str, location_key: str, quantity: int
) -> None:
    app, factory, ids, _database = stocktake_app
    payload = _batch(
        f"p147d-reduce-{lot_key}-{location_key}-{quantity}",
        _decrease(
            client_item_id="blocked",
            location_id=ids[f"loc_{location_key}"],
            lot_id=ids[f"lot_{lot_key}"],
            quantity=quantity,
        ),
    )
    with factory() as db:
        before = _counts(db)
        lot = db.get(InventoryLot, ids[f"lot_{lot_key}"])
        snapshot = (lot.quantity_available, lot.quantity_reserved, lot.version, lot.status)
    with TestClient(app) as client:
        _login(client)
        response = client.post(URL, json=payload)
        assert response.status_code == 409, response.text
    with factory() as db:
        lot = db.get(InventoryLot, ids[f"lot_{lot_key}"])
        assert (lot.quantity_available, lot.quantity_reserved, lot.version, lot.status) == snapshot
        assert _counts(db) == before


def test_reason_is_not_required_and_fixture_is_isolated(stocktake_app) -> None:
    app, factory, ids, database = stocktake_app
    assert database.name == "p1-47d-stocktake-batch.sqlite3"
    assert database.parent.name.startswith("test_")
    assert "carton_erp.sqlite3" not in str(database)
    with factory() as db:
        assert Path(str(db.get_bind().url.database)).resolve() == database.resolve()
    item = _decrease(
        client_item_id="no-reason",
        location_id=ids["loc_fg1"],
        lot_id=ids["lot_normal"],
        quantity=1,
    )
    assert "reason" not in item
    with TestClient(app) as client:
        _login(client)
        response = client.post(URL, json=_batch("p147d-no-reason", item))
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("operation", ["add", "decrease"])
def test_stale_map_version_rejects_the_entire_stocktake_without_writes(
    stocktake_app, operation: str
) -> None:
    app, factory, ids, _database = stocktake_app
    item = (
        _add(
            client_item_id="stale-map-add",
            location_id=ids["loc_fg1_add"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=3,
            expected_layout_version=2,
        )
        if operation == "add"
        else _decrease(
            client_item_id="stale-map-decrease",
            location_id=ids["loc_fg1"],
            lot_id=ids["lot_normal"],
            quantity=1,
            expected_layout_version=2,
        )
    )
    with factory() as db:
        before = _counts(db)
        lot = db.get(InventoryLot, ids["lot_normal"])
        lot_snapshot = (
            lot.quantity_available,
            lot.quantity_reserved,
            lot.version,
            lot.status,
        )
    with TestClient(app) as client:
        _login(client, "p147d-admin" if operation == "add" else "p147d-operator")
        response = client.post(URL, json=_batch(f"p147d-stale-map-{operation}", item))
        assert response.status_code == 409, response.text
    with factory() as db:
        assert _counts(db) == before
        lot = db.get(InventoryLot, ids["lot_normal"])
        assert (
            lot.quantity_available,
            lot.quantity_reserved,
            lot.version,
            lot.status,
        ) == lot_snapshot


def test_two_same_identity_adds_share_one_pallet_and_preserve_quantity(
    stocktake_app,
) -> None:
    app, factory, ids, _database = stocktake_app
    payload = _batch(
        "p147d-same-location-adds",
        _add(
            client_item_id="same-location-a",
            location_id=ids["loc_fg3_add"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=4,
        ),
        _add(
            client_item_id="same-location-b",
            location_id=ids["loc_fg3_add"],
            inventory_type="finished",
            customer_id=ids["customer"],
            product_id=ids["product"],
            quantity=6,
        ),
    )
    with TestClient(app) as client:
        _login(client, "p147d-admin")
        response = client.post(URL, json=payload)
        assert response.status_code == 200, response.text
        result = response.json()

    lot_ids = [row["lot_id"] for row in result["items"]]
    movement_ids = [row["movement_id"] for row in result["items"]]
    assert len(set(lot_ids)) == 2
    assert len(set(movement_ids)) == 2
    assert [row["quantity_after"] for row in result["items"]] == [4, 6]
    with factory() as db:
        lots = list(
            db.scalars(
                select(InventoryLot)
                .where(InventoryLot.id.in_(lot_ids))
                .order_by(InventoryLot.id)
            )
        )
        assert len(lots) == 2
        assert sum(int(row.quantity_available) for row in lots) == 10
        pallet_items = list(
            db.scalars(
                select(InventoryPalletItem)
                .where(InventoryPalletItem.inventory_lot_id.in_(lot_ids))
                .order_by(InventoryPalletItem.inventory_lot_id)
            )
        )
        assert len(pallet_items) == 2
        assert len({row.pallet_id for row in pallet_items}) == 1
        assert sum(int(row.quantity) for row in pallet_items) == 10
        movements = list(
            db.scalars(
                select(InventoryMovement)
                .where(InventoryMovement.id.in_(movement_ids))
                .order_by(InventoryMovement.id)
            )
        )
        assert [row.movement_type for row in movements] == ["manual_in", "manual_in"]
        assert [(row.before_available, row.after_available) for row in movements] == [
            (0, 4),
            (0, 6),
        ]
        assert sum(row.after_available - row.before_available for row in movements) == 10
        assert _counts(db) == (11, 2, 1)


def test_batch_claim_blocks_competing_reservation_and_keeps_one_ledger_truth(
    stocktake_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.order import Order, OrderItem
    from app.services import warehouse_stocktake_batch as stocktake_service
    from app.services.warehouse_inventory import (
        WarehouseInventoryError,
        reserve_finished_inventory,
    )

    app, factory, ids, _database = stocktake_app
    with factory() as db:
        order = Order(
            order_number="P147D-RACE-ORDER",
            customer_id=ids["customer"],
            order_date=date(2026, 8, 13),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("20"),
        )
        db.add(order)
        db.flush()
        order_item = OrderItem(
            order_id=order.id,
            product_id=ids["product"],
            quantity=20,
            delivered_quantity=0,
            unit_price=Decimal("1"),
            subtotal=Decimal("20"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="P147D-FG",
            snapshot_product_name="五层加强纸箱",
            snapshot_spec="500×300×200mm",
            snapshot_material="K=A",
        )
        db.add(order_item)
        db.commit()
        order_item_id = int(order_item.id)

    claimed = threading.Event()
    release_claim = threading.Event()
    original_claim = stocktake_service._claim_stocktake_batch_state

    def pause_after_claim(*args, **kwargs):
        original_claim(*args, **kwargs)
        claimed.set()
        assert release_claim.wait(5), "competing reservation never reached the claim"

    monkeypatch.setattr(
        stocktake_service,
        "_claim_stocktake_batch_state",
        pause_after_claim,
    )
    batch_response: list[object] = []

    def submit_batch() -> None:
        with TestClient(app) as client:
            _login(client)
            batch_response.append(
                client.post(
                    URL,
                    json=_batch(
                        "p147d-claim-race",
                        _decrease(
                            client_item_id="claim-race-decrease",
                            location_id=ids["loc_fg1"],
                            lot_id=ids["lot_normal"],
                            quantity=3,
                        ),
                    ),
                )
            )

    worker = threading.Thread(target=submit_batch, daemon=True)
    worker.start()
    assert claimed.wait(5), "stocktake batch never acquired its writer claim"
    competing_error: Exception | None = None
    try:
        with factory() as competing_db:
            competing_db.connection().exec_driver_sql("PRAGMA busy_timeout = 100")
            try:
                reserve_finished_inventory(
                    competing_db,
                    order_item_id=order_item_id,
                    inventory_lot_id=ids["lot_normal"],
                    quantity=2,
                    expected_version=1,
                    operator_id=ids["operator"],
                    idempotency_key="p147d-race-reservation",
                    warning_acknowledged_codes=[],
                )
                competing_db.commit()
            except (OperationalError, WarehouseInventoryError) as error:
                competing_db.rollback()
                competing_error = error
    finally:
        release_claim.set()
        worker.join(10)

    assert not worker.is_alive()
    assert competing_error is not None
    if isinstance(competing_error, OperationalError):
        assert "locked" in str(competing_error).lower()
    else:
        assert competing_error.status_code == 409
    assert len(batch_response) == 1
    response = batch_response[0]
    assert getattr(response, "status_code") == 200, getattr(response, "text")

    with factory() as db:
        lot = db.get(InventoryLot, ids["lot_normal"])
        assert lot is not None
        assert (
            lot.quantity_available,
            lot.quantity_reserved,
            lot.version,
            lot.status,
        ) == (7, 0, 2, "active")
        assert db.scalar(
            select(func.count(InventoryReservation.id)).where(
                InventoryReservation.inventory_lot_id == lot.id
            )
        ) == 0
        movements = list(
            db.scalars(
                select(InventoryMovement)
                .where(InventoryMovement.inventory_lot_id == lot.id)
                .order_by(InventoryMovement.id)
            )
        )
        assert len(movements) == 1
        assert (
            movements[0].movement_type,
            movements[0].before_available,
            movements[0].after_available,
            movements[0].before_reserved,
            movements[0].after_reserved,
        ) == ("adjust", 10, 7, 0, 0)
        assert _counts(db)[1:] == (1, 1)


def test_confirmed_capacity_allows_existing_occupancy_but_blocks_a_new_slot(
    stocktake_app,
) -> None:
    app, factory, ids, _database = stocktake_app
    with factory() as db:
        occupied_location = db.get(WarehouseLocation, ids["loc_fg3"])
        empty_location = db.get(WarehouseLocation, ids["loc_fg3_add"])
        assert occupied_location is not None and empty_location is not None
        area = db.scalar(
            select(WarehouseArea)
            .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
            .where(
                WarehouseFloor.floor_number == occupied_location.warehouse_floor,
                WarehouseArea.area_code == occupied_location.area_code,
            )
        )
        assert area is not None
        area.planned_pallet_capacity = 1
        area.capacity_review_status = "confirmed"
        area.capacity_eligible = True
        area.confirmed_pallet_capacity = 1
        area.capacity_reviewed_by = "P1-47D 容量复核员"
        area.capacity_reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        assert empty_location.area_code == area.area_code
        db.commit()

    with TestClient(app) as client:
        _login(client)
        decrease = client.post(
            URL,
            json=_batch(
                "p147d-capacity-decrease",
                _decrease(
                    client_item_id="capacity-decrease",
                    location_id=ids["loc_fg3"],
                    lot_id=ids["lot_to_zero"],
                    quantity=1,
                ),
            ),
        )
        assert decrease.status_code == 200, decrease.text
        _login(client, "p147d-admin")
        append = client.post(
            URL,
            json=_batch(
                "p147d-capacity-append",
                _add(
                    client_item_id="capacity-append",
                    location_id=ids["loc_fg3"],
                    inventory_type="finished",
                    customer_id=ids["customer"],
                    product_id=ids["product"],
                    quantity=2,
                ),
            ),
        )
        assert append.status_code == 200, append.text
        blocked = client.post(
            URL,
            json=_batch(
                "p147d-capacity-new-slot",
                _add(
                    client_item_id="capacity-new-slot",
                    location_id=ids["loc_fg3_add"],
                    inventory_type="finished",
                    customer_id=ids["customer"],
                    product_id=ids["product"],
                    quantity=3,
                ),
            ),
        )
        assert blocked.status_code == 409, blocked.text
        assert "容量" in blocked.json()["detail"]

    appended = append.json()["items"][0]
    with factory() as db:
        source = db.get(InventoryLot, ids["lot_to_zero"])
        added = db.get(InventoryLot, appended["lot_id"])
        assert source is not None and added is not None
        assert (source.quantity_available, source.version, source.status) == (4, 2, "active")
        assert (added.quantity_available, added.version, added.status) == (2, 1, "active")
        source_item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.inventory_lot_id == source.id
            )
        )
        added_item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.inventory_lot_id == added.id
            )
        )
        assert source_item is not None and added_item is not None
        assert source_item.pallet_id == added_item.pallet_id == ids["zero_pallet"]
        assert db.scalar(
            select(func.count(InventoryPallet.id)).where(
                InventoryPallet.location_id == ids["loc_fg3_add"],
                InventoryPallet.is_current.is_(True),
            )
        ) == 0
        assert _counts(db) == (10, 2, 2)


def test_finished_add_rejects_snapshot_only_current_pallet_without_any_write(
    stocktake_app,
) -> None:
    app, factory, ids, _database = stocktake_app
    with factory() as db:
        pallet = InventoryPallet(
            pallet_code="PLT-P147D-SNAPSHOT-ONLY",
            location_id=ids["loc_fg1_add"],
            location_occupancy_key="PRIMARY",
            status="active",
            is_current=True,
            version=1,
            created_by=ids["admin"],
        )
        db.add(pallet)
        db.flush()
        db.add(
            InventoryPalletItem(
                pallet_id=pallet.id,
                inventory_lot_id=None,
                customer_id=ids["customer"],
                product_id=ids["product"],
                inventory_code="P147D-FG",
                customer_name_snapshot="P1-47D 客户",
                product_name="五层加强纸箱",
                item_type="finished",
                quantity=Decimal("2"),
                unit="boxes",
                match_status="matched",
            )
        )
        db.commit()
        before = _counts(db)

    with TestClient(app) as client:
        _login(client, "p147d-admin")
        response = client.post(
            URL,
            json=_batch(
                "p147d-snapshot-pallet-block",
                _add(
                    client_item_id="snapshot-pallet-add",
                    location_id=ids["loc_fg1_add"],
                    inventory_type="finished",
                    customer_id=ids["customer"],
                    product_id=ids["product"],
                    quantity=3,
                ),
            ),
        )
        assert response.status_code == 409, response.text
        assert "快照" in response.json()["detail"] or "正式库存" in response.json()["detail"]

    with factory() as db:
        assert _counts(db) == before
        assert db.scalar(
            select(func.count(InventoryPalletItem.id)).where(
                InventoryPalletItem.pallet_id == pallet.id
            )
        ) == 1


def test_open_legacy_stocktake_task_blocks_decrease_without_any_write(
    stocktake_app,
) -> None:
    app, factory, ids, _database = stocktake_app
    with factory() as db:
        order = StocktakeOrder(
            order_number="ST-P147D-OPEN",
            location_id=ids["loc_fg1"],
            status="submitted",
            version=1,
            submitted_by=ids["operator"],
            submitted_at=datetime.now(timezone.utc).replace(tzinfo=None),
            idempotency_key="p147d-open-stocktake-task",
        )
        db.add(order)
        db.flush()
        db.add(
            StocktakeItem(
                order_id=order.id,
                inventory_lot_id=ids["lot_normal"],
                lot_version_snapshot=1,
                available_quantity_snapshot=10,
                reserved_quantity_snapshot=0,
                on_hand_quantity_snapshot=10,
                counted_quantity=10,
                difference_quantity=0,
                lot_number_snapshot="FG-P147D-NORMAL",
                customer_name_snapshot="P1-47D 客户",
                product_name_snapshot="五层加强纸箱",
                specification_snapshot="500×300×200mm",
                inventory_code_snapshot="P147D-FG",
                location_code_snapshot="1F-FG-01",
                unit_snapshot="boxes",
            )
        )
        db.commit()
        before = _counts(db)

    with TestClient(app) as client:
        _login(client)
        response = client.post(
            URL,
            json=_batch(
                "p147d-open-task-block",
                _decrease(
                    client_item_id="open-task-blocked",
                    location_id=ids["loc_fg1"],
                    lot_id=ids["lot_normal"],
                    quantity=1,
                ),
            ),
        )
        assert response.status_code == 409, response.text
        assert "盘点任务" in response.json()["detail"]

    with factory() as db:
        lot = db.get(InventoryLot, ids["lot_normal"])
        assert lot is not None
        assert (
            lot.quantity_available,
            lot.quantity_reserved,
            lot.version,
            lot.status,
        ) == (10, 0, 1, "active")
        assert _counts(db) == before


def test_stocktake_only_permission_gets_scoped_overview_candidates_and_projection(
    stocktake_app,
) -> None:
    app, factory, ids, _database = stocktake_app
    with factory() as db:
        operator = db.get(User, ids["operator"])
        assert operator is not None
        operator.customer_access_mode = "selected"
        db.add(
            UserCustomerScope(
                user_id=operator.id,
                customer_id=ids["customer"],
                assigned_by=ids["admin"],
            )
        )
        for permission_code in ("warehouse.view", "warehouse.execute"):
            db.add(
                UserPermissionOverride(
                    user_id=operator.id,
                    permission_code=permission_code,
                    is_allowed=False,
                    granted_by=ids["admin"],
                )
            )
        db.commit()

    with TestClient(app) as client:
        _login(client)
        overview = client.get("/api/warehouse/twin-dashboard/overview?days=30")
        assert overview.status_code == 200, overview.text
        body = overview.json()
        assert body["scope"]["customer_restricted"] is True
        locations = {row["location_id"]: row for row in body["locations"]}
        assert locations
        assert all(row["source_version"] in {"TWIN_V1", "V11"} for row in locations.values())
        projected = [
            item
            for row in locations.values()
            for pallet in row["pallets"]
            for item in pallet["items"]
        ] + [item for row in locations.values() for item in row["loose_items"]]
        by_lot = {item["lot_id"]: item for item in projected}
        assert by_lot[ids["lot_normal"]]["stocktake_decrease_eligible"] is True
        assert by_lot[ids["lot_normal"]]["stocktake_decrease_block_reason"] is None
        assert by_lot[ids["lot_reserved"]]["stocktake_decrease_eligible"] is False
        assert "预占" in by_lot[ids["lot_reserved"]]["stocktake_decrease_block_reason"]
        assert by_lot[ids["lot_semi"]]["product_id"] == ids["product"]
        assert by_lot[ids["lot_semi"]]["allowed_product_ids"] == [ids["product"]]
        assert {item.get("customer_id") for item in projected} <= {ids["customer"]}

        candidates = client.get(
            "/api/warehouse/floor3/product-candidates",
            params={"q": "", "customer_id": ids["customer"], "limit": 50},
        )
        assert candidates.status_code == 200, candidates.text
        candidate_items = candidates.json()["items"]
        assert candidate_items
        assert {item["customer_id"] for item in candidate_items} == {ids["customer"]}
        denied_scope = client.get(
            "/api/warehouse/floor3/product-candidates",
            params={"q": "", "customer_id": ids["other_customer"], "limit": 50},
        )
        assert denied_scope.status_code == 403, denied_scope.text


@pytest.mark.parametrize(
    ("formal_break", "expected_reason"),
    [
        ("draft_policy", "尚未发布"),
        ("disabled_area", "区域尚未启用"),
        ("missing_geometry", "缺少地图位置"),
    ],
)
def test_overview_projection_fails_closed_when_formal_location_authority_breaks(
    stocktake_app,
    formal_break: str,
    expected_reason: str,
) -> None:
    app, factory, ids, _database = stocktake_app
    with factory() as db:
        operator = db.get(User, ids["operator"])
        location = db.get(WarehouseLocation, ids["loc_fg1"])
        assert operator is not None and location is not None
        operator.customer_access_mode = "selected"
        db.add(
            UserCustomerScope(
                user_id=operator.id,
                customer_id=ids["customer"],
                assigned_by=ids["admin"],
            )
        )
        area = db.scalar(
            select(WarehouseArea)
            .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
            .where(
                WarehouseFloor.floor_number == 1,
                WarehouseArea.area_code == "FG",
            )
        )
        assert area is not None and area.storage_policy is not None
        if formal_break == "draft_policy":
            area.storage_policy.status = "draft"
        elif formal_break == "disabled_area":
            area.construction_status = "layout_complete"
        else:
            assert location.floor3_layout is not None
            db.delete(location.floor3_layout)
        db.commit()

    with TestClient(app) as client:
        _login(client)
        response = client.get("/api/warehouse/twin-dashboard/overview?days=30")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["scope"]["customer_restricted"] is True
        location_row = next(
            row for row in body["locations"] if row["location_id"] == ids["loc_fg1"]
        )
        projected = [
            item
            for pallet in location_row["pallets"]
            for item in pallet["items"]
        ] + location_row["loose_items"]
        lot = next(item for item in projected if item["lot_id"] == ids["lot_normal"])
        assert lot["stocktake_decrease_eligible"] is False
        assert expected_reason in lot["stocktake_decrease_block_reason"]


def test_finished_and_semi_stocktake_support_rack_but_dispatch_stays_blocked(
    stocktake_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def published_floor_identity(floor_number: int) -> dict | None:
        zones = {
            1: {
                "zone-1f-fg": "FG",
                "zone-1f-semi": "SEMI",
                "zone-1f-dispatch": "DISPATCH",
            },
            3: {"zone-3f-fg": "FG", "zone-3f-draft": "DRAFT"},
        }.get(floor_number)
        if zones is None:
            return None
        return {
            "floor_code": f"{floor_number}F",
            "revision": "p1-47d-formal-map",
            "feature_ids": frozenset(zones),
            "zones_by_id": zones,
            "zone_ids_by_area": {
                area: tuple(key for key, value in zones.items() if value == area)
                for area in set(zones.values())
            },
        }

    monkeypatch.setattr(
        "app.services.location_candidates.load_warehouse_twin_published_floor_identity",
        published_floor_identity,
    )
    app, factory, ids, _database = stocktake_app
    with factory() as db:
        before = _counts(db)
    with TestClient(app) as client:
        _login(client, "p147d-admin")
        finished = client.post(
            URL,
            json=_batch(
                "p1126-finished-rack-partner",
                _add(
                    client_item_id="finished-rack",
                    location_id=ids["loc_fg1_rack"],
                    inventory_type="finished",
                    customer_id=ids["customer"],
                    product_id=ids["product"],
                    quantity=2,
                    source_kind="partner_transfer",
                ),
            ),
        )
        assert finished.status_code == 200, finished.text
        dispatch = client.post(
            URL,
            json=_batch(
                "p1126-dispatch-blocked",
                _add(
                    client_item_id="dispatch-add",
                    location_id=ids["loc_dispatch"],
                    inventory_type="finished",
                    customer_id=ids["customer"],
                    product_id=ids["product"],
                    quantity=2,
                ),
            ),
        )
        assert dispatch.status_code == 409, dispatch.text
        semi = client.post(
            URL,
            json=_batch(
                "p147d-semi-rack-supported",
                _add(
                    client_item_id="semi-rack",
                    location_id=ids["loc_semi1_rack"],
                    inventory_type="semi_finished",
                    customer_id=ids["customer"],
                    product_id=ids["product"],
                    quantity=5,
                ),
            ),
        )
        assert semi.status_code == 200, semi.text

    finished_result = finished.json()["items"][0]
    result = semi.json()["items"][0]
    with factory() as db:
        finished_lot = db.get(InventoryLot, finished_result["lot_id"])
        finished_movement = db.get(InventoryMovement, finished_result["movement_id"])
        lot = db.get(InventoryLot, result["lot_id"])
        movement = db.get(InventoryMovement, result["movement_id"])
        assert finished_lot is not None and finished_movement is not None
        assert finished_lot.pallet_item is None
        assert (finished_lot.inventory_type, finished_lot.unit, finished_lot.warehouse_location_id, finished_lot.quantity_available) == (
            "finished", "boxes", ids["loc_fg1_rack"], 2
        )
        assert "合作纸箱厂搬入" in (finished_lot.remarks or "")
        assert "合作纸箱厂搬入" in (finished_movement.reason or "")
        assert finished_result["source_kind"] == "partner_transfer"
        assert lot is not None and movement is not None
        assert (
            lot.inventory_type,
            lot.unit,
            lot.warehouse_location_id,
            lot.quantity_available,
        ) == ("semi_finished", "sheets", ids["loc_semi1_rack"], 5)
        assert (movement.movement_type, movement.before_available, movement.after_available) == (
            "manual_in",
            0,
            5,
        )
        assert _counts(db) == (before[0] + 2, before[1] + 2, before[2] + 2)


def test_frontend_uses_one_batch_post_cancel_is_zero_write_and_keeps_prior_modes() -> None:
    source = FRONTEND.read_text(encoding="utf-8")
    helper = STOCKTAKE_DRAFT.read_text(encoding="utf-8")
    assert "P1_47D_ENABLED" not in source
    assert 'value.permissions.includes("warehouse.stocktake.submit")' in source
    assert 'moveAction' in source and '"stocktake"' in source
    assert "confirmStocktakeDrafts" in source
    confirm_start = source.index("  const confirmStocktakeDrafts")
    confirm_end = source.index("\n  const ", confirm_start + 2)
    confirm = source[confirm_start:confirm_end]
    assert confirm.count("mutateJson(") == 1
    assert confirm.count('"/api/warehouse/twin-operations/stocktake-batches"') == 1
    assert "Promise.all" not in confirm
    assert all(
        old_path not in confirm
        for old_path in (
            "/twin-operations/finished-inbound",
            "/twin-operations/semi-finished-inbound",
            "/quantity-correction",
        )
    )
    cancel_start = source.index("  const cancelStocktakeDrafts")
    cancel_end = source.index("\n  const ", cancel_start + 2)
    cancel = source[cancel_start:cancel_end]
    assert "clearStocktakeDrafts()" in cancel
    assert "mutateJson(" not in cancel and "fetch(" not in cancel
    assert "buildStocktakeBatchPayload" in helper
    assert '"/api/warehouse/twin-operations/move-batches"' in source
    assert '"/api/warehouse/pallets/merge-batches"' in source
    assert "locationEditMode" in source and "mapMode === \"planning\"" in source
    assert "temporary-finished-inbound" not in source
