from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from fastapi.testclient import TestClient

from app.services.production_workflow import receipt_auto_finished_location_projection
from tests.test_p1_81_receipt_purpose_flow import (
    _create_frozen_sources,
    _freeze_receipt_fact,
    _receive,
    _seed_material_and_staging,
)
from tests.test_phase11_requisition import _login, requisition_app


def _seed_published_fin_ground_plan(session_factory) -> int:
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseGroundLayoutPlan,
        WarehouseGroundLayoutSlot,
        WarehouseLocation,
    )

    with session_factory() as session:
        existing = session.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "F1-FIN-001-L001"
            )
        )
        if existing is not None:
            return existing.id
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 1)
        )
        assert floor is not None
        area = WarehouseArea(
            floor_id=floor.id,
            area_code="FIN-001",
            area_name="匿名成品待送区",
            construction_status="enabled",
            capacity_review_status="confirmed",
            capacity_eligible=True,
            confirmed_pallet_capacity=2,
            capacity_reviewed_by="匿名复核员",
            capacity_reviewed_at=datetime.now(),
        )
        area.storage_policy = WarehouseAreaStoragePolicy(
            map_feature_id="zone-p019-fin-001",
            allowed_inventory_types_json='["finished"]',
            storage_layout="pallet_ground",
            status="published",
            published_map_revision="p019-fin-map-v1",
            version=1,
        )
        session.add(area)
        session.flush()
        location = WarehouseLocation(
            location_code="F1-FIN-001-L001",
            location_name="成品待送堆放区 001 号位",
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
            width_pct=Decimal("10"),
            height_pct=Decimal("10"),
            version=2,
            source_type="manual",
            layout_kind="physical_pallet",
        )
        session.add(location)
        session.flush()
        plan = WarehouseGroundLayoutPlan(
            area_id=area.id,
            status="published",
            target_slot_count=1,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision="p019-fin-map-v1",
            published_map_revision="p019-fin-map-v1",
            preview_fingerprint="a" * 64,
            version=1,
            publish_idempotency_key="p019-fin-plan-publish",
            publish_request_hash="b" * 64,
            updated_by=1,
            published_by=1,
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
                x_mm=Decimal("1000"),
                y_mm=Decimal("1000"),
                width_mm=1200,
                depth_mm=1000,
            )
        )
        session.commit()
        return location.id


def _mock_fin_runtime_identity(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.warehouse_inventory import (
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseLocation,
    )
    from app.services import location_candidates

    with session_factory() as session:
        location, area, policy = session.execute(
            select(
                WarehouseLocation,
                WarehouseArea,
                WarehouseAreaStoragePolicy,
            )
            .join(
                WarehouseFloor,
                WarehouseFloor.floor_number == WarehouseLocation.warehouse_floor,
            )
            .join(
                WarehouseArea,
                WarehouseArea.floor_id == WarehouseFloor.id,
            )
            .join(
                WarehouseAreaStoragePolicy,
                WarehouseAreaStoragePolicy.area_id == WarehouseArea.id,
            )
            .where(
                WarehouseLocation.location_code == "F1-FIN-001-L001",
                WarehouseArea.area_code == WarehouseLocation.area_code,
            )
        ).one()
        revision = str(policy.published_map_revision)
        feature_id = str(policy.map_feature_id)
        area_code = str(area.area_code).strip().upper()

    original = location_candidates.load_warehouse_twin_published_floor_identity

    def load_identity(floor_number: int):
        if int(floor_number) == 1:
            return {
                "revision": revision,
                "zones_by_id": {feature_id: area_code},
                "zone_ids_by_area": {area_code: (feature_id,)},
            }
        return original(floor_number)

    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        load_identity,
    )


