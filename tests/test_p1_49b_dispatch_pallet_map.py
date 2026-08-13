from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.warehouse import router as warehouse_router
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.order import OrderItem
from app.models.product import Product
from app.models.production import ProductionCompletion
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    Floor3LocationLayout,
    InventoryLocationMovement,
    InventoryLot,
    InventoryLotTransfer,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.production_workflow import CompletionCommand, complete_production_batch
from test_n029_production_service import PASSWORD, _add_case, production_app


MOVE_BATCH_URL = "/api/warehouse/twin-operations/move-batches"
FRONTEND = (
    Path(__file__).resolve().parents[1]
    / "factory_twin"
    / "frontend"
    / "src"
    / "WarehouseTwinApp.tsx"
)
WAREHOUSE_INVENTORY = FRONTEND.with_name("warehouseInventory.mjs")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _direct_command(task, quantity: int) -> CompletionCommand:
    return CompletionCommand(
        task_id=int(task.id),
        expected_version=int(task.version),
        disposition="direct",
        material_input_quantity=quantity,
        actual_output_quantity=quantity,
        defective_quantity=0,
        direct_delivery_quantity=quantity,
    )


def _published_area(
    floor: WarehouseFloor,
    *,
    area_code: str,
    feature_id: str,
) -> WarehouseArea:
    area = WarehouseArea(
        floor=floor,
        area_code=area_code,
        area_name=f"{floor.floor_code}-{area_code}",
        construction_status="enabled",
    )
    area.storage_policy = WarehouseAreaStoragePolicy(
        map_feature_id=feature_id,
        allowed_inventory_types_json='["finished"]',
        storage_layout="pallet_ground",
        status="published",
        published_map_revision="p1-49b-test-map",
        version=1,
    )
    return area


def _mapped_location(
    *,
    code: str,
    floor_number: int,
    area_code: str,
    source_version: str,
    sort_order: int,
) -> WarehouseLocation:
    location = WarehouseLocation(
        location_code=code,
        location_name=code,
        warehouse_type="finished",
        is_active=True,
        warehouse_floor=floor_number,
        area_code=area_code,
        storage_type="ground",
        placement_status="placed",
        is_temporary=False,
        source_version=source_version,
        sort_order=sort_order,
    )
    location.floor3_layout = Floor3LocationLayout(
        left_pct=10,
        top_pct=10,
        width_pct=8,
        height_pct=8,
        z_index=0,
        version=1,
        source_type="manual",
    )
    return location


def _inventory_totals(db) -> tuple[int, int, int, int, int]:
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


def _remaining_reserved(db) -> int:
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


