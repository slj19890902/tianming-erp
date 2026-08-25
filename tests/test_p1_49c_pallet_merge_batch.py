from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

import app.api.warehouse as warehouse_api
import app.services.warehouse_pallet_merge_batch as pallet_merge_batch_service
from app.api.deliveries import (
    _inventory_sources_for_order_item,
    _pick_source_location,
)
from app.api.deps import get_db
from app.api.warehouse import router as warehouse_router
from app.core.security import hash_password
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import ProductionCompletion
from app.models.user import User
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLocationMovement,
    InventoryLot,
    InventoryLotTransfer,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseLocation,
)
from app.services import location_candidates
from app.services.production_workflow import CompletionCommand, complete_production_batch
from app.services.floor3_locations import move_pallet
from app.services.warehouse_inventory import release_finished_reservation
from test_n029_production_service import PASSWORD, _add_case, production_app


MERGE_BATCH_URL = "/api/warehouse/pallets/merge-batches"
CURRENT_MAP_REVISION = "p1-49c-test-map"
FRONTEND = (
    Path(__file__).resolve().parents[1]
    / "factory_twin"
    / "frontend"
    / "src"
    / "WarehouseTwinApp.tsx"
)


def _login(client: TestClient, username: str = "n029-admin") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    map_feature_id: str,
) -> WarehouseArea:
    area = WarehouseArea(
        floor=floor,
        area_code=area_code,
        area_name=f"{floor.floor_code}-{area_code}",
        construction_status="enabled",
    )
    area.storage_policy = WarehouseAreaStoragePolicy(
        map_feature_id=map_feature_id,
        allowed_inventory_types_json='["finished"]',
        storage_layout="pallet_ground",
        status="published",
        published_map_revision=CURRENT_MAP_REVISION,
        version=1,
    )
    return area


def _mapped_location(
    *,
    code: str,
    floor: int,
    area_code: str,
    source_version: str,
    sort_order: int,
) -> WarehouseLocation:
    location = WarehouseLocation(
        location_code=code,
        location_name=code,
        warehouse_type="finished",
        is_active=True,
        warehouse_floor=floor,
        area_code=area_code,
        storage_type="ground",
        placement_status="placed",
        is_temporary=False,
        source_version=source_version,
        sort_order=sort_order,
    )
    location.floor3_layout = Floor3LocationLayout(
        left_pct=Decimal("10"),
        top_pct=Decimal("10"),
        width_pct=Decimal("8"),
        height_pct=Decimal("8"),
        z_index=0,
        version=1,
        source_type="manual",
    )
    return location


def _published_ground_plan(
    db,
    *,
    area: WarehouseArea,
    locations: list[WarehouseLocation],
    operator_id: int,
    key: str,
) -> None:
    plan = WarehouseGroundLayoutPlan(
        area_id=area.id,
        status="published",
        target_slot_count=len(locations),
        numbering_origin="south",
        row_direction="from_aisle_inward",
        slot_direction="left_to_right",
        row_start_no=1,
        slot_start_no=1,
        draft_map_revision=CURRENT_MAP_REVISION,
        published_map_revision=CURRENT_MAP_REVISION,
        preview_fingerprint="c" * 64,
        version=1,
        publish_idempotency_key=f"p1-49c-{key}",
        publish_request_hash="d" * 64,
        updated_by=operator_id,
        published_by=operator_id,
        published_at=_now(),
    )
    db.add(plan)
    db.flush()
    db.add_all(
        [
            WarehouseGroundLayoutSlot(
                plan_id=plan.id,
                location_id=location.id,
                route_sequence=index,
                row_no=1,
                slot_no=index,
                x_mm=Decimal(1000 + index * 1400),
                y_mm=Decimal("1000"),
                width_mm=1200,
                depth_mm=1000,
            )
            for index, location in enumerate(locations, start=1)
        ]
    )


def _merge_payload(
    ids: dict,
    *,
    key: str,
    source_ids: list[int] | None = None,
    source_versions: list[int] | None = None,
    target_id: int | None = None,
    target_version: int | None = None,
) -> dict:
    pallets = list(source_ids or ids["source_pallets"])
    versions = list(source_versions or [1] * len(pallets))
    assert len(pallets) == len(versions)
    return {
        "idempotency_key": key,
        "confirmed": True,
        "target_pallet_id": target_id or ids["target_pallet"],
        "expected_target_version": target_version or 1,
        "sources": [
            {
                "client_item_id": f"source-{index}",
                "pallet_id": pallet_id,
                "expected_version": versions[index - 1],
            }
            for index, pallet_id in enumerate(pallets, start=1)
        ],
    }


def _lot_immutable(row: InventoryLot) -> tuple:
    return (
        int(row.id),
        row.lot_number,
        row.inventory_type,
        int(row.quantity_available or 0),
        int(row.quantity_reserved or 0),
        int(row.quantity_consumed or 0),
        int(row.quantity_damaged or 0),
        int(row.quantity_scrapped or 0),
        row.unit,
        row.status,
        row.source_type,
        row.source_ref_type,
        row.source_ref_id,
        row.stock_date,
        row.stock_date_accuracy,
        row.stock_date_original_text,
        row.finished_detail.owner_customer_id if row.finished_detail else None,
        row.finished_detail.product_id if row.finished_detail else None,
    )


def _reservation_snapshot(db, lot_ids: list[int]) -> list[tuple]:
    rows = db.scalars(
        select(InventoryReservation)
        .where(InventoryReservation.inventory_lot_id.in_(lot_ids))
        .order_by(InventoryReservation.id)
    ).all()
    return [
        (
            int(row.id),
            int(row.inventory_lot_id),
            row.reservation_type,
            row.order_id,
            row.order_item_id,
            int(row.reserved_stock_quantity or 0),
            int(row.credited_requirement_quantity or 0),
            int(row.consumed_stock_quantity or 0),
            int(row.released_stock_quantity or 0),
            int(row.consumed_requirement_quantity or 0),
            int(row.released_requirement_quantity or 0),
            row.status,
            row.idempotency_key,
        )
        for row in rows
    ]