def _seed_floor3_v11_fallback(
    session_factory,
    *,
    area_code: str = "E2",
    location_code: str | None = None,
    storage_type: str = "ground",
    placement_status: str = "placed",
    is_temporary: bool = False,
    sort_order: int = 1,
) -> int:
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    with session_factory() as session:
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 3)
        )
        if floor is None:
            floor = WarehouseFloor(
                floor_code="P022-3F",
                floor_name="P0-22 匿名三楼",
                floor_number=3,
                construction_status="enabled",
            )
            session.add(floor)
            session.flush()
        area = session.scalar(
            select(WarehouseArea).where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code == area_code,
            )
        )
        if area is None:
            area = WarehouseArea(
                floor_id=floor.id,
                area_code=area_code,
                area_name=f"匿名三楼 {area_code} 成品区",
                construction_status="enabled",
                capacity_review_status="confirmed",
                capacity_eligible=True,
                confirmed_pallet_capacity=2,
                capacity_reviewed_by="P0-22 匿名复核",
                capacity_reviewed_at=datetime.now(),
            )
            session.add(area)
            session.flush()
        location = WarehouseLocation(
            location_code=location_code or f"{area_code}-P022-{sort_order:02d}",
            location_name=f"匿名三楼 {area_code} 第1位",
            warehouse_type="finished",
            is_active=True,
            is_temporary=is_temporary,
            warehouse_floor=3,
            area_code=area_code,
            storage_type=storage_type,
            placement_status=placement_status,
            source_version="V11",
            sort_order=sort_order,
        )
        location.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal("10"),
            top_pct=Decimal("10"),
            width_pct=Decimal("5"),
            height_pct=Decimal("5"),
            version=3,
            source_type="manual",
            layout_kind="physical_pallet",
        )
        session.add(location)
        session.commit()
        return int(location.id)


def _occupy_all_fin_locations(session_factory) -> None:
    from app.models.warehouse_inventory import InventoryPallet, WarehouseLocation

    with session_factory() as session:
        locations = session.scalars(
            select(WarehouseLocation).where(
                WarehouseLocation.warehouse_floor == 1,
                WarehouseLocation.area_code.in_(("FIN-001", "FIN-002", "FIN-003")),
                WarehouseLocation.is_active.is_(True),
            )
        ).all()
        assert locations
        for location in locations:
            session.add(
                InventoryPallet(
                    pallet_code=f"P022-FIN-OCCUPIED-{location.id}",
                    location_id=location.id,
                    location_occupancy_key="PRIMARY",
                    status="active",
                    is_current=True,
                    needs_relocation=False,
                    version=1,
                    created_by=1,
                    updated_by=1,
                )
            )
        session.commit()


def _mock_floor3_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    area_codes: tuple[str, ...] = (),
    area_subtypes: dict[str, str] | None = None,
) -> None:
    from app.services import production_workflow

    subtypes = area_subtypes or {
        area_code: "finished_storage" for area_code in area_codes
    }
    monkeypatch.setattr(
        production_workflow,
        "load_warehouse_twin_floor",
        lambda floor_code: {
            "floor_code": floor_code,
            "revision": "p022-current-3f-map",
            "features": [
                {
                    "id": f"zone-p022-{area_code.lower()}",
                    "feature_kind": "zone",
                    "erp_area_code": area_code,
                    "subtype": subtype,
                    "storage_mode": "floor",
                    "points": [[0, 0], [100, 0], [100, 100], [0, 100]],
                }
                for area_code, subtype in subtypes.items()
            ],
        },
    )


def _v11_receipt_fact_counts(session_factory) -> dict[str, int]:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.production import ProductionCompletion, ProductionStockTransfer
    from app.models.warehouse_inventory import (
        InventoryLocationMovement,
        InventoryLot,
        InventoryMovement,
        InventoryPallet,
        InventoryPalletItem,
        WarehouseGroundOccupancy,
    )

    models = {
        "receipt_item": IncomingReceiptItem,
        "completion": ProductionCompletion,
        "stock_transfer": ProductionStockTransfer,
        "lot": InventoryLot,
        "inventory_movement": InventoryMovement,
        "pallet": InventoryPallet,
        "pallet_item": InventoryPalletItem,
        "location_movement": InventoryLocationMovement,
        "ground_occupancy": WarehouseGroundOccupancy,
    }
    with session_factory() as session:
        return {
            name: int(session.scalar(select(func.count(model.id))) or 0)
            for name, model in models.items()
        }