def _dashboard(client: TestClient) -> dict:
    response = client.get("/api/warehouse/twin-dashboard/overview?days=30")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def dispatch_pallet_app(production_app):
    app, factory, ids = production_app
    app.include_router(warehouse_router, prefix="/api/warehouse")

    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "n029-admin"))
        customer_a = db.get(Customer, ids["customer_a"])
        customer_b = db.get(Customer, ids["customer_b"])
        product_a = db.get(Product, ids["product_a"])
        product_b = db.scalar(
            select(Product).where(Product.customer_id == ids["customer_b"])
        )
        scoped = db.scalar(select(User).where(User.username == "n029-scoped"))
        staging = db.get(WarehouseLocation, ids["staging"])
        assert all(
            row is not None
            for row in (
                admin,
                scoped,
                customer_a,
                customer_b,
                product_a,
                product_b,
                staging,
            )
        )
        db.add(
            UserPermissionOverride(
                user_id=scoped.id,
                permission_code="warehouse.view",
                is_allowed=True,
                granted_by=admin.id,
            )
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
        dispatch_area = _published_area(
            floor1,
            area_code="DISPATCH",
            feature_id="zone-1f-dispatch",
        )
        floor1_area = _published_area(
            floor1,
            area_code="FG",
            feature_id="zone-1f-fg",
        )
        floor3_area = _published_area(
            floor3,
            area_code="FG-004",
            feature_id="zone-3f-fg-004",
        )
        db.add_all([floor1, floor3, dispatch_area, floor1_area, floor3_area])
        db.flush()
        staging.floor3_layout = Floor3LocationLayout(
            left_pct=4,
            top_pct=4,
            width_pct=12,
            height_pct=12,
            z_index=0,
            version=1,
            source_type="manual",
            created_by=admin.id,
        )
        floor1_target = _mapped_location(
            code="1F-FG-P149B-01",
            floor_number=1,
            area_code="FG",
            source_version="TWIN_V1",
            sort_order=101,
        )
        floor3_target = _mapped_location(
            code="3F-FG004-P149B-01",
            floor_number=3,
            area_code="FG-004",
            source_version="TWIN_V1",
            sort_order=102,
        )
        db.add_all([floor1_target, floor3_target])
        db.flush()

        _order_a, item_a, task_a = _add_case(
            db,
            key="p149b-a",
            customer=customer_a,
            product=product_a,
            quantity=7,
        )
        _order_b, item_b, task_b = _add_case(
            db,
            key="p149b-b",
            customer=customer_b,
            product=product_b,
            quantity=9,
        )
        first = complete_production_batch(
            db,
            idempotency_key="p149b-direct-a",
            commands=[_direct_command(task_a, 7)],
            operator_id=admin.id,
        ).completions[0]
        second = complete_production_batch(
            db,
            idempotency_key="p149b-direct-b",
            commands=[_direct_command(task_b, 9)],
            operator_id=admin.id,
        ).completions[0]
        first_lot = db.get(InventoryLot, first.inventory_lot_id)
        second_lot = db.get(InventoryLot, second.inventory_lot_id)
        assert first_lot is not None and first_lot.pallet_item is not None
        assert second_lot is not None and second_lot.pallet_item is not None
        first_pallet = first_lot.pallet_item.pallet
        second_pallet = second_lot.pallet_item.pallet

        legacy_loose = InventoryLot(
            lot_number="FG-P149B-LEGACY-LOOSE",
            inventory_type="finished",
            warehouse_location_id=staging.id,
            quantity_available=3,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="production_completion",
            source_ref_type="production_completion",
            source_ref_id=149099,
            stock_date=date(2026, 8, 1),
            stock_date_accuracy="exact",
            stock_date_original_text="2026-08-01",
            last_movement_at=_now(),
            version=1,
        )
        legacy_loose.finished_detail = FinishedGoodsInventoryDetail(
            owner_customer_id=customer_a.id,
            owner_customer_name_snapshot=customer_a.name,
            is_general=False,
            product_id=product_a.id,
            inventory_code_snapshot=product_a.product_code,
            product_name_snapshot="P1-49B 历史散存",
        )
        db.add(legacy_loose)
        db.commit()
        result = {
            **ids,
            "first_pallet": first_pallet.id,
            "second_pallet": second_pallet.id,
            "first_lot": first_lot.id,
            "second_lot": second_lot.id,
            "first_item": item_a.id,
            "second_item": item_b.id,
            "first_completion": first.id,
            "second_completion": second.id,
            "legacy_loose": legacy_loose.id,
            "floor1_target": floor1_target.id,
            "floor3_target": floor3_target.id,
        }
    yield app, factory, result


def test_dispatch_dashboard_returns_every_system_pallet_without_item_crossing(
    dispatch_pallet_app,
) -> None:
    app, _factory, ids = dispatch_pallet_app
    with TestClient(app) as client:
        _login(client, "n029-admin")
        payload = _dashboard(client)

    dispatch = next(
        row for row in payload["locations"] if row["location_code"] == "F1-DISPATCH-01"
    )
    assert dispatch["pallet"] is None
    assert [row["pallet_id"] for row in dispatch["pallets"]] == sorted(
        [ids["first_pallet"], ids["second_pallet"]]
    )
    assert {
        pallet["pallet_id"]: [item["lot_id"] for item in pallet["items"]]
        for pallet in dispatch["pallets"]
    } == {
        ids["first_pallet"]: [ids["first_lot"]],
        ids["second_pallet"]: [ids["second_lot"]],
    }
    assert [row["lot_id"] for row in dispatch["loose_items"]] == [
        ids["legacy_loose"]
    ]
    assert payload["summary"]["occupied_pallets"] >= 2
    floor1 = next(row for row in payload["floors"] if row["floor_code"] == "1F")
    assert floor1["occupied_pallets"] >= 2


def test_single_pallet_location_keeps_legacy_pallet_and_pallets_contract(
    dispatch_pallet_app,
) -> None:
    app, factory, ids = dispatch_pallet_app
    payload = {
        "idempotency_key": "p149b-single-location-contract",
        "confirmed": True,
        "items": [
            {
                "client_item_id": "move-first",
                "operation": "pallet_move",
                "pallet_id": ids["first_pallet"],
                "expected_version": 1,
                "target_location_id": ids["floor1_target"],
            }
        ],
    }
    with TestClient(app) as client:
        _login(client, "n029-admin")
        response = client.post(MOVE_BATCH_URL, json=payload)
        assert response.status_code == 200, response.text
        overview = _dashboard(client)

    target = next(
        row
        for row in overview["locations"]
        if row["location_id"] == ids["floor1_target"]
    )
    assert target["pallet"]["pallet_id"] == ids["first_pallet"]
    assert [row["pallet_id"] for row in target["pallets"]] == [
        ids["first_pallet"]
    ]
    with factory() as db:
        pallet = db.get(InventoryPallet, ids["first_pallet"])
        assert pallet is not None
        assert pallet.location_occupancy_key == "PRIMARY"


def test_customer_scope_only_exposes_its_dispatch_pallet_and_loose_lot(
    dispatch_pallet_app,
) -> None:
    app, factory, ids = dispatch_pallet_app
    with factory() as db:
        scoped = db.scalar(select(User).where(User.username == "n029-scoped"))
        second_pallet = db.get(InventoryPallet, ids["second_pallet"])
        assert scoped is not None
        assert second_pallet is not None
        second_pallet_code = second_pallet.pallet_code
        scopes = list(
            db.scalars(
                select(UserCustomerScope).where(UserCustomerScope.user_id == scoped.id)
            )
        )
        assert {row.customer_id for row in scopes} == {ids["customer_a"]}

    with TestClient(app) as client:
        _login(client, "n029-scoped")
        payload = _dashboard(client)
    dispatch = next(
        row for row in payload["locations"] if row["location_code"] == "F1-DISPATCH-01"
    )
    assert [row["pallet_id"] for row in dispatch["pallets"]] == [
        ids["first_pallet"]
    ]
    assert [row["lot_id"] for row in dispatch["loose_items"]] == [
        ids["legacy_loose"]
    ]
    visible_pallets = [
        pallet
        for location in payload["locations"]
        for pallet in location.get("pallets", [])
    ]
    visible_items = [
        item
        for location in payload["locations"]
        for item in [
            *(item for pallet in location.get("pallets", []) for item in pallet["items"]),
            *location.get("loose_items", []),
        ]
    ]
    visible_pallet_ids = {row["pallet_id"] for row in visible_pallets}
    visible_pallet_codes = {row["pallet_code"] for row in visible_pallets}
    visible_lot_ids = {row["lot_id"] for row in visible_items}
    visible_customer_ids = {
        row["customer_id"] for row in visible_items if row.get("customer_id") is not None
    }
    assert ids["first_pallet"] in visible_pallet_ids
    assert ids["second_pallet"] not in visible_pallet_ids
    assert second_pallet_code not in visible_pallet_codes
    assert ids["second_lot"] not in visible_lot_ids
    assert visible_customer_ids == {ids["customer_a"]}
    assert payload["summary"]["occupied_pallets"] == len(visible_pallet_ids)


@pytest.mark.parametrize(
    "mixed_variant",
    ("authorized_plus_unidentified", "authorized_plus_unauthorized"),
)
def test_customer_scope_fail_closes_every_mixed_snapshot_pallet(
    dispatch_pallet_app,
    mixed_variant: str,
) -> None:
    app, factory, ids = dispatch_pallet_app
    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "n029-admin"))
        customer_a = db.get(Customer, ids["customer_a"])
        customer_b = db.get(Customer, ids["customer_b"])
        product_a = db.get(Product, ids["product_a"])
        product_b = db.scalar(
            select(Product).where(Product.customer_id == ids["customer_b"])
        )
        staging = db.get(WarehouseLocation, ids["staging"])
        assert all(
            row is not None
            for row in (admin, customer_a, customer_b, product_a, product_b, staging)
        )

        pallet_code = f"PLT-P149B-SCOPE-{mixed_variant.upper()}"
        mixed_pallet = InventoryPallet(
            pallet_code=pallet_code,
            location_id=staging.id,
            location_occupancy_key=f"P149B-SCOPE:{mixed_variant}",
            status="active",
            is_current=True,
            version=1,
            created_by=admin.id,
        )
        db.add(mixed_pallet)
        db.flush()
        authorized_item_code = f"P149B-{mixed_variant}-AUTHORIZED"
        restricted_item_code = f"P149B-{mixed_variant}-RESTRICTED"
        restricted_customer = (
            None
            if mixed_variant == "authorized_plus_unidentified"
            else customer_b
        )
        restricted_product = (
            product_a
            if restricted_customer is None
            else product_b
        )
        db.add_all(
            [
                InventoryPalletItem(
                    pallet_id=mixed_pallet.id,
                    inventory_lot_id=None,
                    customer_id=customer_a.id,
                    product_id=product_a.id,
                    inventory_code=authorized_item_code,
                    customer_name_snapshot=customer_a.name,
                    product_name=product_a.product_name,
                    item_type="finished",
                    quantity=2,
                    unit="boxes",
                    match_status="matched",
                ),
                InventoryPalletItem(
                    pallet_id=mixed_pallet.id,
                    inventory_lot_id=None,
                    customer_id=(
                        restricted_customer.id
                        if restricted_customer is not None
                        else None
                    ),
                    product_id=restricted_product.id,
                    inventory_code=restricted_item_code,
                    customer_name_snapshot=(
                        restricted_customer.name
                        if restricted_customer is not None
                        else "UNIDENTIFIED-CUSTOMER-SNAPSHOT"
                    ),
                    product_name=restricted_product.product_name,
                    item_type="finished",
                    quantity=3,
                    unit="boxes",
                    match_status="matched",
                ),
            ]
        )
        db.commit()
        mixed_pallet_id = mixed_pallet.id

    with TestClient(app) as client:
        _login(client, "n029-admin")
        admin_payload = _dashboard(client)
    admin_pallets = [
        pallet
        for location in admin_payload["locations"]
        for pallet in location.get("pallets", [])
    ]
    admin_mixed_pallet = next(
        row for row in admin_pallets if row["pallet_id"] == mixed_pallet_id
    )
    assert admin_mixed_pallet["pallet_code"] == pallet_code
    assert {row["inventory_code"] for row in admin_mixed_pallet["items"]} == {
        authorized_item_code,
        restricted_item_code,
    }

    with TestClient(app) as client:
        _login(client, "n029-scoped")
        scoped_payload = _dashboard(client)
    scoped_pallets = [
        pallet
        for location in scoped_payload["locations"]
        for pallet in location.get("pallets", [])
    ]
    scoped_items = [
        item
        for location in scoped_payload["locations"]
        for item in [
            *(item for pallet in location.get("pallets", []) for item in pallet["items"]),
            *location.get("loose_items", []),
        ]
    ]
    assert mixed_pallet_id not in {row["pallet_id"] for row in scoped_pallets}
    assert pallet_code not in {row["pallet_code"] for row in scoped_pallets}
    assert {authorized_item_code, restricted_item_code}.isdisjoint(
        {row["inventory_code"] for row in scoped_items}
    )
    assert scoped_payload["summary"]["occupied_pallets"] == len(
        {row["pallet_id"] for row in scoped_pallets}
    )