def _order_trace_snapshot(db, order_item_ids: list[int]) -> list[tuple]:
    rows = db.scalars(
        select(OrderItem).where(OrderItem.id.in_(order_item_ids)).order_by(OrderItem.id)
    ).all()
    orders = {
        row.id: row
        for row in db.scalars(
            select(Order).where(Order.id.in_({item.order_id for item in rows}))
        ).all()
    }
    return [
        (
            int(row.id),
            int(row.order_id),
            orders[row.order_id].order_number,
            orders[row.order_id].customer_id,
            row.product_id,
            int(row.quantity or 0),
            int(row.delivered_quantity or 0),
            bool(row.is_force_closed),
            orders[row.order_id].status,
        )
        for row in rows
    ]


def _completion_snapshot(db, completion_ids: list[int]) -> list[tuple]:
    rows = db.scalars(
        select(ProductionCompletion)
        .where(ProductionCompletion.id.in_(completion_ids))
        .order_by(ProductionCompletion.id)
    ).all()
    return [
        (
            int(row.id),
            int(row.order_item_id),
            int(row.inventory_lot_id),
            int(row.quantity),
            int(row.order_reserved_quantity),
            int(row.direct_delivery_quantity),
            int(row.stock_quantity),
            row.initial_disposition,
            row.status,
        )
        for row in rows
    ]


def _delivery_trace(db, order_item_ids: list[int]) -> dict[int, list[tuple]]:
    result: dict[int, list[tuple]] = {}
    for item_id in order_item_ids:
        item = db.get(OrderItem, item_id)
        assert item is not None
        sources = _inventory_sources_for_order_item(
            db,
            order_item=item,
            planned_delivery_quantity=int(item.quantity or 0),
        )
        result[item_id] = [
            (
                int(source["reservation_id"]),
                int(source["lot_id"]),
                source["source_type"],
                int(source["remaining_reserved_stock_quantity"]),
                int(source["covered_requirement_quantity"]),
                int(source["quantity_to_pick_stock"]),
                int(source["quantity_to_pick_requirement"]),
            )
            for source in sources
        ]
    return result


def _write_snapshot(db, pallet_ids: list[int], lot_ids: list[int]) -> dict:
    pallets = db.scalars(
        select(InventoryPallet)
        .where(InventoryPallet.id.in_(pallet_ids))
        .order_by(InventoryPallet.id)
    ).all()
    lots = db.scalars(
        select(InventoryLot).where(InventoryLot.id.in_(lot_ids)).order_by(InventoryLot.id)
    ).all()
    pallet_items = db.scalars(
        select(InventoryPalletItem)
        .where(InventoryPalletItem.inventory_lot_id.in_(lot_ids))
        .order_by(InventoryPalletItem.id)
    ).all()
    return {
        "pallets": [
            (
                int(row.id),
                row.location_id,
                row.location_occupancy_key,
                row.status,
                bool(row.is_current),
                int(row.version),
                row.closed_at,
            )
            for row in pallets
        ],
        "lots": [
            (
                *_lot_immutable(row),
                int(row.warehouse_location_id),
                int(row.version),
                row.last_movement_at,
            )
            for row in lots
        ],
        "items": [
            (
                int(row.id),
                int(row.pallet_id),
                row.inventory_lot_id,
                row.customer_id,
                row.product_id,
                row.item_type,
                str(row.quantity),
                row.unit,
                row.match_status,
            )
            for row in pallet_items
        ],
        "reservations": _reservation_snapshot(db, lot_ids),
        "location_movements": int(
            db.scalar(select(func.count(InventoryLocationMovement.id))) or 0
        ),
        "inventory_movements": int(
            db.scalar(select(func.count(InventoryMovement.id))) or 0
        ),
        "lot_transfers": int(db.scalar(select(func.count(InventoryLotTransfer.id))) or 0),
        "merge_audits": int(
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action_code
                    == "warehouse.pallet_merge_batch.confirmed"
                )
            )
            or 0
        ),
    }