def _receive_full_order_to_fallback(
    client: TestClient,
    session_factory,
    *,
    key_prefix: str,
):
    source = _create_frozen_sources(
        client,
        session_factory,
        order_quantity=10,
        purchase_total=10,
        order_purpose=10,
        stock_purpose=0,
    )[0]
    frozen = _freeze_receipt_fact(
        client,
        source,
        idempotency_key=f"{key_prefix}-price",
    )
    assert frozen.status_code == 200, frozen.text
    received = _receive(
        client,
        source,
        frozen.json(),
        quantity=10,
        idempotency_key=f"{key_prefix}-receive",
    )
    assert received.status_code == 200, received.text
    return source, frozen.json(), received


def test_receipt_preview_uses_real_published_fin_member_not_legacy_dispatch(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    expected_location_id = _seed_published_fin_ground_plan(session_factory)
    _mock_fin_runtime_identity(session_factory, monkeypatch)

    with session_factory() as session:
        projection = receipt_auto_finished_location_projection(session)
        location = session.get(
            __import__(
                "app.models.warehouse_inventory",
                fromlist=["WarehouseLocation"],
            ).WarehouseLocation,
            expected_location_id,
        )
        assert location is not None
        assert projection == {
            "ready": True,
            "location_id": location.id,
            "location_code": location.location_code,
            "location_name": location.location_name,
            "current_address_name": location.location_name,
            "employee_location_name": location.location_name,
            "position_status": "mapped",
            "layout_version": 2,
            "capacity_warning": None,
            "issue": None,
        }


def test_receipt_preview_reports_missing_fin_ground_plan_instead_of_dispatch(
    requisition_app,
) -> None:
    _app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)

    from app.models.warehouse_inventory import WarehouseGroundLayoutPlan

    with session_factory() as session:
        plan = session.scalar(select(WarehouseGroundLayoutPlan))
        assert plan is not None
        session.delete(plan)
        session.commit()

    with session_factory() as session:
        projection = receipt_auto_finished_location_projection(session)

    assert projection["ready"] is False
    assert "FIN-001～003" in str(projection["issue"])
    assert "地堆排位" in str(projection["issue"])
    assert "DISPATCH" not in str(projection["issue"])


def test_receipt_falls_back_to_current_empty_floor3_position_when_fin_is_full(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryPallet,
        WarehouseGroundOccupancy,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    _seed_published_fin_ground_plan(session_factory)
    floor3_location_id = _seed_floor3_v11_fallback(session_factory, area_code="E2")
    _occupy_all_fin_locations(session_factory)
    _mock_floor3_runtime(monkeypatch, area_codes=("E2",))

    with session_factory() as session:
        from app.services.production_workflow import _receipt_auto_location_name

        projection = receipt_auto_finished_location_projection(session)
        assert projection["ready"] is True
        assert projection["layout_version"] == 3
        fallback_location = session.get(
            __import__(
                "app.models.warehouse_inventory",
                fromlist=["WarehouseLocation"],
            ).WarehouseLocation,
            floor3_location_id,
        )
        assert fallback_location is not None
        assert projection["location_name"] == _receipt_auto_location_name(fallback_location)

    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p022-floor3-price",
        )
        assert frozen.status_code == 200, frozen.text
        received = _receive(
            client,
            source,
            frozen.json(),
            quantity=10,
            idempotency_key="p022-floor3-receive",
        )
        assert received.status_code == 200, received.text

    with session_factory() as session:
        completion = session.scalar(
            select(ProductionCompletion).where(
                ProductionCompletion.origin == "receipt_auto"
            )
        )
        assert completion is not None
        lot = session.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None
        assert lot.warehouse_location_id == floor3_location_id
        assert lot.pallet_item is not None
        pallet = session.get(InventoryPallet, lot.pallet_item.pallet_id)
        assert pallet is not None
        assert pallet.location_id == floor3_location_id
        assert pallet.location_occupancy_key == "PRIMARY"
        assert pallet.is_current is True
        assert pallet.needs_relocation is False
        assert session.scalar(
            select(WarehouseGroundOccupancy.id).where(
                WarehouseGroundOccupancy.pallet_id == pallet.id
            )
        ) is None


