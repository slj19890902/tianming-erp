from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.models.audit import OperationLog
from app.models.production import (
    ProductionCompletion,
    ProductionCompletionBatch,
    ProductionStockTransfer,
    ProductionTask,
)
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLocationMovement,
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
    WarehouseGroundPlacementMutation,
    WarehouseLocation,
)
from app.services.production_workflow import (
    CompletionCommand,
    ProductionWorkflowError,
    StockTransferCommand,
    complete_production_batch,
    list_production_completions,
    transfer_direct_completion_to_stock,
)
from test_n029_production_service import production_app


PARENT_REVISION = "hi17v8x9z06"
TARGET_REVISION = "ii17v8x9z06"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def _direct_command(task, *, quantity: int) -> CompletionCommand:
    return CompletionCommand(
        task_id=int(task.id),
        expected_version=int(task.version),
        disposition="direct",
        material_input_quantity=quantity,
        actual_output_quantity=quantity,
        defective_quantity=0,
        direct_delivery_quantity=quantity,
    )


def _new_direct_case(db, *, key: str, quantity: int):
    from test_n029_production_service import _add_case

    from app.models.customer import Customer
    from app.models.product import Product

    customer = db.scalar(select(Customer).where(Customer.name == "N029客户A"))
    product = db.scalar(select(Product).where(Product.product_code == "MASTER-A"))
    assert customer is not None and product is not None
    return _add_case(
        db,
        key=key,
        customer=customer,
        product=product,
        quantity=quantity,
        material_status="received",
    )


def _seed_current_fin_target(
    db,
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int = 1,
) -> list[WarehouseLocation]:
    from app.models.user import User
    from app.services import location_candidates

    revision = "p1-102-direct-fin-map"
    feature_id = "zone-p1-102-fin-001"
    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        lambda floor_number: (
            {
                "revision": revision,
                "zones_by_id": {feature_id: "FIN-001"},
                "zone_ids_by_area": {"FIN-001": (feature_id,)},
            }
            if int(floor_number) == 1
            else None
        ),
    )
    operator = db.scalar(select(User).where(User.username == "n029-admin"))
    assert operator is not None
    floor = WarehouseFloor(
        floor_code="P1102-F1",
        floor_name="P1-102 匿名一楼",
        floor_number=1,
        construction_status="enabled",
    )
    db.add(floor)
    db.flush()
    area = WarehouseArea(
        floor_id=floor.id,
        area_code="FIN-001",
        area_name="匿名真实成品待送区",
        construction_status="enabled",
    )
    area.storage_policy = WarehouseAreaStoragePolicy(
        map_feature_id=feature_id,
        allowed_inventory_types_json='["finished"]',
        storage_layout="pallet_ground",
        status="published",
        published_map_revision=revision,
        version=1,
    )
    location = WarehouseLocation(
        location_code="F1-FIN-001-P001",
        location_name="匿名真实成品待送位 001",
        warehouse_type="finished",
        is_active=True,
        warehouse_floor=1,
        area_code="FIN-001",
        storage_type="ground",
        placement_status="placed",
        source_version="TWIN_V1",
    )
    location.floor3_layout = Floor3LocationLayout(
        left_pct=Decimal("10"),
        top_pct=Decimal("10"),
        width_pct=Decimal("6"),
        height_pct=Decimal("10"),
        z_index=1,
        version=1,
        source_type="manual",
        layout_kind="physical_pallet",
    )
    db.add_all([area, location])
    db.flush()
    locations = [location]
    for index in range(2, count + 1):
        extra = WarehouseLocation(
            location_code=f"F1-FIN-001-P{index:03d}",
            location_name=f"匿名真实成品待送位 {index:03d}",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=1,
            area_code="FIN-001",
            storage_type="ground",
            placement_status="placed",
            source_version="TWIN_V1",
        )
        extra.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal(str(10 + index * 8)),
            top_pct=Decimal("10"),
            width_pct=Decimal("6"),
            height_pct=Decimal("10"),
            z_index=index,
            version=1,
            source_type="manual",
            layout_kind="physical_pallet",
        )
        db.add(extra)
        locations.append(extra)
    db.flush()
    plan = WarehouseGroundLayoutPlan(
        area_id=area.id,
        status="published",
        target_slot_count=count,
        numbering_origin="south",
        row_direction="from_aisle_inward",
        slot_direction="left_to_right",
        row_start_no=1,
        slot_start_no=1,
        draft_map_revision=revision,
        published_map_revision=revision,
        preview_fingerprint="a" * 64,
        version=1,
        publish_idempotency_key="p1-102-direct-fin-plan",
        publish_request_hash="b" * 64,
        updated_by=operator.id,
        published_by=operator.id,
        published_at=datetime.now(),
    )
    db.add(plan)
    db.flush()
    db.add(
        WarehouseGroundLayoutSlot(
            plan_id=plan.id,
            location_id=location.id,
            route_sequence=1,
            row_no=1,
            slot_no=1,
            x_mm=Decimal("1000"),
            y_mm=Decimal("1000"),
            width_mm=1200,
            depth_mm=1000,
        )
    )
    for index, extra in enumerate(locations[1:], start=2):
        db.add(
            WarehouseGroundLayoutSlot(
                plan_id=plan.id,
                location_id=extra.id,
                route_sequence=index,
                row_no=1,
                slot_no=index,
                x_mm=Decimal(str(1000 + (index - 1) * 1400)),
                y_mm=Decimal("1000"),
                width_mm=1200,
                depth_mm=1000,
            )
        )
    db.flush()
    return locations