@pytest.fixture()
def pallet_merge_app(production_app, monkeypatch: pytest.MonkeyPatch):
    identities = {
        1: {
            "revision": CURRENT_MAP_REVISION,
            "zones_by_id": {
                "zone-p149c-dispatch": "DISPATCH",
                "zone-p149c-1f-fg": "P149C-FG",
            },
            "zone_ids_by_area": {
                "DISPATCH": ("zone-p149c-dispatch",),
                "P149C-FG": ("zone-p149c-1f-fg",),
            },
        },
        2: {
            "revision": CURRENT_MAP_REVISION,
            "zones_by_id": {
                "zone-p149c-2f-out-of-scope": "P149C-2F-OUT-OF-SCOPE"
            },
            "zone_ids_by_area": {
                "P149C-2F-OUT-OF-SCOPE": (
                    "zone-p149c-2f-out-of-scope",
                )
            },
        },
        3: {
            "revision": CURRENT_MAP_REVISION,
            "zones_by_id": {
                "zone-p149c-3f-a1": "P149C-A1",
                "zone-p149c-3f-target": "P149C-TARGET",
            },
            "zone_ids_by_area": {
                "P149C-A1": ("zone-p149c-3f-a1",),
                "P149C-TARGET": ("zone-p149c-3f-target",),
            },
        },
    }
    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        lambda floor_number: identities.get(int(floor_number)),
    )
    app, factory, base_ids = production_app
    app.include_router(warehouse_router, prefix="/api/warehouse")

    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "n029-admin"))
        scoped = db.scalar(select(User).where(User.username == "n029-scoped"))
        customer_a = db.get(Customer, base_ids["customer_a"])
        customer_b = db.get(Customer, base_ids["customer_b"])
        product_a = db.get(Product, base_ids["product_a"])
        product_a3 = db.scalar(
            select(Product).where(
                Product.customer_id == base_ids["customer_a"],
                Product.id != base_ids["product_a"],
            )
        )
        product_b = db.scalar(
            select(Product).where(Product.customer_id == base_ids["customer_b"])
        )
        staging = db.get(WarehouseLocation, base_ids["staging"])
        assert all(
            row is not None
            for row in (
                admin,
                scoped,
                customer_a,
                customer_b,
                product_a,
                product_a3,
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
        second_admin = User(
            username="p149c-second-admin",
            password_hash=hash_password(PASSWORD),
            role="admin",
            real_name="P1-49C Second Admin",
            must_change_password=False,
            customer_access_mode="all",
        )
        db.add(second_admin)

        cases = []
        for key, product, quantity in (
            ("p149c-source-dispatch", product_a, 6),
            ("p149c-source-1f", product_a3, 7),
            ("p149c-source-3f", product_a, 8),
            ("p149c-target", product_a3, 9),
        ):
            order, order_item, task = _add_case(
                db,
                key=key,
                customer=customer_a,
                product=product,
                quantity=quantity,
            )
            completion = complete_production_batch(
                db,
                idempotency_key=f"{key}-completion",
                commands=[_direct_command(task, quantity)],
                operator_id=admin.id,
            ).completions[0]
            lot = db.get(InventoryLot, completion.inventory_lot_id)
            assert lot is not None
            assert int(lot.quantity_reserved or 0) == quantity
            assert db.scalar(
                select(InventoryReservation.id).where(
                    InventoryReservation.inventory_lot_id == lot.id,
                    InventoryReservation.order_item_id == order_item.id,
                    InventoryReservation.reservation_type == "finished_order",
                )
            ) is not None
            cases.append((order, order_item, completion))

        foreign_order, foreign_item, foreign_task = _add_case(
            db,
            key="p149c-foreign",
            customer=customer_b,
            product=product_b,
            quantity=11,
        )
        foreign_completion = complete_production_batch(
            db,
            idempotency_key="p149c-foreign-completion",
            commands=[_direct_command(foreign_task, 11)],
            operator_id=admin.id,
        ).completions[0]

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
            map_feature_id="zone-p149c-dispatch",
        )
        floor1_area = _published_area(
            floor1,
            area_code="P149C-FG",
            map_feature_id="zone-p149c-1f-fg",
        )
        floor3_area = _published_area(
            floor3,
            area_code="P149C-A1",
            map_feature_id="zone-p149c-3f-a1",
        )
        target_area = _published_area(
            floor3,
            area_code="P149C-TARGET",
            map_feature_id="zone-p149c-3f-target",
        )
        db.add_all(
            [floor1, floor3, dispatch_area, floor1_area, floor3_area, target_area]
        )
        db.flush()
        staging.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal("4"),
            top_pct=Decimal("4"),
            width_pct=Decimal("12"),
            height_pct=Decimal("12"),
            z_index=0,
            version=1,
            source_type="manual",
            created_by=admin.id,
        )
        floor1_source = _mapped_location(
            code="1F-P149C-SOURCE",
            floor=1,
            area_code="P149C-FG",
            source_version="TWIN_V1",
            sort_order=14901,
        )
        floor3_source = _mapped_location(
            code="3F-P149C-SOURCE",
            floor=3,
            area_code="P149C-A1",
            source_version="TWIN_V1",
            sort_order=14902,
        )
        target_location = _mapped_location(
            code="3F-P149C-TARGET",
            floor=3,
            area_code="P149C-TARGET",
            source_version="TWIN_V1",
            sort_order=14903,
        )
        orphan_location = _mapped_location(
            code="1F-P149C-ORPHAN",
            floor=1,
            area_code="P149C-FG",
            source_version="TWIN_V1",
            sort_order=14904,
        )
        db.add_all(
            [floor1_source, floor3_source, target_location, orphan_location]
        )
        db.flush()
        _published_ground_plan(
            db,
            area=floor1_area,
            locations=[floor1_source, orphan_location],
            operator_id=admin.id,
            key="floor1-sources",
        )
        _published_ground_plan(
            db,
            area=floor3_area,
            locations=[floor3_source],
            operator_id=admin.id,
            key="floor3-source",
        )
        _published_ground_plan(
            db,
            area=target_area,
            locations=[target_location],
            operator_id=admin.id,
            key="floor3-target",
        )

        placements = [staging, floor1_source, floor3_source, target_location]
        pallet_ids: list[int] = []
        lot_ids: list[int] = []
        for (_order, _item, completion), location in zip(cases, placements, strict=True):
            lot = db.get(InventoryLot, completion.inventory_lot_id)
            assert lot is not None and lot.pallet_item is not None
            pallet = lot.pallet_item.pallet
            assert pallet is not None
            if location.id != staging.id:
                pallet.location_id = location.id
                pallet.location_occupancy_key = "PRIMARY"
                lot.warehouse_location_id = location.id
                lot.last_movement_at = _now()
            pallet_ids.append(int(pallet.id))
            lot_ids.append(int(lot.id))

        foreign_lot = db.get(InventoryLot, foreign_completion.inventory_lot_id)
        assert foreign_lot is not None and foreign_lot.pallet_item is not None
        foreign_pallet = foreign_lot.pallet_item.pallet
        assert foreign_pallet is not None
        db.commit()

        ids = {
            **base_ids,
            "source_pallets": pallet_ids[:3],
            "target_pallet": pallet_ids[3],
            "all_pallets": pallet_ids,
            "source_lots": lot_ids[:3],
            "target_lot": lot_ids[3],
            "all_lots": lot_ids,
            "order_items": [int(row[1].id) for row in cases],
            "completions": [int(row[2].id) for row in cases],
            "foreign_pallet": int(foreign_pallet.id),
            "foreign_lot": int(foreign_lot.id),
            "foreign_item": int(foreign_item.id),
            "floor1_source": int(floor1_source.id),
            "floor3_source": int(floor3_source.id),
            "target_location": int(target_location.id),
            "target_area": int(target_area.id),
            "orphan_location": int(orphan_location.id),
            "second_admin": int(second_admin.id),
            "product_b": int(product_b.id),
        }
    yield app, factory, ids