def test_customer_scope_hides_disguised_cross_product_snapshot_pallet(
    dispatch_pallet_app,
) -> None:
    app, factory, ids = dispatch_pallet_app
    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "n029-admin"))
        customer_a = db.get(Customer, ids["customer_a"])
        product_b = db.scalar(
            select(Product).where(Product.customer_id == ids["customer_b"])
        )
        staging = db.get(WarehouseLocation, ids["staging"])
        assert all(row is not None for row in (admin, customer_a, product_b, staging))
        pallet_code = "PLT-P149B-DISGUISED-SNAPSHOT"
        secret_code = "P149B-SECRET-CROSS-PRODUCT-SNAPSHOT"
        pallet = InventoryPallet(
            pallet_code=pallet_code,
            location_id=staging.id,
            location_occupancy_key="P149B-SCOPE:DISGUISED-SNAPSHOT",
            status="active",
            is_current=True,
            version=1,
            created_by=admin.id,
        )
        pallet.items.append(
            InventoryPalletItem(
                inventory_lot_id=None,
                customer_id=customer_a.id,
                product_id=product_b.id,
                inventory_code=secret_code,
                customer_name_snapshot=customer_a.name,
                product_name=product_b.product_name,
                item_type="finished",
                quantity=17,
                unit="boxes",
                match_status="matched",
            )
        )
        db.add(pallet)
        db.commit()
        pallet_id = pallet.id

    with TestClient(app) as client:
        _login(client, "n029-admin")
        admin_payload = _dashboard(client)
    admin_pallets = [
        pallet
        for location in admin_payload["locations"]
        for pallet in location.get("pallets", [])
    ]
    admin_pallet = next(row for row in admin_pallets if row["pallet_id"] == pallet_id)
    assert admin_pallet["pallet_code"] == pallet_code
    assert [row["inventory_code"] for row in admin_pallet["items"]] == [secret_code]

    with TestClient(app) as client:
        _login(client, "n029-scoped")
        scoped_payload = _dashboard(client)
    scoped_pallets = [
        pallet
        for location in scoped_payload["locations"]
        for pallet in location.get("pallets", [])
    ]
    scoped_items = [
        item
        for location in scoped_payload["locations"]
        for item in [
            *(item for pallet in location.get("pallets", []) for item in pallet["items"]),
            *location.get("loose_items", []),
        ]
    ]
    assert pallet_id not in {row["pallet_id"] for row in scoped_pallets}
    assert pallet_code not in {row["pallet_code"] for row in scoped_pallets}
    assert secret_code not in {row["inventory_code"] for row in scoped_items}