def test_receipt_accepts_current_floor3_temporary_fallback(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot, InventoryPallet

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    _seed_published_fin_ground_plan(session_factory)
    temporary_location_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="F12",
        location_code="F12-P022-TEMP",
        storage_type="temporary_aisle",
        is_temporary=True,
    )
    _occupy_all_fin_locations(session_factory)
    _mock_floor3_runtime(
        monkeypatch,
        area_subtypes={"F12": "temporary_turnover"},
    )

    with TestClient(app) as client:
        _login(client, "admin")
        _receive_full_order_to_fallback(
            client,
            session_factory,
            key_prefix="p022-floor3-temporary",
        )

    with session_factory() as session:
        completion = session.scalar(
            select(ProductionCompletion).where(
                ProductionCompletion.origin == "receipt_auto"
            )
        )
        assert completion is not None
        lot = session.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None and lot.pallet_item is not None
        pallet = session.get(InventoryPallet, lot.pallet_item.pallet_id)
        assert pallet is not None
        assert pallet.location_id == temporary_location_id
        assert pallet.location_occupancy_key == "PRIMARY"
        assert pallet.is_current is True
        assert pallet.needs_relocation is True


def test_floor3_fallback_same_receipt_key_replay_has_zero_business_increment(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    _seed_published_fin_ground_plan(session_factory)
    _seed_floor3_v11_fallback(session_factory, area_code="E2")
    _occupy_all_fin_locations(session_factory)
    _mock_floor3_runtime(monkeypatch, area_codes=("E2",))

    with TestClient(app) as client:
        _login(client, "admin")
        source, frozen, received = _receive_full_order_to_fallback(
            client,
            session_factory,
            key_prefix="p022-floor3-replay",
        )
        before = _v11_receipt_fact_counts(session_factory)
        replayed = _receive(
            client,
            source,
            frozen,
            quantity=10,
            idempotency_key="p022-floor3-replay-receive",
        )
        after = _v11_receipt_fact_counts(session_factory)

    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["receipt_item_id"] == received.json()["receipt_item_id"]
    assert after == before


def test_floor3_fallback_revert_closes_lot_and_releases_primary_pallet(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryPallet,
        WarehouseGroundOccupancy,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    _seed_published_fin_ground_plan(session_factory)
    floor3_location_id = _seed_floor3_v11_fallback(session_factory, area_code="E2")
    _occupy_all_fin_locations(session_factory)
    _mock_floor3_runtime(monkeypatch, area_codes=("E2",))

    with TestClient(app) as client:
        _login(client, "admin")
        _source, _frozen, received = _receive_full_order_to_fallback(
            client,
            session_factory,
            key_prefix="p022-floor3-revert",
        )
        receipt_item_id = int(received.json()["receipt_item_id"])
        with session_factory() as session:
            completion = session.scalar(
                select(ProductionCompletion).where(
                    ProductionCompletion.origin == "receipt_auto"
                )
            )
            assert completion is not None
            completion_id = int(completion.id)
            lot = session.get(InventoryLot, completion.inventory_lot_id)
            assert lot is not None and lot.pallet_item is not None
            lot_id = int(lot.id)
            pallet_id = int(lot.pallet_item.pallet_id)

        reverted = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={},
        )
        assert reverted.status_code == 200, reverted.text

    with session_factory() as session:
        completion = session.get(ProductionCompletion, completion_id)
        lot = session.get(InventoryLot, lot_id)
        pallet = session.get(InventoryPallet, pallet_id)
        assert completion is not None and completion.status == "reversed"
        assert lot is not None
        assert lot.status == "closed"
        assert int(lot.quantity_available or 0) == 0
        assert int(lot.quantity_reserved or 0) == 0
        assert pallet is not None
        assert pallet.status == "closed"
        assert pallet.is_current is False
        assert pallet.location_id is None
        assert pallet.needs_relocation is False
        assert session.scalar(
            select(InventoryPallet.id).where(
                InventoryPallet.location_id == floor3_location_id,
                InventoryPallet.is_current.is_(True),
            )
        ) is None
        assert session.scalar(
            select(WarehouseGroundOccupancy.id).where(
                WarehouseGroundOccupancy.pallet_id == pallet_id,
                WarehouseGroundOccupancy.status == "active",
            )
        ) is None


def test_floor3_primary_pallet_can_transfer_to_stock_and_replay_once(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.production import ProductionCompletion, ProductionStockTransfer
    from app.models.user import User
    from app.models.warehouse_inventory import (
        InventoryLocationMovement,
        InventoryLot,
        InventoryMovement,
        InventoryPallet,
    )
    from app.services.production_workflow import (
        StockTransferCommand,
        transfer_direct_completion_to_stock,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    _seed_published_fin_ground_plan(session_factory)
    source_location_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="E2",
    )
    target_location_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="E3",
    )
    _occupy_all_fin_locations(session_factory)
    _mock_floor3_runtime(monkeypatch, area_codes=("E2", "E3"))

    with TestClient(app) as client:
        _login(client, "admin")
        _receive_full_order_to_fallback(
            client,
            session_factory,
            key_prefix="p022-floor3-transfer",
        )
        with session_factory() as session:
            completion = session.scalar(
                select(ProductionCompletion).where(
                    ProductionCompletion.origin == "receipt_auto"
                )
            )
            assert completion is not None
            completion_id = int(completion.id)
            lot = session.get(InventoryLot, completion.inventory_lot_id)
            assert lot is not None and lot.pallet_item is not None
            assert lot.warehouse_location_id == source_location_id
            source_pallet_id = int(lot.pallet_item.pallet_id)

    command = StockTransferCommand(
        idempotency_key="p022-floor3-transfer-stock",
        location_id=target_location_id,
        expected_layout_version=3,
    )
    with session_factory() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        transferred = transfer_direct_completion_to_stock(
            session,
            completion_id=completion_id,
            command=command,
            operator_id=admin.id,
        )
        assert transferred.replayed is False
        transfer_id = int(transferred.transfer.id)
        session.commit()
    after_transfer = _v11_receipt_fact_counts(session_factory)
    with session_factory() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        replayed = transfer_direct_completion_to_stock(
            session,
            completion_id=completion_id,
            command=command,
            operator_id=admin.id,
        )
        assert replayed.replayed is True
        assert int(replayed.transfer.id) == transfer_id
        session.commit()
    after_replay = _v11_receipt_fact_counts(session_factory)

    assert after_replay == after_transfer
    with session_factory() as session:
        completion = session.get(ProductionCompletion, completion_id)
        assert completion is not None
        lot = session.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None and lot.pallet_item is not None
        assert lot.warehouse_location_id == target_location_id
        target_pallet = session.get(InventoryPallet, lot.pallet_item.pallet_id)
        assert target_pallet is not None
        assert target_pallet.location_id == target_location_id
        assert target_pallet.location_occupancy_key == "PRIMARY"
        assert target_pallet.is_current is True
        assert session.scalar(
            select(InventoryPallet.id).where(
                InventoryPallet.location_id == source_location_id,
                InventoryPallet.is_current.is_(True),
            )
        ) is None
        source_pallet = session.get(InventoryPallet, source_pallet_id)
        assert source_pallet is not None
        assert source_pallet_id == target_pallet.id or source_pallet.is_current is False
        clear_movement = session.scalar(
            select(InventoryLocationMovement).where(
                InventoryLocationMovement.pallet_id == source_pallet_id,
                InventoryLocationMovement.movement_type == "clear",
            )
        )
        assert clear_movement is not None
        assert clear_movement.remarks == "直接待送完工整批转入正式库存位"
        stock_movement = session.scalar(
            select(InventoryMovement).where(
                InventoryMovement.inventory_lot_id == lot.id,
                InventoryMovement.movement_type == "adjust",
            )
        )
        assert stock_movement is not None
        assert stock_movement.reason == "直接待送完工整批转入正式库位"
        assert int(
            session.scalar(
                select(func.count(ProductionStockTransfer.id)).where(
                    ProductionStockTransfer.completion_id == completion_id
                )
            )
            or 0
        ) == 1


def test_floor3_moved_completion_key_stays_compatible_without_accepting_other_keys(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.production import ProductionCompletion
    from app.models.user import User
    from app.models.warehouse_inventory import InventoryLot, InventoryPallet
    from app.services.production_workflow import (
        ProductionWorkflowError,
        StockTransferCommand,
        transfer_direct_completion_to_stock,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    _seed_published_fin_ground_plan(session_factory)
    source_location_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="E2",
    )
    target_location_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="E3",
    )
    _occupy_all_fin_locations(session_factory)
    _mock_floor3_runtime(monkeypatch, area_codes=("E2", "E3"))

    with TestClient(app) as client:
        _login(client, "admin")
        _receive_full_order_to_fallback(
            client,
            session_factory,
            key_prefix="p022-floor3-moved-key",
        )

    with session_factory() as session:
        completion = session.scalar(
            select(ProductionCompletion).where(
                ProductionCompletion.origin == "receipt_auto"
            )
        )
        assert completion is not None
        completion_id = int(completion.id)
        completion.origin = "manual"
        lot = session.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None and lot.pallet_item is not None
        assert lot.warehouse_location_id == source_location_id
        pallet = session.get(InventoryPallet, lot.pallet_item.pallet_id)
        assert pallet is not None
        pallet.location_occupancy_key = "UNRELATED-PALLET-KEY"
        session.commit()

    command = StockTransferCommand(
        idempotency_key="p022-floor3-moved-key-transfer",
        location_id=target_location_id,
        expected_layout_version=3,
    )
    with session_factory() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        with pytest.raises(
            ProductionWorkflowError,
            match="当前直接待送系统栈板状态异常",
        ):
            transfer_direct_completion_to_stock(
                session,
                completion_id=completion_id,
                command=command,
                operator_id=admin.id,
            )
        session.rollback()

    with session_factory() as session:
        completion = session.get(ProductionCompletion, completion_id)
        assert completion is not None
        lot = session.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None and lot.pallet_item is not None
        pallet = session.get(InventoryPallet, lot.pallet_item.pallet_id)
        assert pallet is not None
        pallet.location_occupancy_key = f"PRODUCTION_COMPLETION:{completion_id}"
        session.commit()

    with session_factory() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        transferred = transfer_direct_completion_to_stock(
            session,
            completion_id=completion_id,
            command=command,
            operator_id=admin.id,
        )
        assert transferred.replayed is False
        session.commit()

    with session_factory() as session:
        completion = session.get(ProductionCompletion, completion_id)
        assert completion is not None
        lot = session.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None and lot.pallet_item is not None
        assert lot.warehouse_location_id == target_location_id
        target_pallet = session.get(InventoryPallet, lot.pallet_item.pallet_id)
        assert target_pallet is not None
        assert target_pallet.is_current is True
        assert target_pallet.location_id == target_location_id
        assert (
            target_pallet.location_occupancy_key
            == f"PRODUCTION_COMPLETION:{completion_id}"
        )
        assert session.scalar(
            select(InventoryPallet.id).where(
                InventoryPallet.location_id == source_location_id,
                InventoryPallet.is_current.is_(True),
            )
        ) is None


def test_floor3_fallback_respects_confirmed_area_capacity(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryPallet,
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    _app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    _seed_published_fin_ground_plan(session_factory)
    full_location_id = _seed_floor3_v11_fallback(session_factory, area_code="A2")
    safe_location_id = _seed_floor3_v11_fallback(session_factory, area_code="E2")
    _occupy_all_fin_locations(session_factory)
    _mock_floor3_runtime(monkeypatch, area_codes=("A2", "E2"))

    with session_factory() as session:
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 3)
        )
        assert floor is not None
        full_area = session.scalar(
            select(WarehouseArea).where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code == "A2",
            )
        )
        assert full_area is not None
        full_area.confirmed_pallet_capacity = 1
        sibling = WarehouseLocation(
            location_code="A2-P022-OCCUPIED",
            location_name="匿名三楼 A2 已占用位",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="A2",
            storage_type="ground",
            placement_status="placed",
            source_version="V11",
            sort_order=2,
        )
        session.add(sibling)
        session.flush()
        session.add(
            InventoryPallet(
                pallet_code="P022-A2-CAPACITY",
                location_id=sibling.id,
                location_occupancy_key="PRIMARY",
                status="active",
                is_current=True,
                needs_relocation=False,
                version=1,
                created_by=1,
                updated_by=1,
            )
        )
        session.commit()

    with session_factory() as session:
        from app.services.production_workflow import _receipt_auto_location_name

        projection = receipt_auto_finished_location_projection(session)
        full_location = session.get(WarehouseLocation, full_location_id)
        safe_location = session.get(WarehouseLocation, safe_location_id)
        assert full_location is not None and safe_location is not None
        assert projection["ready"] is True
        assert projection["location_name"] == _receipt_auto_location_name(safe_location)
        assert projection["location_name"] != _receipt_auto_location_name(full_location)