def test_three_sources_merge_across_dispatch_1f_3f_preserves_all_facts(
    pallet_merge_app,
) -> None:
    app, factory, ids = pallet_merge_app
    key = "p149c-three-source-success"
    payload = _merge_payload(ids, key=key)

    with factory() as db:
        lots_before = [
            _lot_immutable(row)
            for row in db.scalars(
                select(InventoryLot)
                .where(InventoryLot.id.in_(ids["all_lots"]))
                .order_by(InventoryLot.id)
            ).all()
        ]
        reservations_before = _reservation_snapshot(db, ids["all_lots"])
        orders_before = _order_trace_snapshot(db, ids["order_items"])
        completions_before = _completion_snapshot(db, ids["completions"])
        delivery_before = _delivery_trace(db, ids["order_items"])
        total_before = tuple(
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
        movements_before = int(
            db.scalar(select(func.count(InventoryLocationMovement.id))) or 0
        )
        stock_movements_before = int(
            db.scalar(select(func.count(InventoryMovement.id))) or 0
        )
        last_stock_movement_id_before = int(
            db.scalar(select(func.max(InventoryMovement.id))) or 0
        )
        transfers_before = int(
            db.scalar(select(func.count(InventoryLotTransfer.id))) or 0
        )

    with TestClient(app) as client:
        _login(client)
        response = client.post(MERGE_BATCH_URL, json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["batch_id"] == key
        assert result["idempotent_replay"] is False
        assert len(result["request_hash"]) == 64
        assert [row["client_item_id"] for row in result["sources"]] == [
            "source-1",
            "source-2",
            "source-3",
        ]

    with factory() as db:
        sources = [db.get(InventoryPallet, pallet_id) for pallet_id in ids["source_pallets"]]
        target = db.get(InventoryPallet, ids["target_pallet"])
        assert target is not None and all(source is not None for source in sources)
        assert all(
            source.status == "closed"
            and source.is_current is False
            and source.location_id is None
            and source.closed_at is not None
            and source.version == 2
            for source in sources
        )
        assert target.status == "active"
        assert target.is_current is True
        assert target.location_id == ids["target_location"]
        assert target.location_occupancy_key == "PRIMARY"
        assert target.version == 1 + len(ids["source_pallets"])
        assert sorted(int(row.inventory_lot_id) for row in target.items) == sorted(
            ids["all_lots"]
        )
        lots_after_rows = db.scalars(
            select(InventoryLot)
            .where(InventoryLot.id.in_(ids["all_lots"]))
            .order_by(InventoryLot.id)
        ).all()
        assert [_lot_immutable(row) for row in lots_after_rows] == lots_before
        assert {row.warehouse_location_id for row in lots_after_rows} == {
            ids["target_location"]
        }
        occupancy = db.scalar(
            select(WarehouseGroundOccupancy).where(
                WarehouseGroundOccupancy.pallet_id == target.id,
                WarehouseGroundOccupancy.status == "active",
            )
        )
        assert occupancy is not None
        assert occupancy.primary_location_id == ids["target_location"]
        assert occupancy.capacity_quantity >= sum(
            int(row.quantity_available or 0)
            + int(row.quantity_reserved or 0)
            + int(row.quantity_damaged or 0)
            for row in lots_after_rows
        )
        assert db.scalar(
            select(WarehouseGroundOccupancySlot.id).where(
                WarehouseGroundOccupancySlot.occupancy_id == occupancy.id,
                WarehouseGroundOccupancySlot.location_id == ids["target_location"],
                WarehouseGroundOccupancySlot.status == "active",
            )
        ) is not None
        assert _reservation_snapshot(db, ids["all_lots"]) == reservations_before
        assert _order_trace_snapshot(db, ids["order_items"]) == orders_before
        assert _completion_snapshot(db, ids["completions"]) == completions_before
        assert _delivery_trace(db, ids["order_items"]) == delivery_before
        for item_id in ids["order_items"]:
            for source in _inventory_sources_for_order_item(
                db,
                order_item=db.get(OrderItem, item_id),
                planned_delivery_quantity=int(db.get(OrderItem, item_id).quantity),
            ):
                location = _pick_source_location(db, source=source)
                assert location["location_id"] == ids["target_location"]
                assert location["pallet_id"] == ids["target_pallet"]
        total_after = tuple(
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
        assert total_after == total_before
        assert int(db.scalar(select(func.count(InventoryLotTransfer.id))) or 0) == transfers_before
        assert (
            int(db.scalar(select(func.count(InventoryLocationMovement.id))) or 0)
            == movements_before + 6
        )
        assert (
            int(db.scalar(select(func.count(InventoryMovement.id))) or 0)
            == stock_movements_before + 3
        )
        new_stock_movements = db.scalars(
            select(InventoryMovement)
            .where(InventoryMovement.id > last_stock_movement_id_before)
            .order_by(InventoryMovement.id)
        ).all()
        assert len(new_stock_movements) == 3
        assert {row.movement_type for row in new_stock_movements} == {
            "location_transfer"
        }
        assert all(
            (
                row.before_available,
                row.before_reserved,
                row.before_consumed,
                row.before_damaged,
                row.before_scrapped,
            )
            == (
                row.after_available,
                row.after_reserved,
                row.after_consumed,
                row.after_damaged,
                row.after_scrapped,
            )
            for row in new_stock_movements
        )
        audit = db.scalar(
            select(OperationLog).where(OperationLog.batch_id == key)
        )
        assert audit is not None
        assert audit.result == "success"
        assert audit.actor_user_id_snapshot is not None
        after_success = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])

    with TestClient(app) as client:
        _login(client)
        replay = client.post(MERGE_BATCH_URL, json=payload)
        assert replay.status_code == 200, replay.text
        replay_result = replay.json()
        assert replay_result["batch_id"] == result["batch_id"]
        assert replay_result["request_hash"] == result["request_hash"]
        assert replay_result["sources"] == result["sources"]
        assert replay_result["idempotent_replay"] is True

    with factory() as db:
        assert _write_snapshot(db, ids["all_pallets"], ids["all_lots"]) == after_success


def test_twin_ground_move_releases_source_and_creates_one_target_occupancy(
    pallet_merge_app,
) -> None:
    _app, factory, ids = pallet_merge_app
    pallet_id = ids["source_pallets"][1]
    lot_id = ids["source_lots"][1]
    with factory() as db:
        pallet = db.get(InventoryPallet, pallet_id)
        lot = db.get(InventoryLot, lot_id)
        admin = db.scalar(select(User).where(User.username == "n029-admin"))
        assert pallet is not None and lot is not None and admin is not None
        source_location_id = int(pallet.location_id)
        source_occupancy = WarehouseGroundOccupancy(
            pallet_id=pallet.id,
            primary_location_id=source_location_id,
            customer_id=int(lot.finished_detail.owner_customer_id),
            product_id=int(lot.finished_detail.product_id),
            footprint_kind="single",
            capacity_quantity=(
                int(lot.quantity_available or 0)
                + int(lot.quantity_reserved or 0)
                + int(lot.quantity_damaged or 0)
            ),
            status="active",
            version=1,
            created_by=admin.id,
        )
        db.add(source_occupancy)
        db.flush()
        db.add(
            WarehouseGroundOccupancySlot(
                occupancy_id=source_occupancy.id,
                location_id=source_location_id,
                slot_sequence=1,
                status="active",
            )
        )
        db.commit()

        result = move_pallet(
            db,
            pallet_id=pallet.id,
            expected_version=1,
            to_location_id=ids["orphan_location"],
            remarks="P1-102 TWIN 地堆转位",
            operator_id=admin.id,
            idempotency_key="p1102-twin-ground-move",
            require_published_target=True,
            expected_target_layout_version=1,
        )
        db.commit()
        assert result.replayed is False
        db.refresh(source_occupancy)
        assert source_occupancy.status == "released"
        active = db.scalar(
            select(WarehouseGroundOccupancy).where(
                WarehouseGroundOccupancy.pallet_id == pallet.id,
                WarehouseGroundOccupancy.status == "active",
            )
        )
        assert active is not None
        assert active.primary_location_id == ids["orphan_location"]
        active_count = int(
            db.scalar(
                select(func.count(WarehouseGroundOccupancy.id)).where(
                    WarehouseGroundOccupancy.pallet_id == pallet.id,
                    WarehouseGroundOccupancy.status == "active",
                )
            )
            or 0
        )
        assert active_count == 1

        replay = move_pallet(
            db,
            pallet_id=pallet.id,
            expected_version=1,
            to_location_id=ids["orphan_location"],
            remarks="P1-102 TWIN 地堆转位",
            operator_id=admin.id,
            idempotency_key="p1102-twin-ground-move",
            require_published_target=True,
            expected_target_layout_version=1,
        )
        assert replay.replayed is True
        assert int(
            db.scalar(
                select(func.count(WarehouseGroundOccupancy.id)).where(
                    WarehouseGroundOccupancy.pallet_id == pallet.id,
                    WarehouseGroundOccupancy.status == "active",
                )
            )
            or 0
        ) == 1


def test_same_key_different_payload_and_cross_actor_do_not_replay_or_leak(
    pallet_merge_app,
) -> None:
    app, factory, ids = pallet_merge_app
    key = "p149c-replay-ownership"
    payload = _merge_payload(ids, key=key)
    with TestClient(app) as client:
        _login(client)
        success = client.post(MERGE_BATCH_URL, json=payload)
        assert success.status_code == 200, success.text

    with factory() as db:
        after_success = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])

    changed = json.loads(json.dumps(payload))
    changed["sources"] = list(reversed(changed["sources"]))
    with TestClient(app) as client:
        _login(client)
        conflict = client.post(MERGE_BATCH_URL, json=changed)
        assert conflict.status_code == 409, conflict.text
        _login(client, "p149c-second-admin")
        cross_actor = client.post(MERGE_BATCH_URL, json=payload)
        assert cross_actor.status_code == 409, cross_actor.text
    assert key not in cross_actor.text
    assert success.json()["request_hash"] not in cross_actor.text

    with factory() as db:
        assert _write_snapshot(db, ids["all_pallets"], ids["all_lots"]) == after_success
        assert (
            int(
                db.scalar(
                    select(func.count(OperationLog.id)).where(OperationLog.batch_id == key)
                )
                or 0
            )
            == 1
        )