def test_customer_scope_removes_hidden_mixed_linked_pallet_from_all_projections(
    dispatch_pallet_app,
) -> None:
    app, factory, ids = dispatch_pallet_app
    with TestClient(app) as client:
        _login(client, "n029-scoped")
        scoped_before = _dashboard(client)

    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "n029-admin"))
        customer_a = db.get(Customer, ids["customer_a"])
        customer_b = db.get(Customer, ids["customer_b"])
        product_a = db.get(Product, ids["product_a"])
        product_b = db.scalar(
            select(Product).where(Product.customer_id == ids["customer_b"])
        )
        assert all(
            row is not None
            for row in (admin, customer_a, customer_b, product_a, product_b)
        )
        location_code = "1F-FG-P149B-HIDDEN-MIXED"
        location = _mapped_location(
            code=location_code,
            floor_number=1,
            area_code="FG",
            source_version="TWIN_V1",
            sort_order=149,
        )
        pallet_code = "PLT-P149B-HIDDEN-LINKED-MIXED"
        pallet = InventoryPallet(
            pallet_code=pallet_code,
            location=location,
            location_occupancy_key="PRIMARY",
            status="active",
            is_current=True,
            version=1,
            created_by=admin.id,
        )
        db.add_all([location, pallet])
        db.flush()

        lots: list[InventoryLot] = []
        expected_quantities: dict[int, int] = {}
        for suffix, customer, product, quantity in (
            ("A", customer_a, product_a, 11),
            ("B", customer_b, product_b, 13),
        ):
            lot = InventoryLot(
                lot_number=f"FG-P149B-HIDDEN-{suffix}",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=quantity,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 8, 12),
                stock_date_accuracy="exact",
                stock_date_original_text="2026-08-12",
                last_movement_at=_now(),
                version=1,
            )
            lot.finished_detail = FinishedGoodsInventoryDetail(
                owner_customer_id=customer.id,
                owner_customer_name_snapshot=customer.name,
                is_general=False,
                product_id=product.id,
                inventory_code_snapshot=f"P149B-HIDDEN-{suffix}",
                product_name_snapshot=product.product_name,
            )
            lot.pallet_item = InventoryPalletItem(
                pallet_id=pallet.id,
                customer_id=customer.id,
                product_id=product.id,
                inventory_code=f"P149B-HIDDEN-{suffix}",
                customer_name_snapshot=customer.name,
                product_name=product.product_name,
                item_type="finished",
                quantity=quantity,
                unit="boxes",
                match_status="matched",
            )
            db.add(lot)
            lots.append(lot)
        db.flush()
        expected_quantities = {lot.id: int(lot.quantity_available) for lot in lots}
        db.commit()
        location_id = location.id
        pallet_id = pallet.id

    with TestClient(app) as client:
        _login(client, "n029-admin")
        admin_payload = _dashboard(client)
    admin_location = next(
        row for row in admin_payload["locations"] if row["location_id"] == location_id
    )
    assert admin_location["location_code"] == location_code
    assert [row["pallet_id"] for row in admin_location["pallets"]] == [pallet_id]
    assert {
        row["lot_id"]: row["quantity"]
        for row in admin_location["pallets"][0]["items"]
    } == expected_quantities

    with TestClient(app) as client:
        _login(client, "n029-scoped")
        scoped_after = _dashboard(client)
    assert location_id not in {row["location_id"] for row in scoped_after["locations"]}
    assert location_code not in {row["location_code"] for row in scoped_after["locations"]}
    assert pallet_id not in {
        pallet["pallet_id"]
        for location in scoped_after["locations"]
        for pallet in location.get("pallets", [])
    }
    assert set(expected_quantities).isdisjoint(
        {
            item["lot_id"]
            for location in scoped_after["locations"]
            for item in [
                *(item for pallet in location.get("pallets", []) for item in pallet["items"]),
                *location.get("loose_items", []),
            ]
        }
    )
    assert scoped_after["summary"]["active_lots"] == scoped_before["summary"]["active_lots"]
    assert scoped_after["summary"]["quantities"] == scoped_before["summary"]["quantities"]
    assert scoped_after["summary"] == scoped_before["summary"]
    assert scoped_after["floors"] == scoped_before["floors"]
    assert scoped_after["distribution"] == scoped_before["distribution"]
    assert scoped_after["age_distribution"] == scoped_before["age_distribution"]
    assert scoped_after["trend"] == scoped_before["trend"]
    assert scoped_after["throughput"] == scoped_before["throughput"]

    with factory() as db:
        pallet = db.get(InventoryPallet, pallet_id)
        staging = db.get(WarehouseLocation, ids["staging"])
        linked_lots = list(
            db.scalars(select(InventoryLot).where(InventoryLot.id.in_(expected_quantities)))
        )
        assert pallet is not None and staging is not None
        assert {row.id for row in linked_lots} == set(expected_quantities)
        pallet.location_id = staging.id
        pallet.location_occupancy_key = "P149B-SCOPE:HIDDEN-MIXED-LINKED"
        for lot in linked_lots:
            lot.warehouse_location_id = staging.id
        db.commit()

    with TestClient(app) as client:
        _login(client, "n029-scoped")
        shared_scoped = _dashboard(client)
    shared_dispatch = next(
        row
        for row in shared_scoped["locations"]
        if row["location_code"] == "F1-DISPATCH-01"
    )
    assert [row["pallet_id"] for row in shared_dispatch["pallets"]] == [
        ids["first_pallet"]
    ]
    assert [row["lot_id"] for row in shared_dispatch["loose_items"]] == [
        ids["legacy_loose"]
    ]
    assert pallet_id not in {row["pallet_id"] for row in shared_dispatch["pallets"]}
    assert set(expected_quantities).isdisjoint(
        {
            item["lot_id"]
            for item in [
                *(item for row in shared_dispatch["pallets"] for item in row["items"]),
                *shared_dispatch["loose_items"],
            ]
        }
    )
    assert shared_scoped["summary"] == scoped_before["summary"]
    assert shared_scoped["floors"] == scoped_before["floors"]
    assert shared_scoped["distribution"] == scoped_before["distribution"]
    assert shared_scoped["age_distribution"] == scoped_before["age_distribution"]
    assert shared_scoped["trend"] == scoped_before["trend"]
    assert shared_scoped["throughput"] == scoped_before["throughput"]


