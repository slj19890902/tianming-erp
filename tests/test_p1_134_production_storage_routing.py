from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, func, select

from app.models.customer_finished_storage_preference import (
    CustomerFinishedStoragePreference,
)
from app.models.production import ProductionCompletion, ProductionStockTransfer
from app.models.user import User
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryPallet,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseLocation,
)
from app.services.production_workflow import ProductionWorkflowError
from tests.test_n029_production_service import _complete, _login, production_app
from tests.test_p1_134_customer_finished_storage import (
    customer_finished_storage_app,
)


def test_mobile_production_surface_remains_read_only() -> None:
    from app.api.mobile_erp import router as mobile_router

    production_routes = {
        route.path: set(route.methods or set())
        for route in mobile_router.routes
        if route.path.startswith("/production/")
    }
    assert production_routes == {
        "/production/tasks": {"GET"},
        "/production/tasks/{task_id}/drawing": {"GET"},
        "/production/recent": {"GET"},
        "/production/pending/lookup": {"GET"},
    }


def _set_preferences(
    db,
    *,
    customer_id: int,
    area_ids: Iterable[int],
) -> None:
    db.execute(
        delete(CustomerFinishedStoragePreference).where(
            CustomerFinishedStoragePreference.customer_id == customer_id
        )
    )
    db.add_all(
        CustomerFinishedStoragePreference(
            customer_id=customer_id,
            warehouse_area_id=int(area_id),
            priority=priority,
        )
        for priority, area_id in enumerate(area_ids, start=1)
    )
    db.flush()


def _patch_floor_identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    floor_number: int,
    revision: str,
    features: dict[str, str],
) -> None:
    from app.services import location_candidates

    previous = location_candidates.load_warehouse_twin_published_floor_identity

    def load_identity(requested_floor_number: int):
        if int(requested_floor_number) == int(floor_number):
            return {
                "revision": revision,
                "zones_by_id": dict(features),
                "zone_ids_by_area": {
                    area_code: tuple(
                        feature_id
                        for feature_id, mapped_area_code in features.items()
                        if mapped_area_code == area_code
                    )
                    for area_code in set(features.values())
                },
            }
        return previous(requested_floor_number)

    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        load_identity,
    )