def test_exact_replay_rechecks_revoked_customer_scope_before_returning_result(
    pallet_merge_app,
) -> None:
    app, factory, ids = pallet_merge_app
    key = "p149c-replay-after-scope-revoked"
    payload = _merge_payload(ids, key=key)
    with TestClient(app) as client:
        _login(client, "n029-scoped")
        success = client.post(MERGE_BATCH_URL, json=payload)
        assert success.status_code == 200, success.text
        successful_result = success.json()

    with factory() as db:
        scoped = db.scalar(select(User).where(User.username == "n029-scoped"))
        assert scoped is not None
        rows = db.scalars(
            select(UserCustomerScope).where(UserCustomerScope.user_id == scoped.id)
        ).all()
        assert {row.customer_id for row in rows} == {ids["customer_a"]}
        for row in rows:
            db.delete(row)
        db.commit()
        after_success = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])
        merge_audit_ids = list(
            db.scalars(
                select(OperationLog.id)
                .where(OperationLog.batch_id == key)
                .order_by(OperationLog.id)
            ).all()
        )
        assert len(merge_audit_ids) == 1

    with TestClient(app) as client:
        _login(client, "n029-scoped")
        replay = client.post(MERGE_BATCH_URL, json=payload)
    assert replay.status_code == 403, replay.text
    assert key not in replay.text
    assert successful_result["request_hash"] not in replay.text
    for source_result in successful_result["sources"]:
        assert str(source_result["source_movement_id"]) not in replay.text
        assert str(source_result["target_movement_id"]) not in replay.text

    with factory() as db:
        assert _write_snapshot(db, ids["all_pallets"], ids["all_lots"]) == after_success
        assert list(
            db.scalars(
                select(OperationLog.id)
                .where(OperationLog.batch_id == key)
                .order_by(OperationLog.id)
            ).all()
        ) == merge_audit_ids