def test_each_dispatch_pallet_moves_independently_and_preserves_delivery_facts(
    dispatch_pallet_app,
) -> None:
    from app.api.deliveries import _inventory_sources_for_order_item, _pick_source_location

    app, factory, ids = dispatch_pallet_app
    with factory() as db:
        first_lot = db.get(InventoryLot, ids["first_lot"])
        second_lot = db.get(InventoryLot, ids["second_lot"])
        first_item = db.get(OrderItem, ids["first_item"])
        first_completion = db.get(ProductionCompletion, ids["first_completion"])
        assert all(row is not None for row in (first_lot, second_lot, first_item, first_completion))
        before_totals = _inventory_totals(db)
        before_reserved = _remaining_reserved(db)
        first_stock_date = first_lot.stock_date
        second_state = (
            second_lot.warehouse_location_id,
            second_lot.version,
            second_lot.quantity_available,
            second_lot.quantity_reserved,
        )
        order_state = (
            first_item.quantity,
            first_item.delivered_quantity,
            first_completion.status,
            first_completion.direct_delivery_quantity,
            first_completion.order_reserved_quantity,
        )
        sources_before = _inventory_sources_for_order_item(
            db,
            order_item=first_item,
            planned_delivery_quantity=int(first_item.quantity),
        )
        source_before = next(row for row in sources_before if row["lot_id"] == first_lot.id)
        pick_before = _pick_source_location(db, source=source_before)

    payload = {
        "idempotency_key": "p149b-move-one-dispatch-pallet",
        "confirmed": True,
        "items": [
            {
                "client_item_id": "dispatch-pallet-a",
                "operation": "pallet_move",
                "pallet_id": ids["first_pallet"],
                "expected_version": 1,
                "target_location_id": ids["floor3_target"],
            }
        ],
    }
    with TestClient(app) as client:
        _login(client, "n029-admin")
        first = client.post(MOVE_BATCH_URL, json=payload)
        assert first.status_code == 200, first.text
        replay = client.post(MOVE_BATCH_URL, json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        overview = _dashboard(client)

    dispatch = next(
        row for row in overview["locations"] if row["location_code"] == "F1-DISPATCH-01"
    )
    assert dispatch["pallet"]["pallet_id"] == ids["second_pallet"]
    assert [row["pallet_id"] for row in dispatch["pallets"]] == [
        ids["second_pallet"]
    ]

    with factory() as db:
        first_pallet = db.get(InventoryPallet, ids["first_pallet"])
        second_pallet = db.get(InventoryPallet, ids["second_pallet"])
        first_lot = db.get(InventoryLot, ids["first_lot"])
        second_lot = db.get(InventoryLot, ids["second_lot"])
        first_item = db.get(OrderItem, ids["first_item"])
        first_completion = db.get(ProductionCompletion, ids["first_completion"])
        assert all(
            row is not None
            for row in (
                first_pallet,
                second_pallet,
                first_lot,
                second_lot,
                first_item,
                first_completion,
            )
        )
        assert (
            first_pallet.location_id,
            first_pallet.location_occupancy_key,
            first_pallet.version,
        ) == (ids["floor3_target"], "PRIMARY", 2)
        assert first_lot.warehouse_location_id == ids["floor3_target"]
        assert first_lot.stock_date == first_stock_date
        assert (
            second_pallet.location_id,
            second_pallet.location_occupancy_key,
            second_pallet.version,
        ) == (ids["staging"], f"PRODUCTION_COMPLETION:{ids['second_completion']}", 1)
        assert (
            second_lot.warehouse_location_id,
            second_lot.version,
            second_lot.quantity_available,
            second_lot.quantity_reserved,
        ) == second_state
        assert _inventory_totals(db) == before_totals
        assert _remaining_reserved(db) == before_reserved
        assert (
            first_item.quantity,
            first_item.delivered_quantity,
            first_completion.status,
            first_completion.direct_delivery_quantity,
            first_completion.order_reserved_quantity,
        ) == order_state
        sources_after = _inventory_sources_for_order_item(
            db,
            order_item=first_item,
            planned_delivery_quantity=int(first_item.quantity),
        )
        source_after = next(row for row in sources_after if row["lot_id"] == first_lot.id)
        pick_after = _pick_source_location(db, source=source_after)
        assert source_after["quantity_to_pick_stock"] == source_before["quantity_to_pick_stock"]
        assert pick_after["pallet_id"] == pick_before["pallet_id"] == ids["first_pallet"]
        assert pick_after["location_id"] == ids["floor3_target"]
        assert pick_before["location_id"] == ids["staging"]
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0
        movements = list(
            db.scalars(
                select(InventoryLocationMovement).where(
                    InventoryLocationMovement.pallet_id == ids["first_pallet"],
                    InventoryLocationMovement.movement_type == "move",
                )
            )
        )
        assert len(movements) == 1
        assert (
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action_code == "warehouse.movement_batch.confirmed",
                    OperationLog.batch_id == "p149b-move-one-dispatch-pallet",
                )
            )
            == 1
        )