def _seed_formal_location(
    db,
    *,
    floor_number: int,
    area_code: str,
    location_code: str,
    map_revision: str,
    map_feature_id: str,
    storage_type: str,
    actor_id: int,
    with_ground_plan: bool = False,
) -> tuple[WarehouseArea, WarehouseLocation]:
    floor = db.scalar(
        select(WarehouseFloor).where(
            WarehouseFloor.floor_number == floor_number
        )
    )
    if floor is None:
        floor = WarehouseFloor(
            floor_code=f"P134-{floor_number}F",
            floor_name=f"P1-134 {floor_number}F",
            floor_number=floor_number,
            construction_status="enabled",
        )
        db.add(floor)
        db.flush()
    area = WarehouseArea(
        floor_id=floor.id,
        area_code=area_code,
        area_name=f"{area_code} 成品区",
        construction_status="enabled",
        planned_location_count=1,
    )
    area.storage_policy = WarehouseAreaStoragePolicy(
        map_feature_id=map_feature_id,
        allowed_inventory_types_json='["finished"]',
        storage_layout="pallet_ground" if with_ground_plan else "rack",
        status="published",
        published_map_revision=map_revision,
        version=1,
        updated_by=actor_id,
    )
    db.add(area)
    db.flush()
    location = WarehouseLocation(
        location_code=location_code,
        location_name=f"{area_code} 1号位",
        warehouse_type="finished",
        is_active=True,
        warehouse_floor=floor_number,
        area_code=area_code,
        storage_type=storage_type,
        sort_order=1,
        is_temporary=False,
        source_version="CURRENT_MAP" if floor_number != 1 else "TWIN_V1",
        address_kind="legacy",
        address_area_id=area.id,
        placement_status="placed",
    )
    location.floor3_layout = Floor3LocationLayout(
        left_pct=Decimal("10"),
        top_pct=Decimal("10"),
        width_pct=Decimal("5"),
        height_pct=Decimal("5"),
        version=1,
        source_type="manual",
        layout_kind=(
            "physical_pallet" if storage_type == "ground" else "physical_rack"
        ),
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.add(location)
    db.flush()
    if with_ground_plan:
        plan = WarehouseGroundLayoutPlan(
            area_id=area.id,
            status="published",
            target_slot_count=1,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision=map_revision,
            published_map_revision=map_revision,
            preview_fingerprint="a" * 64,
            version=1,
            publish_idempotency_key=f"p1-134-{floor_number}-{area_code}",
            publish_request_hash="b" * 64,
            updated_by=actor_id,
            published_by=actor_id,
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
    db.flush()
    return area, location


def _seed_legacy_fin_then_floor3(
    factory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    actor_id: int,
) -> tuple[int, int]:
    map_revision = "p1-134-legacy-fin-map"
    map_feature_id = "ZONE-P134-1F-FIN-001"
    with factory() as db:
        _fin_area, fin_location = _seed_formal_location(
            db,
            floor_number=1,
            area_code="FIN-001",
            location_code="P134-1F-FIN-001-L001",
            map_revision=map_revision,
            map_feature_id=map_feature_id,
            storage_type="ground",
            actor_id=actor_id,
            with_ground_plan=True,
        )
        floor3 = WarehouseFloor(
            floor_code="P134-3F",
            floor_name="P1-134 3F",
            floor_number=3,
            construction_status="enabled",
        )
        db.add(floor3)
        db.flush()
        floor3_area = WarehouseArea(
            floor_id=floor3.id,
            area_code="P134-FALLBACK",
            area_name="三楼旧规则成品区",
            construction_status="enabled",
        )
        db.add(floor3_area)
        db.flush()
        floor3_location = WarehouseLocation(
            location_code="P134-3F-FALLBACK-L001",
            location_name="三楼旧规则空位",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code=floor3_area.area_code,
            storage_type="ground",
            sort_order=1,
            is_temporary=False,
            source_version="V11",
            address_kind="legacy",
            address_area_id=floor3_area.id,
            placement_status="placed",
        )
        floor3_location.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal("20"),
            top_pct=Decimal("20"),
            width_pct=Decimal("5"),
            height_pct=Decimal("5"),
            version=1,
            source_type="manual",
            layout_kind="physical_pallet",
            created_by=actor_id,
            updated_by=actor_id,
        )
        db.add(floor3_location)
        db.commit()
        fin_location_id = int(fin_location.id)
        floor3_location_id = int(floor3_location.id)

    _patch_floor_identity(
        monkeypatch,
        floor_number=1,
        revision=map_revision,
        features={map_feature_id: "FIN-001"},
    )
    from app.services import production_workflow

    monkeypatch.setattr(
        production_workflow,
        "load_warehouse_twin_floor",
        lambda floor_code: {
            "floor_code": floor_code,
            "revision": "p1-134-current-3f-map",
            "features": [
                {
                    "id": "ZONE-P134-3F-FALLBACK",
                    "feature_kind": "zone",
                    "erp_area_code": "P134-FALLBACK",
                    "subtype": "finished_storage",
                    "storage_mode": "floor",
                    "points": [[0, 0], [100, 0], [100, 100], [0, 100]],
                }
            ],
        },
    )
    return fin_location_id, floor3_location_id


def test_customer_without_preference_keeps_fin_then_floor3_fallback(
    customer_finished_storage_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.production_workflow import _receipt_auto_finished_ground_targets

    factory = customer_finished_storage_app.state.session_factory
    ids = customer_finished_storage_app.state.ids
    fin_location_id, floor3_location_id = _seed_legacy_fin_then_floor3(
        factory,
        monkeypatch,
        actor_id=ids["admin"],
    )

    with factory() as db:
        first = _receipt_auto_finished_ground_targets(
            db,
            customer_id=ids["customer"],
        )
        assert [int(target.location.id) for target in first] == [fin_location_id]
        assert first[0].target_kind == "fin_ground_plan"
        db.add(
            InventoryPallet(
                pallet_code="P134-LEGACY-FIN-OCCUPIED",
                location_id=fin_location_id,
                status="active",
                is_current=True,
                created_by=ids["admin"],
                updated_by=ids["admin"],
            )
        )
        db.commit()

    with factory() as db:
        fallback = _receipt_auto_finished_ground_targets(
            db,
            customer_id=ids["customer"],
        )
        assert [int(target.location.id) for target in fallback] == [
            floor3_location_id
        ]
        assert fallback[0].target_kind == "floor3_v11"


@pytest.mark.parametrize(
    ("ordered_area_keys", "expected_area_key"),
    [
        (("a1", "a2"), "a1"),
        (("a2", "a1"), "a1"),
    ],
)
def test_customer_preference_uses_first_available_area_in_saved_order(
    customer_finished_storage_app,
    ordered_area_keys: tuple[str, str],
    expected_area_key: str,
) -> None:
    from app.services.production_workflow import _receipt_auto_finished_ground_targets

    factory = customer_finished_storage_app.state.session_factory
    ids = customer_finished_storage_app.state.ids
    with factory() as db:
        _set_preferences(
            db,
            customer_id=ids["customer"],
            area_ids=[ids[key] for key in ordered_area_keys],
        )
        db.commit()

    with factory() as db:
        targets = _receipt_auto_finished_ground_targets(
            db,
            customer_id=ids["customer"],
        )
        assert targets
        assert int(targets[0].area.id) == ids[expected_area_key]
        assert targets[0].location.location_code == "4F-A1-L002"
        assert targets[0].target_kind == "preferred_location"


def test_customer_preference_skips_capacity_full_first_area(
    customer_finished_storage_app,
) -> None:
    from app.services.production_workflow import _receipt_auto_finished_ground_targets

    factory = customer_finished_storage_app.state.session_factory
    ids = customer_finished_storage_app.state.ids
    with factory() as db:
        first_area = db.get(WarehouseArea, ids["a1"])
        assert first_area is not None
        first_area.capacity_review_status = "confirmed"
        first_area.capacity_eligible = True
        first_area.confirmed_pallet_capacity = 1
        first_area.capacity_reviewed_by = "P1-134 回归测试"
        first_area.capacity_reviewed_at = datetime.now()
        second_location = db.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.warehouse_floor == 4,
                WarehouseLocation.area_code == "A2",
            )
        )
        assert second_location is not None
        db.execute(
            delete(InventoryPallet).where(
                InventoryPallet.location_id == second_location.id
            )
        )
        _set_preferences(
            db,
            customer_id=ids["customer"],
            area_ids=[ids["a1"], ids["a2"]],
        )
        db.commit()

    with factory() as db:
        targets = _receipt_auto_finished_ground_targets(
            db,
            customer_id=ids["customer"],
        )
        assert targets
        assert int(targets[0].area.id) == ids["a2"]
        assert targets[0].location.location_code == "4F-A2-L001"
        assert all(int(target.area.id) != ids["a1"] for target in targets)