def test_stale_last_source_rolls_back_every_source_target_lot_and_audit(
    pallet_merge_app,
) -> None:
    app, factory, ids = pallet_merge_app
    with factory() as db:
        before = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])
    payload = _merge_payload(
        ids,
        key="p149c-stale-last-source",
        source_versions=[1, 1, 2],
    )
    with TestClient(app) as client:
        _login(client)
        response = client.post(MERGE_BATCH_URL, json=payload)
    assert response.status_code == 409, response.text
    with factory() as db:
        assert _write_snapshot(db, ids["all_pallets"], ids["all_lots"]) == before
        assert db.scalar(
            select(OperationLog.id).where(
                OperationLog.batch_id == "p149c-stale-last-source"
            )
        ) is None


def test_sqlite_concurrent_reservation_change_and_merge_have_one_winner(
    pallet_merge_app,
) -> None:
    app, factory, ids = pallet_merge_app
    key = "p149c-sqlite-lot-version-race"
    source_lot_id = ids["source_lots"][1]
    with factory() as db:
        reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.inventory_lot_id == source_lot_id,
                InventoryReservation.reservation_type == "finished_order",
            )
        )
        source_lot = db.get(InventoryLot, source_lot_id)
        assert reservation is not None and source_lot is not None
        lot_before = _lot_immutable(source_lot)
        reservation_before = _reservation_snapshot(db, [source_lot_id])
        source_location_id = int(source_lot.warehouse_location_id)
        source_pallet_id = ids["source_pallets"][1]
        source_version_before = int(source_lot.version)
        write_before = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])
        merge_audits_before = int(
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action_code
                    == "warehouse.pallet_merge_batch.confirmed"
                )
            )
            or 0
        )
        reservation_id = int(reservation.id)

    reservation_written = Event()
    merge_finished = Event()
    original_get_db_override = app.dependency_overrides[get_db]

    def low_wait_get_db():
        with factory() as db:
            # The reservation transaction deliberately holds SQLite's writer
            # lock.  A short per-request timeout makes this concurrency gate
            # deterministic without weakening the production engine setting.
            db.execute(text("PRAGMA busy_timeout = 250"))
            yield db

    app.dependency_overrides[get_db] = low_wait_get_db

    def release_reservation() -> tuple[str, int]:
        with factory() as db:
            try:
                release_finished_reservation(
                    db,
                    reservation_id=reservation_id,
                    operator_id=ids["second_admin"],
                    release_reason="P1-49C SQLite concurrent reservation release",
                    idempotency_key="p149c-concurrent-release",
                    allow_downstream=True,
                    allow_production_reversal=True,
                )
                db.flush()
                reservation_written.set()
                assert merge_finished.wait(timeout=10)
                db.commit()
                return "reservation", 200
            finally:
                reservation_written.set()

    def merge_batch() -> tuple[str, int]:
        assert reservation_written.wait(timeout=10)
        try:
            response = client.post(
                MERGE_BATCH_URL,
                json=_merge_payload(ids, key=key),
            )
            return "merge", response.status_code
        finally:
            merge_finished.set()

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            _login(client)
            with ThreadPoolExecutor(max_workers=2) as executor:
                reservation_future = executor.submit(release_reservation)
                merge_future = executor.submit(merge_batch)
                reservation_outcome = reservation_future.result(timeout=30)
                merge_outcome = merge_future.result(timeout=30)
    finally:
        app.dependency_overrides[get_db] = original_get_db_override

    outcomes = [reservation_outcome, merge_outcome]
    assert sum(status == 200 for _name, status in outcomes) == 1
    assert reservation_outcome == ("reservation", 200)
    assert merge_outcome == ("merge", 409)
    with factory() as db:
        source_lot = db.get(InventoryLot, source_lot_id)
        source_pallet = db.get(InventoryPallet, source_pallet_id)
        reservation = db.get(InventoryReservation, reservation_id)
        assert source_lot is not None and source_pallet is not None
        assert reservation is not None
        assert source_lot.warehouse_location_id == source_location_id
        assert source_lot.version == source_version_before + 1
        assert source_pallet.status == "active"
        assert source_pallet.is_current is True
        assert source_pallet.location_id == source_location_id
        assert source_lot.quantity_reserved == 0
        assert source_lot.quantity_available == int(lot_before[3]) + int(lot_before[4])
        assert reservation.status == "released"
        assert reservation.released_stock_quantity == reservation.reserved_stock_quantity
        release_movement = db.scalar(
            select(InventoryMovement).where(
                InventoryMovement.idempotency_key == "p149c-concurrent-release"
            )
        )
        assert release_movement is not None
        assert release_movement.movement_type == "release_reserve"
        assert release_movement.before_reserved == int(lot_before[4])
        assert release_movement.after_reserved == 0
        assert release_movement.before_available == int(lot_before[3])
        assert release_movement.after_available == int(lot_before[3]) + int(lot_before[4])
        write_after = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])
        assert write_after["pallets"] == write_before["pallets"]
        assert write_after["items"] == write_before["items"]
        assert write_after["location_movements"] == write_before["location_movements"]
        assert write_after["lot_transfers"] == write_before["lot_transfers"]
        assert write_after["inventory_movements"] == (
            write_before["inventory_movements"] + 1
        )
        before_lots = {row[0]: row for row in write_before["lots"]}
        after_lots = {row[0]: row for row in write_after["lots"]}
        assert {
            lot_id: row
            for lot_id, row in after_lots.items()
            if lot_id != source_lot_id
        } == {
            lot_id: row
            for lot_id, row in before_lots.items()
            if lot_id != source_lot_id
        }
        assert int(
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action_code
                    == "warehouse.pallet_merge_batch.confirmed"
                )
            )
            or 0
        ) == merge_audits_before
        assert db.scalar(
            select(OperationLog.id).where(OperationLog.batch_id == key)
        ) is None
        assert _reservation_snapshot(db, [source_lot_id]) != reservation_before