def _seed_current_twin_stock_target(
    db,
    monkeypatch: pytest.MonkeyPatch,
    *,
    storage_type: str,
) -> WarehouseLocation:
    from app.services import location_candidates

    assert storage_type in {"ground", "rack"}
    revision = "p1-102-direct-fin-map"
    fin_feature_id = "zone-p1-102-fin-001"
    area_code = f"STOCK-{storage_type.upper()}"
    stock_feature_id = f"zone-p1-102-{area_code.lower()}"
    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        lambda floor_number: (
            {
                "revision": revision,
                "zones_by_id": {
                    fin_feature_id: "FIN-001",
                    stock_feature_id: area_code,
                },
                "zone_ids_by_area": {
                    "FIN-001": (fin_feature_id,),
                    area_code: (stock_feature_id,),
                },
            }
            if int(floor_number) == 1
            else None
        ),
    )
    floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_number == 1))
    assert floor is not None
    area = WarehouseArea(
        floor_id=floor.id,
        area_code=area_code,
        area_name=f"Current TWIN {storage_type} stock area",
        construction_status="enabled",
    )
    area.storage_policy = WarehouseAreaStoragePolicy(
        map_feature_id=stock_feature_id,
        allowed_inventory_types_json='["finished"]',
        storage_layout=("pallet_ground" if storage_type == "ground" else "rack"),
        status="published",
        published_map_revision=revision,
        version=1,
    )
    location = WarehouseLocation(
        location_code=f"F1-{area_code}-001",
        location_name=f"Current TWIN {storage_type} stock position",
        warehouse_type="finished",
        is_active=True,
        warehouse_floor=1,
        area_code=area_code,
        storage_type=storage_type,
        placement_status="placed",
        source_version="TWIN_V1",
    )
    location.floor3_layout = Floor3LocationLayout(
        left_pct=Decimal("45"),
        top_pct=Decimal("45"),
        width_pct=Decimal("6"),
        height_pct=Decimal("10"),
        z_index=2,
        version=1,
        source_type="manual",
        layout_kind="physical_pallet",
    )
    db.add_all([area, location])
    db.flush()
    if storage_type == "ground":
        plan = WarehouseGroundLayoutPlan(
            area_id=area.id,
            status="published",
            target_slot_count=1,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision=revision,
            published_map_revision=revision,
            preview_fingerprint="c" * 64,
            version=1,
            publish_idempotency_key=f"p1-102-{storage_type}-plan",
            publish_request_hash="d" * 64,
            updated_by=1,
            published_by=1,
            published_at=datetime.now(),
        )
        db.add(plan)
        db.flush()
        db.add(
            WarehouseGroundLayoutSlot(
                plan_id=plan.id,
                location_id=location.id,
                route_sequence=1,
                row_no=1,
                slot_no=1,
                x_mm=Decimal("5000"),
                y_mm=Decimal("5000"),
                width_mm=1200,
                depth_mm=1000,
            )
        )
    db.flush()
    return location