def test_floor3_fallback_rejects_wrong_zone_unplaced_full_and_policy_bound_locations(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryPallet,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseLocation,
    )
    from app.services.production_workflow import _receipt_auto_finished_ground_targets

    _app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    _seed_published_fin_ground_plan(session_factory)
    capacity_full_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="A2",
    )
    wrong_rack_zone_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="D2",
    )
    policy_bound_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="E1",
    )
    unplaced_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="E2",
        placement_status="unplaced",
    )
    safe_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="E3",
    )
    wrong_surplus_zone_id = _seed_floor3_v11_fallback(
        session_factory,
        area_code="E4",
    )
    _occupy_all_fin_locations(session_factory)

    with session_factory() as session:
        floor = session.scalar(
            select(WarehouseFloor).where(WarehouseFloor.floor_number == 3)
        )
        assert floor is not None
        areas = {
            area.area_code: area
            for area in session.scalars(
                select(WarehouseArea).where(WarehouseArea.floor_id == floor.id)
            ).all()
        }
        areas["A2"].confirmed_pallet_capacity = 1
        occupied = WarehouseLocation(
            location_code="A2-P022-CAPACITY",
            location_name="Anonymous 3F A2 occupied position",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="A2",
            storage_type="ground",
            placement_status="placed",
            source_version="V11",
            sort_order=2,
        )
        session.add(occupied)
        session.flush()
        session.add(
            InventoryPallet(
                pallet_code="P022-A2-CAPACITY-REJECTION",
                location_id=occupied.id,
                location_occupancy_key="PRIMARY",
                status="active",
                is_current=True,
                needs_relocation=False,
                version=1,
                created_by=1,
                updated_by=1,
            )
        )
        session.add(
            WarehouseAreaStoragePolicy(
                area_id=areas["E1"].id,
                map_feature_id="zone-p022-e1-policy-bound",
                allowed_inventory_types_json='["finished"]',
                storage_layout="pallet_ground",
                status="published",
                published_map_revision="p022-current-3f-map",
                version=1,
                updated_by=1,
            )
        )
        session.commit()

    _mock_floor3_runtime(
        monkeypatch,
        area_subtypes={
            "A2": "finished_storage",
            "D2": "rack_storage",
            "E1": "finished_storage",
            "E2": "finished_storage",
            "E3": "finished_storage",
            "E4": "delivery_surplus",
        },
    )
    with session_factory() as session:
        targets = _receipt_auto_finished_ground_targets(session)

    target_ids = {int(target.location.id) for target in targets}
    assert target_ids == {safe_id}
    assert target_ids.isdisjoint(
        {
            capacity_full_id,
            wrong_rack_zone_id,
            policy_bound_id,
            unplaced_id,
            wrong_surplus_zone_id,
        }
    )