def test_second_source_runtime_failure_rolls_back_first_real_merge_flush(
    pallet_merge_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, factory, ids = pallet_merge_app
    key = "p149c-second-source-runtime-failure"
    with factory() as db:
        before = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])

    original = pallet_merge_batch_service.merge_pallet_remaining_goods
    calls = 0

    def fail_on_second_source(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second source failure after first flush")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        pallet_merge_batch_service,
        "merge_pallet_remaining_goods",
        fail_on_second_source,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        response = client.post(
            MERGE_BATCH_URL,
            json=_merge_payload(ids, key=key),
        )
    assert response.status_code == 500, response.text
    assert calls == 2

    with factory() as db:
        assert _write_snapshot(db, ids["all_pallets"], ids["all_lots"]) == before
        assert db.scalar(
            select(OperationLog.id).where(OperationLog.batch_id == key)
        ) is None


def test_audit_append_failure_rolls_back_all_completed_source_merges(
    pallet_merge_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, factory, ids = pallet_merge_app
    key = "p149c-audit-append-failure"
    with factory() as db:
        before = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])

    def fail_audit_append(*args, **kwargs):
        raise RuntimeError("injected audit append failure after merge execution")

    monkeypatch.setattr(warehouse_api, "append_audit_event", fail_audit_append)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        response = client.post(
            MERGE_BATCH_URL,
            json=_merge_payload(ids, key=key),
        )
    assert response.status_code == 500, response.text

    with factory() as db:
        assert _write_snapshot(db, ids["all_pallets"], ids["all_lots"]) == before
        assert db.scalar(
            select(OperationLog.id).where(OperationLog.batch_id == key)
        ) is None


def test_customer_scope_rejects_mixed_customer_batch_without_any_write(
    pallet_merge_app,
) -> None:
    app, factory, ids = pallet_merge_app
    pallet_ids = [*ids["source_pallets"][:2], ids["foreign_pallet"]]
    lot_ids = [*ids["source_lots"][:2], ids["foreign_lot"], ids["target_lot"]]
    with factory() as db:
        before = _write_snapshot(
            db,
            [*pallet_ids, ids["target_pallet"]],
            lot_ids,
        )
    payload = _merge_payload(
        ids,
        key="p149c-scope-denied",
        source_ids=pallet_ids,
    )
    with TestClient(app) as client:
        _login(client, "n029-scoped")
        response = client.post(MERGE_BATCH_URL, json=payload)
    assert response.status_code == 403, response.text
    with factory() as db:
        assert _write_snapshot(
            db,
            [*pallet_ids, ids["target_pallet"]],
            lot_ids,
        ) == before
        assert db.scalar(
            select(OperationLog.id).where(OperationLog.batch_id == "p149c-scope-denied")
        ) is None


@pytest.mark.parametrize(
    "invalid_variant",
    (
        "snapshot_only",
        "non_finished_item",
        "unmatched_item",
        "lot_location_mismatch",
        "damaged_lot",
        "incompatible_frozen",
        "incompatible_unit",
        "product_owner_mismatch",
    ),
)
def test_strict_official_and_compatibility_gate_rolls_back_without_audit(
    pallet_merge_app,
    invalid_variant: str,
) -> None:
    app, factory, ids = pallet_merge_app
    with factory() as db:
        source_pallet = db.get(InventoryPallet, ids["source_pallets"][1])
        source_lot = db.get(InventoryLot, ids["source_lots"][1])
        source_item = source_lot.pallet_item
        assert source_pallet is not None and source_item is not None
        if invalid_variant == "snapshot_only":
            source_item.inventory_lot_id = None
        elif invalid_variant == "non_finished_item":
            source_item.item_type = "semi_finished"
        elif invalid_variant == "unmatched_item":
            source_item.match_status = "pending"
        elif invalid_variant == "lot_location_mismatch":
            source_lot.warehouse_location_id = ids["orphan_location"]
        elif invalid_variant == "damaged_lot":
            source_lot.quantity_damaged = 1
        elif invalid_variant == "incompatible_frozen":
            source_lot.status = "frozen"
        elif invalid_variant == "incompatible_unit":
            source_lot.unit = "sheets"
            source_item.unit = "sheets"
        elif invalid_variant == "product_owner_mismatch":
            source_item.product_id = ids["product_b"]
        db.commit()
        before = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])

    key = f"p149c-invalid-{invalid_variant}"
    with TestClient(app) as client:
        _login(client)
        response = client.post(MERGE_BATCH_URL, json=_merge_payload(ids, key=key))
    assert response.status_code == 409, response.text
    with factory() as db:
        assert _write_snapshot(db, ids["all_pallets"], ids["all_lots"]) == before
        assert db.scalar(
            select(OperationLog.id).where(OperationLog.batch_id == key)
        ) is None


@pytest.mark.parametrize(
    "target_variant",
    ("unpublished", "unmapped", "disabled", "rack"),
)
def test_strict_target_location_gate_rolls_back_entire_batch(
    pallet_merge_app,
    target_variant: str,
) -> None:
    app, factory, ids = pallet_merge_app
    with factory() as db:
        target = db.get(WarehouseLocation, ids["target_location"])
        area = db.get(WarehouseArea, ids["target_area"])
        assert target is not None and area is not None and area.storage_policy is not None
        if target_variant == "unpublished":
            area.storage_policy.status = "draft"
            area.storage_policy.published_map_revision = None
            area.storage_policy.draft_map_revision = "p1-49c-draft"
        elif target_variant == "unmapped":
            target.floor3_layout = None
        elif target_variant == "disabled":
            target.is_active = False
        elif target_variant == "rack":
            target.storage_type = "rack"
        db.commit()
        before = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])
    key = f"p149c-target-{target_variant}"
    with TestClient(app) as client:
        _login(client)
        response = client.post(MERGE_BATCH_URL, json=_merge_payload(ids, key=key))
    assert response.status_code == 409, response.text
    with factory() as db:
        assert _write_snapshot(db, ids["all_pallets"], ids["all_lots"]) == before
        assert db.scalar(
            select(OperationLog.id).where(OperationLog.batch_id == key)
        ) is None