def test_customer_preference_rechecks_capacity_after_location_claim(
    customer_finished_storage_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import production_workflow

    factory = customer_finished_storage_app.state.session_factory
    ids = customer_finished_storage_app.state.ids
    with factory() as db:
        first_area = db.get(WarehouseArea, ids["a1"])
        assert first_area is not None
        first_area.capacity_review_status = "confirmed"
        first_area.capacity_eligible = True
        first_area.confirmed_pallet_capacity = 1
        first_area.capacity_reviewed_by = "P1-134 回归测试"
        first_area.capacity_reviewed_at = datetime.now()
        preference_location_ids = list(
            db.scalars(
                select(WarehouseLocation.id).where(
                    WarehouseLocation.warehouse_floor == 4,
                    WarehouseLocation.area_code.in_(("A1", "A2")),
                )
            ).all()
        )
        db.execute(
            delete(InventoryPallet).where(
                InventoryPallet.location_id.in_(preference_location_ids)
            )
        )
        _set_preferences(
            db,
            customer_id=ids["customer"],
            area_ids=[ids["a1"], ids["a2"]],
        )
        db.commit()

    original_claim = production_workflow.claim_active_placed_location
    capacity_filled_after_claim = False

    def claim_then_fill_first_area(
        db,
        location_id: int,
        *,
        expected_layout_version: int | None = None,
    ) -> bool:
        nonlocal capacity_filled_after_claim
        claimed = original_claim(
            db,
            location_id,
            expected_layout_version=expected_layout_version,
        )
        location = db.get(WarehouseLocation, location_id)
        if (
            claimed
            and not capacity_filled_after_claim
            and location is not None
            and location.area_code == "A1"
        ):
            sibling_id = db.scalar(
                select(WarehouseLocation.id)
                .where(
                    WarehouseLocation.warehouse_floor == 4,
                    WarehouseLocation.area_code == "A1",
                    WarehouseLocation.id != location.id,
                )
                .order_by(WarehouseLocation.id)
                .limit(1)
            )
            assert sibling_id is not None
            db.add(
                InventoryPallet(
                    pallet_code="P134-CAPACITY-RACE",
                    location_id=int(sibling_id),
                    status="active",
                    is_current=True,
                    created_by=ids["admin"],
                    updated_by=ids["admin"],
                )
            )
            db.flush()
            capacity_filled_after_claim = True
        return claimed

    monkeypatch.setattr(
        production_workflow,
        "claim_active_placed_location",
        claim_then_fill_first_area,
    )
    with factory() as db:
        target = production_workflow._receipt_auto_finished_ground_target(
            db,
            claim=True,
            customer_id=ids["customer"],
        )
        assert capacity_filled_after_claim is True
        assert int(target.area.id) == ids["a2"]
        assert target.location.location_code == "4F-A2-L001"
        db.rollback()


def test_full_customer_preferences_fail_closed_even_when_fin_is_available(
    customer_finished_storage_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.production_workflow import (
        _receipt_auto_finished_ground_target,
        _receipt_auto_finished_ground_targets,
    )

    factory = customer_finished_storage_app.state.session_factory
    ids = customer_finished_storage_app.state.ids
    fin_location_id, _floor3_location_id = _seed_legacy_fin_then_floor3(
        factory,
        monkeypatch,
        actor_id=ids["admin"],
    )
    with factory() as db:
        _set_preferences(
            db,
            customer_id=ids["customer"],
            area_ids=[ids["a2"]],
        )
        db.commit()

    with factory() as db:
        assert _receipt_auto_finished_ground_targets(db)[0].location.id == fin_location_id
        with pytest.raises(
            ProductionWorkflowError,
            match="不会改放一楼",
        ) as error:
            _receipt_auto_finished_ground_target(
                db,
                claim=False,
                customer_id=ids["customer"],
            )
        assert error.value.status_code == 409


def _seed_production_customer_areas(
    factory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    customer_id: int,
    actor_id: int,
    specs: tuple[tuple[str, str, bool], ...],
) -> dict[str, tuple[int, int]]:
    revision = "p1-134-production-4f-map"
    features: dict[str, str] = {}
    seeded: dict[str, tuple[int, int]] = {}
    with factory() as db:
        for index, (area_code, storage_type, with_ground_plan) in enumerate(
            specs,
            start=1,
        ):
            feature_id = f"ZONE-P134-4F-{area_code}"
            area, location = _seed_formal_location(
                db,
                floor_number=4,
                area_code=area_code,
                location_code=f"P134-4F-{area_code}-L001",
                map_revision=revision,
                map_feature_id=feature_id,
                storage_type=storage_type,
                actor_id=actor_id,
                with_ground_plan=with_ground_plan,
            )
            location.sort_order = index
            features[feature_id] = area_code
            seeded[area_code] = (int(area.id), int(location.id))
        db.flush()
        _set_preferences(
            db,
            customer_id=customer_id,
            area_ids=[seeded[specs[0][0]][0]],
        )
        db.commit()
    _patch_floor_identity(
        monkeypatch,
        floor_number=4,
        revision=revision,
        features=features,
    )
    return seeded


def test_manual_stock_completion_rejects_location_outside_customer_preferences(
    production_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, factory, ids = production_app
    with factory() as db:
        actor_id = int(
            db.scalar(select(User.id).where(User.username == "n029-admin"))
        )
    seeded = _seed_production_customer_areas(
        factory,
        monkeypatch,
        customer_id=ids["customer_a"],
        actor_id=actor_id,
        specs=(("PREF", "rack", False), ("OUTSIDE", "rack", False)),
    )

    with TestClient(app) as client:
        _login(client)
        rejected = _complete(
            client,
            ids,
            "stock",
            idempotency_key="p1-134-stock-outside-preference",
            disposition="stock",
            location_id=seeded["OUTSIDE"][1],
            expected_layout_version=1,
        )
    assert rejected.status_code == 409, rejected.text
    assert "该客户已指定默认成品区域" in rejected.json()["detail"]

    with factory() as db:
        assert (
            db.scalar(
                select(func.count(ProductionCompletion.id)).where(
                    ProductionCompletion.task_id
                    == ids["cases"]["stock"]["task"]
                )
            )
            == 0
        )


def test_direct_completion_stock_transfer_cannot_bypass_customer_preferences(
    production_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, factory, ids = production_app
    with factory() as db:
        actor_id = int(
            db.scalar(select(User.id).where(User.username == "n029-admin"))
        )
    seeded = _seed_production_customer_areas(
        factory,
        monkeypatch,
        customer_id=ids["customer_a"],
        actor_id=actor_id,
        specs=(
            ("PREFERRED-GROUND", "ground", True),
            ("OUTSIDE-TRANSFER", "rack", False),
        ),
    )
    preferred_location_id = seeded["PREFERRED-GROUND"][1]
    outside_location_id = seeded["OUTSIDE-TRANSFER"][1]

    with TestClient(app) as client:
        _login(client)
        completed = _complete(
            client,
            ids,
            "transfer",
            idempotency_key="p1-134-transfer-source-complete",
            disposition="direct",
        )
        assert completed.status_code == 200, completed.text
        completion_id = int(completed.json()["items"][0]["id"])
        assert (
            completed.json()["items"][0]["warehouse_location_id"]
            == preferred_location_id
        )
        rejected = client.post(
            f"/api/production/completions/{completion_id}/stock-transfers",
            json={
                "idempotency_key": "p1-134-transfer-outside-preference",
                "location_id": outside_location_id,
                "expected_layout_version": 1,
            },
        )

    assert rejected.status_code == 409, rejected.text
    assert "该客户已指定默认成品区域" in rejected.json()["detail"]
    with factory() as db:
        completion = db.get(ProductionCompletion, completion_id)
        assert completion is not None
        assert int(completion.warehouse_location_id) == preferred_location_id
        assert (
            db.scalar(
                select(func.count(ProductionStockTransfer.id)).where(
                    ProductionStockTransfer.completion_id == completion_id
                )
            )
            == 0
        )


def test_preferred_4f_ground_plan_completes_as_preferred_not_legacy_fin(
    production_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.warehouse_inventory import WarehouseGroundOccupancy
    from app.services.production_workflow import _receipt_auto_finished_ground_targets

    app, factory, ids = production_app
    with factory() as db:
        actor_id = int(
            db.scalar(select(User.id).where(User.username == "n029-admin"))
        )
    seeded = _seed_production_customer_areas(
        factory,
        monkeypatch,
        customer_id=ids["customer_a"],
        actor_id=actor_id,
        specs=(("GROUND", "ground", True),),
    )
    ground_area_id, ground_location_id = seeded["GROUND"]

    with factory() as db:
        target = _receipt_auto_finished_ground_targets(
            db,
            customer_id=ids["customer_a"],
        )[0]
        assert target.target_kind == "preferred_location"
        assert target.uses_ground_plan is True
        assert int(target.area.id) == ground_area_id
        assert int(target.location.warehouse_floor) == 4
        assert target.location.area_code not in {"FIN-001", "FIN-002", "FIN-003"}

    with TestClient(app) as client:
        _login(client)
        completed = _complete(
            client,
            ids,
            "idem",
            idempotency_key="p1-134-preferred-4f-ground-direct",
            disposition="direct",
        )
    assert completed.status_code == 200, completed.text
    assert completed.json()["items"][0]["warehouse_location_id"] == ground_location_id

    with factory() as db:
        completion = db.scalar(
            select(ProductionCompletion).where(
                ProductionCompletion.task_id == ids["cases"]["idem"]["task"]
            )
        )
        assert completion is not None
        assert int(completion.warehouse_location_id) == ground_location_id
        occupancy = db.scalar(
            select(WarehouseGroundOccupancy).where(
                WarehouseGroundOccupancy.primary_location_id == ground_location_id,
                WarehouseGroundOccupancy.status == "active",
            )
        )
        assert occupancy is not None
