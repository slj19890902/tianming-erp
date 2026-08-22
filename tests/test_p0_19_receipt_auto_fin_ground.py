from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
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


def test_receipt_preview_uses_real_published_fin_member_not_legacy_dispatch(
    requisition_app,
) -> None:
    _app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    expected_location_id = _seed_published_fin_ground_plan(session_factory)

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
            "location_name": location.location_name,
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


def test_receipt_posts_lot_pallet_and_ground_occupancy_to_real_fin_then_releases_on_revert(
    requisition_app,
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