def test_floor3_fallback_fails_closed_if_policy_is_created_after_selection(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.warehouse_inventory import (
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseLocation,
    )
    from app.services import production_workflow
    from app.services.production_workflow import ProductionWorkflowError

    _app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    _seed_published_fin_ground_plan(session_factory)
    _seed_floor3_v11_fallback(session_factory, area_code="E2")
    _occupy_all_fin_locations(session_factory)
    _mock_floor3_runtime(monkeypatch, area_codes=("E2",))

    original_claim = production_workflow.claim_active_placed_location
    policy_created = False

    def claim_then_publish_policy(
        db,
        location_id: int,
        *,
        expected_layout_version: int | None = None,
    ) -> bool:
        nonlocal policy_created
        claimed = original_claim(
            db,
            location_id,
            expected_layout_version=expected_layout_version,
        )
        if claimed and not policy_created:
            location = db.get(WarehouseLocation, location_id)
            assert location is not None
            floor = db.scalar(
                select(WarehouseFloor).where(
                    WarehouseFloor.floor_number == location.warehouse_floor
                )
            )
            assert floor is not None
            area = db.scalar(
                select(WarehouseArea).where(
                    WarehouseArea.floor_id == floor.id,
                    WarehouseArea.area_code == location.area_code,
                )
            )
            assert area is not None
            db.add(
                WarehouseAreaStoragePolicy(
                    area_id=area.id,
                    map_feature_id="zone-p022-policy-created-after-selection",
                    allowed_inventory_types_json='["finished"]',
                    storage_layout="pallet_ground",
                    status="published",
                    published_map_revision="p022-current-3f-map",
                    version=1,
                    updated_by=1,
                )
            )
            db.flush()
            policy_created = True
        return claimed

    monkeypatch.setattr(
        production_workflow,
        "claim_active_placed_location",
        claim_then_publish_policy,
    )
    with session_factory() as session:
        with pytest.raises(
            ProductionWorkflowError,
            match="可用成品位置刚刚发生变化",
        ):
            production_workflow._receipt_auto_finished_ground_target(
                session,
                claim=True,
            )
        session.rollback()

    assert policy_created is True


def test_receipt_posts_lot_pallet_and_ground_occupancy_to_real_fin_then_releases_on_revert(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryPallet,
        WarehouseGroundOccupancy,
        WarehouseGroundOccupancySlot,
        WarehouseGroundPlacementMutation,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    expected_location_id = _seed_published_fin_ground_plan(session_factory)
    _mock_fin_runtime_identity(session_factory, monkeypatch)

    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p019-fin-price",
        )
        assert frozen.status_code == 200, frozen.text
        received = _receive(
            client,
            source,
            frozen.json(),
            quantity=10,
            idempotency_key="p019-fin-receive",
        )
        assert received.status_code == 200, received.text
        receipt_item_id = int(received.json()["receipt_item_id"])

        with session_factory() as session:
            completion = session.scalar(
                select(ProductionCompletion).where(
                    ProductionCompletion.origin == "receipt_auto"
                )
            )
            assert completion is not None
            lot = session.get(InventoryLot, completion.inventory_lot_id)
            assert lot is not None
            assert lot.warehouse_location_id == expected_location_id
            assert lot.pallet_item is not None
            pallet = session.get(InventoryPallet, lot.pallet_item.pallet_id)
            assert pallet is not None and pallet.is_current
            assert (
                pallet.location_occupancy_key
                == f"PRODUCTION_COMPLETION:{completion.id}"
            )
            occupancy = session.scalar(
                select(WarehouseGroundOccupancy).where(
                    WarehouseGroundOccupancy.pallet_id == pallet.id,
                    WarehouseGroundOccupancy.status == "active",
                )
            )
            assert occupancy is not None
            assert occupancy.primary_location_id == expected_location_id
            assert occupancy.capacity_quantity == 10
            assert session.scalar(
                select(WarehouseGroundOccupancySlot.id).where(
                    WarehouseGroundOccupancySlot.occupancy_id == occupancy.id,
                    WarehouseGroundOccupancySlot.location_id == expected_location_id,
                    WarehouseGroundOccupancySlot.status == "active",
                )
            ) is not None
            assert session.scalar(
                select(WarehouseGroundPlacementMutation.id).where(
                    WarehouseGroundPlacementMutation.occupancy_id == occupancy.id
                )
            ) is not None

        reverted = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={},
        )
        assert reverted.status_code == 200, reverted.text

        with session_factory() as session:
            assert session.scalar(
                select(WarehouseGroundOccupancy.id).where(
                    WarehouseGroundOccupancy.status == "active"
                )
            ) is None
            released = session.scalar(
                select(WarehouseGroundOccupancy).where(
                    WarehouseGroundOccupancy.status == "released"
                )
            )
            assert released is not None