def test_current_space_ledger_direct_completion_uses_real_fin_target(
    production_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.user import User

    _app, factory, ids = production_app
    with factory() as db:
        fin_location = _seed_current_fin_target(db, monkeypatch)[0]
        _order, _item, task = _new_direct_case(
            db,
            key="p1-102-direct-real-fin",
            quantity=12,
        )
        operator = db.scalar(select(User).where(User.username == "n029-admin"))
        assert operator is not None
        command_row = _direct_command(task, quantity=12)
        result = complete_production_batch(
            db,
            idempotency_key="p1-102-direct-real-fin",
            commands=[command_row],
            operator_id=operator.id,
        )
        completion = result.completions[0]
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None and lot.pallet_item is not None
        pallet = lot.pallet_item.pallet
        assert completion.warehouse_location_id == fin_location.id
        assert lot.warehouse_location_id == fin_location.id
        assert pallet.location_id == fin_location.id
        assert pallet.needs_relocation is False
        assert db.scalar(
            select(func.count()).select_from(WarehouseGroundOccupancy).where(
                WarehouseGroundOccupancy.pallet_id == pallet.id,
                WarehouseGroundOccupancy.primary_location_id == fin_location.id,
                WarehouseGroundOccupancy.status == "active",
            )
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(InventoryLot).where(
                InventoryLot.warehouse_location_id == ids["staging"],
                InventoryLot.source_ref_type == "production_completion",
                InventoryLot.source_ref_id == completion.id,
            )
        ) == 0

        replay = complete_production_batch(
            db,
            idempotency_key="p1-102-direct-real-fin",
            commands=[command_row],
            operator_id=operator.id,
        )
        assert replay.replayed is True
        assert replay.completions[0].id == completion.id


def test_direct_batch_claims_one_distinct_fin_slot_per_completion(
    production_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.user import User

    _app, factory, _ids = production_app
    with factory() as db:
        fin_locations = _seed_current_fin_target(db, monkeypatch, count=2)
        _order_a, _item_a, task_a = _new_direct_case(
            db,
            key="p1-102-direct-batch-a",
            quantity=7,
        )
        _order_b, _item_b, task_b = _new_direct_case(
            db,
            key="p1-102-direct-batch-b",
            quantity=9,
        )
        operator = db.scalar(select(User).where(User.username == "n029-admin"))
        assert operator is not None
        result = complete_production_batch(
            db,
            idempotency_key="p1-102-direct-real-fin-batch",
            commands=[
                _direct_command(task_a, quantity=7),
                _direct_command(task_b, quantity=9),
            ],
            operator_id=operator.id,
        )
        assert len(result.completions) == 2
        assert {
            int(completion.warehouse_location_id)
            for completion in result.completions
        } == {int(location.id) for location in fin_locations}
        pallet_ids: set[int] = set()
        for completion in result.completions:
            lot = db.get(InventoryLot, completion.inventory_lot_id)
            assert lot is not None and lot.pallet_item is not None
            pallet = lot.pallet_item.pallet
            pallet_ids.add(int(pallet.id))
            assert pallet.location_id == completion.warehouse_location_id
            assert pallet.needs_relocation is False
        assert len(pallet_ids) == 2
        assert db.scalar(
            select(func.count()).select_from(WarehouseGroundOccupancy).where(
                WarehouseGroundOccupancy.pallet_id.in_(pallet_ids),
                WarehouseGroundOccupancy.status == "active",
            )
        ) == 2


@pytest.mark.parametrize("storage_type", ["ground", "rack"])
def test_direct_fin_transfer_to_current_twin_position_closes_spatial_loop_once(
    production_app,
    monkeypatch: pytest.MonkeyPatch,
    storage_type: str,
) -> None:
    from app.models.user import User

    _app, factory, _ids = production_app
    with factory() as db:
        source_location = _seed_current_fin_target(db, monkeypatch)[0]
        target_location = _seed_current_twin_stock_target(
            db,
            monkeypatch,
            storage_type=storage_type,
        )
        _order, _item, task = _new_direct_case(
            db,
            key=f"p1-102-transfer-{storage_type}",
            quantity=13,
        )
        operator = db.scalar(select(User).where(User.username == "n029-admin"))
        assert operator is not None
        completion = complete_production_batch(
            db,
            idempotency_key=f"p1-102-transfer-{storage_type}-complete",
            commands=[_direct_command(task, quantity=13)],
            operator_id=operator.id,
        ).completions[0]
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None and lot.pallet_item is not None
        source_pallet = lot.pallet_item.pallet
        source_occupancy = db.scalar(
            select(WarehouseGroundOccupancy).where(
                WarehouseGroundOccupancy.pallet_id == source_pallet.id,
                WarehouseGroundOccupancy.primary_location_id == source_location.id,
                WarehouseGroundOccupancy.status == "active",
            )
        )
        assert source_occupancy is not None
        assert source_pallet.location_occupancy_key == (
            f"PRODUCTION_COMPLETION:{completion.id}"
        )
        assert db.scalar(
            select(func.count())
            .select_from(WarehouseGroundPlacementMutation)
            .where(
                WarehouseGroundPlacementMutation.occupancy_id
                == source_occupancy.id,
                WarehouseGroundPlacementMutation.result_lot_id == lot.id,
            )
        ) == 1
        completion_id = int(completion.id)
        lot_id = int(lot.id)
        source_location_id = int(source_location.id)
        source_occupancy_id = int(source_occupancy.id)
        target_location_id = int(target_location.id)
        operator_id = int(operator.id)
        db.commit()

    command = StockTransferCommand(
        idempotency_key=f"p1-102-transfer-{storage_type}-stock",
        location_id=target_location_id,
        expected_layout_version=1,
    )
    with factory() as db:
        transferred = transfer_direct_completion_to_stock(
            db,
            completion_id=completion_id,
            command=command,
            operator_id=operator_id,
        )
        assert transferred.replayed is False
        transfer_id = int(transferred.transfer.id)
        db.commit()

    def fact_counts(db) -> tuple[int, ...]:
        return (
            int(db.scalar(select(func.count()).select_from(InventoryLot)) or 0),
            int(db.scalar(select(func.count()).select_from(InventoryPallet)) or 0),
            int(db.scalar(select(func.count()).select_from(InventoryPalletItem)) or 0),
            int(
                db.scalar(select(func.count()).select_from(WarehouseGroundOccupancy))
                or 0
            ),
            int(
                db.scalar(
                    select(func.count()).select_from(WarehouseGroundOccupancySlot)
                )
                or 0
            ),
            int(
                db.scalar(select(func.count()).select_from(InventoryLocationMovement))
                or 0
            ),
            int(db.scalar(select(func.count()).select_from(InventoryMovement)) or 0),
            int(
                db.scalar(select(func.count()).select_from(ProductionStockTransfer))
                or 0
            ),
            int(
                db.scalar(
                    select(func.count()).select_from(
                        WarehouseGroundPlacementMutation
                    )
                )
                or 0
            ),
        )

    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot is not None and lot.pallet_item is not None
        target_pallet = lot.pallet_item.pallet
        assert lot.warehouse_location_id == target_location_id
        assert target_pallet.is_current is True
        assert target_pallet.status == "active"
        assert target_pallet.location_id == target_location_id
        assert db.scalar(
            select(func.count()).select_from(InventoryPallet).where(
                InventoryPallet.location_id == source_location_id,
                InventoryPallet.is_current.is_(True),
            )
        ) == 0
        source_occupancy = db.get(WarehouseGroundOccupancy, source_occupancy_id)
        assert source_occupancy is not None
        assert source_occupancy.status == "released"
        assert db.scalar(
            select(func.count())
            .select_from(WarehouseGroundOccupancySlot)
            .where(
                WarehouseGroundOccupancySlot.occupancy_id == source_occupancy_id,
                WarehouseGroundOccupancySlot.status == "active",
            )
        ) == 0
        target_occupancy = db.scalar(
            select(WarehouseGroundOccupancy).where(
                WarehouseGroundOccupancy.pallet_id == target_pallet.id,
                WarehouseGroundOccupancy.primary_location_id == target_location_id,
                WarehouseGroundOccupancy.status == "active",
            )
        )
        if storage_type == "ground":
            assert target_occupancy is not None
            assert db.scalar(
                select(func.count())
                .select_from(WarehouseGroundOccupancySlot)
                .where(
                    WarehouseGroundOccupancySlot.occupancy_id
                    == target_occupancy.id,
                    WarehouseGroundOccupancySlot.location_id == target_location_id,
                    WarehouseGroundOccupancySlot.status == "active",
                )
            ) == 1
        else:
            assert target_occupancy is None
        before_replay = fact_counts(db)

    with factory() as db:
        replayed = transfer_direct_completion_to_stock(
            db,
            completion_id=completion_id,
            command=command,
            operator_id=operator_id,
        )
        assert replayed.replayed is True
        assert int(replayed.transfer.id) == transfer_id
        db.commit()
        assert fact_counts(db) == before_replay


def test_direct_completion_56_creates_one_formal_system_pallet_and_replays(
    production_app,
) -> None:
    from app.api.deliveries import (
        _inventory_sources_for_order_item,
        _pick_source_location,
    )
    from app.api.warehouse import _lot_dict

    _app, factory, _ids = production_app
    with factory() as db:
        _order, item, task = _new_direct_case(
            db, key="p1-49a-direct-56", quantity=56
        )
        command_row = _direct_command(task, quantity=56)
        result = complete_production_batch(
            db,
            idempotency_key="p1-49a-direct-56",
            commands=[command_row],
            operator_id=1,
        )
        completion = result.completions[0]
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None and lot.pallet_item is not None
        pallet = lot.pallet_item.pallet
        pallet_id = int(pallet.id)
        lot_id = int(lot.id)
        assert pallet.location_id == completion.warehouse_location_id
        assert pallet.location.location_code == "F1-DISPATCH-01"
        assert pallet.location_occupancy_key == f"PRODUCTION_COMPLETION:{completion.id}"
        assert pallet.pallet_code == f"PLT-F1-PC-{completion.id:010d}"
        assert pallet.is_current is True
        assert pallet.status == "active"
        assert pallet.needs_relocation is True
        assert len(pallet.items) == 1
        assert pallet.items[0].inventory_lot_id == lot.id
        assert int(pallet.items[0].quantity) == 56
        assert int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0) == 56
        assert item.quantity == 56
        history = next(
            row
            for row in list_production_completions(
                db,
                allowed_customer_ids=None,
                completion_ids=[completion.id],
            )
            if row["id"] == completion.id
        )
        assert history["system_pallet_id"] == pallet.id
        assert history["system_pallet_code"] == pallet.pallet_code
        inventory_payload = _lot_dict(lot)
        assert inventory_payload["floor3_binding"] == {
            "pallet_id": pallet.id,
            "pallet_code": pallet.pallet_code,
            "pallet_version": pallet.version,
            "location_id": pallet.location_id,
        }
        sources = _inventory_sources_for_order_item(
            db,
            order_item=item,
            planned_delivery_quantity=56,
        )
        source = next(row for row in sources if row.get("lot_id") == lot.id)
        assert source["quantity_to_pick_stock"] == 56
        pick_location = _pick_source_location(db, source=source)
        assert pick_location["location_id"] == pallet.location_id
        assert pick_location["location_code"] == "F1-DISPATCH-01"
        assert pick_location["pallet_id"] == pallet.id
        assert pick_location["pallet_code"] == pallet.pallet_code
        db.commit()

        replay = complete_production_batch(
            db,
            idempotency_key="p1-49a-direct-56",
            commands=[command_row],
            operator_id=1,
        )
        assert replay.replayed is True
        assert replay.completions[0].id == completion.id
        assert db.scalar(select(func.count()).select_from(InventoryPallet)) == 2
        assert db.scalar(select(func.count()).select_from(InventoryLot)) >= 1
        assert db.scalar(
            select(func.count()).select_from(InventoryPalletItem).where(
                InventoryPalletItem.pallet_id == pallet_id
            )
        ) == 1
        assert db.get(InventoryLot, lot_id).pallet_item.pallet_id == pallet_id


def test_reused_released_direct_pallet_resets_previous_completion_key(
    production_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A released pallet begins its next inbound cycle as PRIMARY again."""

    _app, factory, _ids = production_app
    with factory() as db:
        _seed_current_fin_target(db, monkeypatch, count=2)
        _first_order, _first_item, first_task = _new_direct_case(
            db, key="p1-49a-reuse-old-cycle", quantity=7
        )
        first_completion = complete_production_batch(
            db,
            idempotency_key="p1-49a-reuse-old-cycle",
            commands=[_direct_command(first_task, quantity=7)],
            operator_id=1,
        ).completions[0]
        first_lot = db.get(InventoryLot, first_completion.inventory_lot_id)
        assert first_lot is not None and first_lot.pallet_item is not None
        released_pallet = first_lot.pallet_item.pallet
        assert released_pallet is not None
        assert released_pallet.location_occupancy_key == (
            f"PRODUCTION_COMPLETION:{first_completion.id}"
        )

        # Model the post-delivery released state: the old lot is zero balance
        # and its physical pallet is closed with no current location.  Its
        # previous completion key must not leak into the next receipt cycle.
        first_lot.quantity_available = 0
        first_lot.quantity_reserved = 0
        first_lot.quantity_damaged = 0
        released_pallet.location_id = None
        released_pallet.status = "closed"
        released_pallet.is_current = False
        released_pallet.closed_at = datetime.now()
        prior_occupancy = db.scalar(
            select(WarehouseGroundOccupancy).where(
                WarehouseGroundOccupancy.pallet_id == released_pallet.id,
                WarehouseGroundOccupancy.status == "active",
            )
        )
        assert prior_occupancy is not None
        for slot in list(prior_occupancy.slots):
            slot.status = "released"
            slot.released_at = datetime.now()
        prior_occupancy.status = "released"
        prior_occupancy.released_at = datetime.now()
        prior_occupancy.released_by = 1
        db.flush()

        _second_order, _second_item, second_task = _new_direct_case(
            db, key="p1-49a-reuse-new-cycle", quantity=9
        )
        second_completion = complete_production_batch(
            db,
            idempotency_key="p1-49a-reuse-new-cycle",
            commands=[_direct_command(second_task, quantity=9)],
            operator_id=1,
        ).completions[0]
        second_lot = db.get(InventoryLot, second_completion.inventory_lot_id)
        assert second_lot is not None and second_lot.pallet_item is not None
        assert second_lot.pallet_item.pallet_id == released_pallet.id
        reused = second_lot.pallet_item.pallet
        assert reused.location_occupancy_key == (
            f"PRODUCTION_COMPLETION:{second_completion.id}"
        )
        assert {row.inventory_lot_id for row in reused.items} == {second_lot.id}


def test_two_direct_completion_details_share_location_but_never_share_pallet(
    production_app,
) -> None:
    _app, factory, ids = production_app
    with factory() as db:
        _order_a, _item_a, task_a = _new_direct_case(
            db, key="p1-49a-separate-a", quantity=7
        )
        _order_b, _item_b, task_b = _new_direct_case(
            db, key="p1-49a-separate-b", quantity=9
        )
        first = complete_production_batch(
            db,
            idempotency_key="p1-49a-separate-a",
            commands=[_direct_command(task_a, quantity=7)],
            operator_id=1,
        ).completions[0]
        second = complete_production_batch(
            db,
            idempotency_key="p1-49a-separate-b",
            commands=[_direct_command(task_b, quantity=9)],
            operator_id=1,
        ).completions[0]
        first_pallet = db.get(InventoryLot, first.inventory_lot_id).pallet_item.pallet
        second_pallet = db.get(InventoryLot, second.inventory_lot_id).pallet_item.pallet
        assert first_pallet.id != second_pallet.id
        assert first_pallet.location_id == second_pallet.location_id == ids["staging"]
        assert first_pallet.location_occupancy_key != second_pallet.location_occupancy_key
        assert int(first_pallet.items[0].quantity) == 7
        assert int(second_pallet.items[0].quantity) == 9
        assert db.scalar(
            select(func.count()).select_from(InventoryPallet).where(
                InventoryPallet.location_id == ids["staging"],
                InventoryPallet.is_current.is_(True),
            )
        ) == 2


def test_direct_completion_failure_rolls_back_production_inventory_and_pallet(
    production_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import production_workflow

    _app, factory, _ids = production_app
    with factory() as db:
        _order, _item, task = _new_direct_case(
            db, key="p1-49a-rollback", quantity=11
        )
        task_id = int(task.id)
        db.commit()
        before = {
            "batches": db.scalar(select(func.count()).select_from(ProductionCompletionBatch)),
            "completions": db.scalar(select(func.count()).select_from(ProductionCompletion)),
            "lots": db.scalar(select(func.count()).select_from(InventoryLot)),
            "pallets": db.scalar(select(func.count()).select_from(InventoryPallet)),
            "items": db.scalar(select(func.count()).select_from(InventoryPalletItem)),
            "movements": db.scalar(select(func.count()).select_from(InventoryMovement)),
            "location_movements": db.scalar(
                select(func.count()).select_from(InventoryLocationMovement)
            ),
            "audit": db.scalar(select(func.count()).select_from(OperationLog)),
        }

        def fail_bind(*_args, **_kwargs):
            raise RuntimeError("forced P1-49A pallet failure")

        monkeypatch.setattr(
            production_workflow,
            "_bind_direct_completion_lots_to_system_pallet",
            fail_bind,
        )
        with pytest.raises(RuntimeError, match="forced P1-49A pallet failure"):
            complete_production_batch(
                db,
                idempotency_key="p1-49a-rollback",
                commands=[_direct_command(task, quantity=11)],
                operator_id=1,
            )
        db.rollback()
        after = {
            "batches": db.scalar(select(func.count()).select_from(ProductionCompletionBatch)),
            "completions": db.scalar(select(func.count()).select_from(ProductionCompletion)),
            "lots": db.scalar(select(func.count()).select_from(InventoryLot)),
            "pallets": db.scalar(select(func.count()).select_from(InventoryPallet)),
            "items": db.scalar(select(func.count()).select_from(InventoryPalletItem)),
            "movements": db.scalar(select(func.count()).select_from(InventoryMovement)),
            "location_movements": db.scalar(
                select(func.count()).select_from(InventoryLocationMovement)
            ),
            "audit": db.scalar(select(func.count()).select_from(OperationLog)),
        }
        assert after == before
        persisted_task = db.get(ProductionTask, task_id)
        assert persisted_task is not None
        assert persisted_task.status == "pending"
        assert persisted_task.version == 1


def test_cross_business_pallet_conflict_is_actionable_and_rolls_back(
    production_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import production_workflow

    _app, factory, _ids = production_app
    with factory() as db:
        _seed_current_fin_target(db, monkeypatch, count=1)
        _order, _item, task = _new_direct_case(
            db, key="p0-24-cross-business-pallet", quantity=11
        )
        task_id = int(task.id)
        db.commit()
        before = {
            "batches": db.scalar(
                select(func.count()).select_from(ProductionCompletionBatch)
            ),
            "completions": db.scalar(
                select(func.count()).select_from(ProductionCompletion)
            ),
            "lots": db.scalar(select(func.count()).select_from(InventoryLot)),
            "pallets": db.scalar(select(func.count()).select_from(InventoryPallet)),
            "items": db.scalar(select(func.count()).select_from(InventoryPalletItem)),
        }
        original_finished_in = production_workflow.manual_finished_in

        def bind_to_unrelated_cycle(*args, **kwargs):
            lot = original_finished_in(*args, **kwargs)
            assert lot.pallet_item is not None
            pallet = lot.pallet_item.pallet
            assert pallet is not None
            pallet.location_occupancy_key = "PRODUCTION_COMPLETION:999999"
            db.flush()
            return lot

        monkeypatch.setattr(
            production_workflow,
            "manual_finished_in",
            bind_to_unrelated_cycle,
        )
        with pytest.raises(ProductionWorkflowError) as caught:
            complete_production_batch(
                db,
                idempotency_key="p0-24-cross-business-pallet",
                commands=[_direct_command(task, quantity=11)],
                operator_id=1,
            )
        message = str(caught.value)
        assert "系统发现本次成品的栈板记录不一致" in message
        assert "已安全取消本条入库" in message
        assert "请刷新后重试" in message
        assert "业务栈板" not in message
        assert "库存投影" not in message
        db.rollback()
        after = {
            "batches": db.scalar(
                select(func.count()).select_from(ProductionCompletionBatch)
            ),
            "completions": db.scalar(
                select(func.count()).select_from(ProductionCompletion)
            ),
            "lots": db.scalar(select(func.count()).select_from(InventoryLot)),
            "pallets": db.scalar(select(func.count()).select_from(InventoryPallet)),
            "items": db.scalar(select(func.count()).select_from(InventoryPalletItem)),
        }
        assert after == before
        persisted_task = db.get(ProductionTask, task_id)
        assert persisted_task is not None
        assert persisted_task.status == "pending"
        assert persisted_task.version == 1


def test_direct_completion_audit_links_completion_pallet_lot_quantity_and_actor(
    production_app,
) -> None:
    from fastapi.testclient import TestClient

    app, factory, _ids = production_app
    with factory() as db:
        _order, _item, task = _new_direct_case(
            db, key="p1-49a-audit-direct", quantity=4
        )
        task_id = int(task.id)
        db.commit()
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "n029-admin", "password": "123456"},
        )
        assert login.status_code == 200, login.text
        response = client.post(
            "/api/production/completion-batches",
            json={
                "idempotency_key": "p1-49a-audit-direct",
                "items": [
                    {
                        "task_id": task_id,
                        "expected_version": 1,
                        "disposition": "direct",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        completion_id = int(response.json()["items"][0]["id"])

    with factory() as db:
        completion = db.get(ProductionCompletion, completion_id)
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        pallet = lot.pallet_item.pallet
        event = db.scalar(
            select(OperationLog).where(
                OperationLog.action_code == "production.completion.posted",
                OperationLog.entity_id == completion_id,
            )
        )
        details = json.loads(event.details)
        assert details["system_pallet_id"] == pallet.id
        assert details["system_pallet_code"] == pallet.pallet_code
        assert details["system_pallet_inventory_lot_ids"] == [lot.id]
        assert details["system_pallet_quantity"] == 4
        assert event.actor_user_id_snapshot is not None
        assert event.entity_id == completion_id


def test_invalid_formal_dispatch_location_fails_before_any_completion_fact(
    production_app,
) -> None:
    _app, factory, _ids = production_app
    with factory() as db:
        _order, _item, task = _new_direct_case(
            db, key="p1-49a-invalid-location", quantity=8
        )
        staging = db.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "F1-DISPATCH-01"
            )
        )
        assert staging is not None
        staging.area_code = "WRONG"
        db.commit()
        before = {
            "batches": db.scalar(select(func.count()).select_from(ProductionCompletionBatch)),
            "completions": db.scalar(select(func.count()).select_from(ProductionCompletion)),
            "lots": db.scalar(select(func.count()).select_from(InventoryLot)),
            "pallets": db.scalar(select(func.count()).select_from(InventoryPallet)),
        }
        with pytest.raises(ProductionWorkflowError, match="F1-DISPATCH-01"):
            complete_production_batch(
                db,
                idempotency_key="p1-49a-invalid-location",
                commands=[_direct_command(task, quantity=8)],
                operator_id=1,
            )
        db.rollback()
        after = {
            "batches": db.scalar(select(func.count()).select_from(ProductionCompletionBatch)),
            "completions": db.scalar(select(func.count()).select_from(ProductionCompletion)),
            "lots": db.scalar(select(func.count()).select_from(InventoryLot)),
            "pallets": db.scalar(select(func.count()).select_from(InventoryPallet)),
        }
        assert after == before


def test_p1_49a_migration_round_trip_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import create_engine, text

    database = tmp_path / "p1-49a-migration.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(inventory_pallets)"
            )
        }
        assert "location_occupancy_key" in columns
        connection.execute(
            text(
                "INSERT INTO inventory_pallets "
                "(pallet_code,location_id,status,is_current,needs_relocation,version,"
                "remarks,location_occupancy_key) "
                "VALUES ('P1-49A-MIGRATION',NULL,'closed',0,0,1,NULL,'PRIMARY')"
            )
        )
    engine.dispose()
    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)

    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.execute(
            text(
                "SELECT location_occupancy_key FROM inventory_pallets "
                "WHERE pallet_code='P1-49A-MIGRATION'"
            )
        ).scalar_one() == "PRIMARY"
        unique_indexes = {
            row[1]: row[2]
            for row in connection.exec_driver_sql(
                "PRAGMA index_list(inventory_pallets)"
            )
        }
        assert unique_indexes["uq_inventory_pallets_current_location"] == 1
        index_columns = [
            row[2]
            for row in connection.exec_driver_sql(
                "PRAGMA index_info(uq_inventory_pallets_current_location)"
            )
        ]
        assert index_columns == ["location_id", "location_occupancy_key"]
        connection.execute(
            text(
                "UPDATE inventory_pallets SET location_occupancy_key="
                "'PRODUCTION_COMPLETION:99' WHERE pallet_code='P1-49A-MIGRATION'"
            )
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="P1-49A"):
        command.downgrade(config, PARENT_REVISION)