def test_p1_49b_move_mode_keeps_p1_49c_merge_when_stocktake_is_enabled() -> None:
    source = FRONTEND.read_text(encoding="utf-8")
    inventory_source = WAREHOUSE_INVENTORY.read_text(encoding="utf-8")
    assert 'value.permissions.includes("warehouse.execute")' in source
    assert 'setMapMode("move")' in source
    assert source.count('"/api/warehouse/twin-operations/move-batches"') == 1
    assert "inventoryLocationPallets," in source
    assert "() => inventoryLocationPallets(selectedLocation)" in source
    assert "selectedLocationPallets.map((pallet) =>" in source
    assert "palletMoveSource(selectedLocation, pallet)" in source
    assert "Array.isArray(location?.pallets)" in inventory_source
    assert "listed.length ? listed : location?.pallet ? [location.pallet] : []" in inventory_source
    assert "leftId - rightId" in inventory_source
    assert "pallet_move" in source
    assert "楼层" in source and "区域" in source and "具体货位" in source
    assert "P1_47D_ENABLED" not in source
    assert 'value.permissions.includes("warehouse.stocktake.submit")' in source
    assert source.count('"/api/warehouse/twin-operations/stocktake-batches"') == 1
    assert "const P1_49C_ENABLED = true;" in source
    assert "selectedMergePalletIds" not in source
    assert source.count('"/api/warehouse/pallets/merge-batches"') == 1


def test_p1_49b_fixture_never_uses_the_formal_database(dispatch_pallet_app) -> None:
    _app, factory, _ids = dispatch_pallet_app
    with factory() as db:
        database = Path(str(db.get_bind().url.database)).resolve()
    assert database.name == "n029-service.sqlite3"
    assert database.parent.name.startswith("test_")
    assert "carton_erp.sqlite3" not in str(database)