def test_published_mapped_2f_twin_location_is_outside_merge_scope_without_write(
    pallet_merge_app,
) -> None:
    app, factory, ids = pallet_merge_app
    key = "p149c-out-of-scope-2f-twin"
    source_pallet_id = ids["source_pallets"][1]
    source_lot_id = ids["source_lots"][1]
    with factory() as db:
        floor2 = WarehouseFloor(
            floor_code="2F",
            floor_name="二楼",
            floor_number=2,
            construction_status="enabled",
        )
        area2 = _published_area(
            floor2,
            area_code="P149C-2F-OUT-OF-SCOPE",
            map_feature_id="zone-p149c-2f-out-of-scope",
        )
        location2 = _mapped_location(
            code="2F-P149C-OUT-OF-SCOPE",
            floor=2,
            area_code=area2.area_code,
            source_version="TWIN_V1",
            sort_order=14920,
        )
        db.add_all([floor2, area2, location2])
        db.flush()
        admin = db.scalar(select(User).where(User.username == "n029-admin"))
        assert admin is not None
        _published_ground_plan(
            db,
            area=area2,
            locations=[location2],
            operator_id=admin.id,
            key="floor2-out-of-scope",
        )

        source_pallet = db.get(InventoryPallet, source_pallet_id)
        source_lot = db.get(InventoryLot, source_lot_id)
        assert source_pallet is not None and source_lot is not None
        source_pallet.location_id = location2.id
        source_pallet.location_occupancy_key = "PRIMARY"
        source_lot.warehouse_location_id = location2.id
        source_lot.last_movement_at = _now()
        db.commit()

    with TestClient(app) as client:
        _login(client)
        # Login has its own security audit.  Capture the zero-write baseline
        # after authentication so only the rejected merge request is measured.
        with factory() as db:
            before = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])
            audit_count_before = int(
                db.scalar(select(func.count(OperationLog.id))) or 0
            )
        response = client.post(
            MERGE_BATCH_URL,
            json=_merge_payload(ids, key=key),
        )
    assert response.status_code == 409, response.text

    with factory() as db:
        assert _write_snapshot(db, ids["all_pallets"], ids["all_lots"]) == before
        assert int(db.scalar(select(func.count(OperationLog.id))) or 0) == (
            audit_count_before
        )
        assert db.scalar(
            select(OperationLog.id).where(OperationLog.batch_id == key)
        ) is None


def test_compatible_frozen_pallets_can_merge_without_changing_frozen_state(
    pallet_merge_app,
) -> None:
    app, factory, ids = pallet_merge_app
    with factory() as db:
        rows = db.scalars(
            select(InventoryLot).where(InventoryLot.id.in_(ids["all_lots"]))
        ).all()
        for row in rows:
            row.status = "frozen"
        db.commit()
        before = sorted(_lot_immutable(row) for row in rows)
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            MERGE_BATCH_URL,
            json=_merge_payload(ids, key="p149c-compatible-frozen"),
        )
    assert response.status_code == 200, response.text
    with factory() as db:
        rows = db.scalars(
            select(InventoryLot)
            .where(InventoryLot.id.in_(ids["all_lots"]))
            .order_by(InventoryLot.id)
        ).all()
        assert sorted(_lot_immutable(row) for row in rows) == before
        assert {row.status for row in rows} == {"frozen"}


def test_one_source_plus_target_is_the_minimum_valid_two_pallet_merge(
    pallet_merge_app,
) -> None:
    app, factory, ids = pallet_merge_app
    payload = _merge_payload(
        ids,
        key="p149c-minimum-two-pallets",
        source_ids=[ids["source_pallets"][0]],
    )
    with TestClient(app) as client:
        _login(client)
        response = client.post(MERGE_BATCH_URL, json=payload)
    assert response.status_code == 200, response.text
    with factory() as db:
        source = db.get(InventoryPallet, ids["source_pallets"][0])
        target = db.get(InventoryPallet, ids["target_pallet"])
        assert source is not None and target is not None
        assert source.status == "closed" and source.is_current is False
        assert target.status == "active" and target.is_current is True
        assert {int(item.inventory_lot_id) for item in target.items} == {
            ids["source_lots"][0],
            ids["target_lot"],
        }


@pytest.mark.parametrize(
    ("payload_mutator", "expected_status"),
    (
        ("not_confirmed", 422),
        ("no_source", 422),
        ("too_many_sources", 422),
        ("duplicate_source", 409),
        ("target_is_source", 409),
    ),
)
def test_batch_shape_is_strict_and_never_writes(
    pallet_merge_app,
    payload_mutator: str,
    expected_status: int,
) -> None:
    app, factory, ids = pallet_merge_app
    key = f"p149c-shape-{payload_mutator}"
    payload = _merge_payload(ids, key=key)
    if payload_mutator == "not_confirmed":
        payload["confirmed"] = False
    elif payload_mutator == "no_source":
        payload["sources"] = []
    elif payload_mutator == "too_many_sources":
        payload["sources"] = [
            {
                "client_item_id": f"source-{index}",
                "pallet_id": ids["source_pallets"][0] + index,
                "expected_version": 1,
            }
            for index in range(20)
        ]
    elif payload_mutator == "duplicate_source":
        payload["sources"][1]["pallet_id"] = payload["sources"][0]["pallet_id"]
    elif payload_mutator == "target_is_source":
        payload["sources"][1]["pallet_id"] = ids["target_pallet"]
    with factory() as db:
        before = _write_snapshot(db, ids["all_pallets"], ids["all_lots"])
    with TestClient(app) as client:
        _login(client)
        response = client.post(MERGE_BATCH_URL, json=payload)
    assert response.status_code == expected_status, response.text
    with factory() as db:
        assert _write_snapshot(db, ids["all_pallets"], ids["all_lots"]) == before
        assert db.scalar(
            select(OperationLog.id).where(OperationLog.batch_id == key)
        ) is None


def test_frontend_uses_one_atomic_merge_batch_alongside_p1_47d() -> None:
    source = FRONTEND.read_text(encoding="utf-8")
    assert "P1_49C_ENABLED = true" in source
    assert '"/api/warehouse/pallets/merge-batches"' in source
    assert "const [mergeSources" in source
    assert "const [mergeTarget" in source
    assert "Promise.all" not in source[source.find("confirmPalletMergeBatch") : source.find("confirmPalletMergeBatch") + 2500]
    assert "P1_47D_ENABLED" not in source
    assert 'value.permissions.includes("warehouse.stocktake.submit")' in source
    assert source.count('"/api/warehouse/twin-operations/stocktake-batches"') == 1
