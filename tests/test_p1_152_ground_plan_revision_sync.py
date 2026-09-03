from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import sessionmaker

from app.api import warehouse as warehouse_api
from app.core.database import create_sqlite_engine
from app.models import Base
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
from app.services import warehouse_ground_plan_revision_sync as revision_sync
from app.services.warehouse_floor1_candidate_planner import (
    validate_capacity_layout_slots_for_zone,
)
from app.services.warehouse_ground_slots import (
    WarehouseGroundSlotError,
    number_ground_physical_slots,
)


CURRENT_REVISION = "p1-152-current-4f"
TARGET_REVISION = "p1-152-target-4f"
TRIGGER_NAME = "trg_ground_plans_published_immutable"
SLOT_COUNT_PER_PLAN = 12


@dataclass(frozen=True)
class RevisionWorld:
    session_factory: sessionmaker
    current_layout: dict
    target_layout: dict
    operator_id: int
    plan_ids: tuple[int, int]
    policy_ids: tuple[int, int]


def _zone_feature(
    *,
    feature_id: str,
    area_code: str,
    area_id: int,
    floor_id: int,
    bottom_mm: int,
) -> dict:
    return {
        "id": feature_id,
        "feature_kind": "zone",
        "feature_code": f"ZONE-4F-{area_code}",
        "name": f"四楼{area_code}区",
        "erp_area_code": area_code,
        "formal_floor_id": floor_id,
        "formal_area_id": area_id,
        "points": [
            [0, bottom_mm],
            [14_400, bottom_mm],
            [14_400, bottom_mm + 1_000],
            [0, bottom_mm + 1_000],
        ],
        "storage_mode": "storage",
        "no_stacking": False,
        "elevation_mm": 0,
        "storage_height_mm": 0,
        "allowed_inventory_types": ["finished"],
        "storage_layout": "pallet_ground",
    }


def _layout(*, revision: str, features: list[dict], marker_x: int) -> dict:
    return {
        "layout_id": "layout-4f-p1-152",
        "floor_code": "4F",
        "revision": revision,
        "bounds_mm": {
            "min_x": 0,
            "min_y": 0,
            "max_x": 20_000,
            "max_y": 8_000,
        },
        "features": [
            *features,
            {
                "id": "unrelated-functional-zone",
                "feature_kind": "zone",
                "points": [
                    [marker_x, 5_000],
                    [marker_x + 1_000, 5_000],
                    [marker_x + 1_000, 6_000],
                    [marker_x, 6_000],
                ],
            },
        ],
        "structures": [],
        "placements": [],
        "racks": [],
        "pallets": [],
    }


def _location_layout_payload(location: WarehouseLocation) -> dict:
    layout = location.floor3_layout
    assert layout is not None
    return {
        "location_id": int(location.id),
        "left_pct": float(layout.left_pct),
        "top_pct": float(layout.top_pct),
        "width_pct": float(layout.width_pct),
        "height_pct": float(layout.height_pct),
        "z_index": int(layout.z_index),
        "expected_version": int(layout.version),
        "layout_kind": layout.layout_kind,
    }


def _seed_plan(
    db,
    *,
    area: WarehouseArea,
    policy: WarehouseAreaStoragePolicy,
    feature: dict,
    floor_layout: dict,
    operator_id: int,
) -> WarehouseGroundLayoutPlan:
    plan = WarehouseGroundLayoutPlan(
        area_id=area.id,
        status="published",
        target_slot_count=SLOT_COUNT_PER_PLAN,
        numbering_origin="south",
        row_direction="from_aisle_inward",
        slot_direction="left_to_right",
        row_start_no=1,
        slot_start_no=1,
        draft_map_revision=CURRENT_REVISION,
        published_map_revision=CURRENT_REVISION,
        preview_fingerprint="0" * 64,
        version=2,
        publish_idempotency_key=f"p1-152-{area.area_code}",
        publish_request_hash="1" * 64,
        updated_by=operator_id,
        published_by=operator_id,
        published_at=revision_sync.beijing_now_naive(),
    )
    db.add(plan)
    db.flush()

    locations: list[WarehouseLocation] = []
    width_pct = (Decimal("100") / SLOT_COUNT_PER_PLAN).quantize(
        Decimal("0.0001")
    )
    for index in range(SLOT_COUNT_PER_PLAN):
        location = WarehouseLocation(
            location_code=f"4F-{area.area_code}-P01-{index + 1:02d}",
            location_name=f"四楼{area.area_code}区 第1排 {index + 1}号位",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=4,
            area_code=area.area_code,
            storage_type="ground",
            sort_order=index + 1,
            is_temporary=False,
            source_version="CURRENT_MAP",
            address_kind="legacy",
            placement_status="placed",
        )
        location.floor3_layout = Floor3LocationLayout(
            left_pct=(width_pct * index).quantize(Decimal("0.0001")),
            top_pct=Decimal("0"),
            width_pct=width_pct,
            height_pct=Decimal("100"),
            z_index=index + 1,
            version=1,
            source_type="manual",
            layout_kind="physical_pallet",
            created_by=operator_id,
            updated_by=operator_id,
        )
        db.add(location)
        locations.append(location)
    db.flush()

    measured = validate_capacity_layout_slots_for_zone(
        floor_layout,
        feature_id=policy.map_feature_id,
        slots=[_location_layout_payload(location) for location in locations],
    )
    numbered = number_ground_physical_slots(
        measured,
        numbering_origin=plan.numbering_origin,
        row_direction=plan.row_direction,
        slot_direction=plan.slot_direction,
        row_start_no=plan.row_start_no,
        slot_start_no=plan.slot_start_no,
    )
    by_id = {location.id: location for location in locations}
    for row in numbered:
        location = by_id[int(row["location_id"])]
        plan.slots.append(
            WarehouseGroundLayoutSlot(
                location_id=location.id,
                route_sequence=int(row["route_sequence"]),
                row_no=int(row["row_no"]),
                slot_no=int(row["slot_no"]),
                x_mm=Decimal(str(row["x_mm"])).quantize(Decimal("0.001")),
                y_mm=Decimal(str(row["y_mm"])).quantize(Decimal("0.001")),
                width_mm=1_200,
                depth_mm=1_000,
            )
        )
    db.flush()

    rebuilt = revision_sync._rebuild_slots(plan, floor_layout)
    revision_sync._assert_plan_geometry_matches(plan, rebuilt)
    plan.preview_fingerprint = revision_sync.ground_preview_fingerprint(
        area_id=area.id,
        policy_version=policy.version,
        map_revision=CURRENT_REVISION,
        configuration=revision_sync._configuration(plan),
        slots=rebuilt,
    )
    db.flush()
    return plan


@pytest.fixture
def revision_world(tmp_path) -> RevisionWorld:
    engine = create_sqlite_engine(tmp_path / "p1-152-ground-plan-sync.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db:
        operator = User(
            username="p1-152-admin",
            password_hash="test-only",
            role="admin",
            real_name="P1-152 管理员",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
            ui_mode="standard",
        )
        floor = WarehouseFloor(
            floor_code="4F",
            floor_name="四楼",
            floor_number=4,
            construction_status="enabled",
            planning_reference_pallet_capacity=24,
        )
        db.add_all([operator, floor])
        db.flush()
        areas = [
            WarehouseArea(
                floor_id=floor.id,
                area_code="EDIT-001",
                area_name="四楼编辑一区",
                planned_location_count=12,
                planned_pallet_capacity=12,
                construction_status="enabled",
            ),
            WarehouseArea(
                floor_id=floor.id,
                area_code="EDIT-002",
                area_name="四楼编辑二区",
                planned_location_count=12,
                planned_pallet_capacity=12,
                construction_status="enabled",
            ),
        ]
        db.add_all(areas)
        db.flush()
        features = [
            _zone_feature(
                feature_id="zone-edit-001",
                area_code=areas[0].area_code,
                area_id=areas[0].id,
                floor_id=floor.id,
                bottom_mm=0,
            ),
            _zone_feature(
                feature_id="zone-edit-002",
                area_code=areas[1].area_code,
                area_id=areas[1].id,
                floor_id=floor.id,
                bottom_mm=2_000,
            ),
        ]
        current_layout = _layout(
            revision=CURRENT_REVISION,
            features=features,
            marker_x=16_000,
        )
        target_layout = _layout(
            revision=TARGET_REVISION,
            features=deepcopy(features),
            marker_x=17_000,
        )
        policies = [
            WarehouseAreaStoragePolicy(
                area_id=area.id,
                map_feature_id=feature["id"],
                allowed_inventory_types_json=json.dumps(["finished"]),
                storage_layout="pallet_ground",
                status="published",
                draft_map_revision=CURRENT_REVISION,
                published_map_revision=CURRENT_REVISION,
                version=version,
                updated_by=operator.id,
            )
            for area, feature, version in zip(areas, features, (5, 3), strict=True)
        ]
        db.add_all(policies)
        db.flush()
        plans = [
            _seed_plan(
                db,
                area=area,
                policy=policy,
                feature=feature,
                floor_layout=current_layout,
                operator_id=operator.id,
            )
            for area, policy, feature in zip(areas, policies, features, strict=True)
        ]
        db.commit()
        db.execute(
            text(
                f"""
                CREATE TRIGGER {TRIGGER_NAME}
                BEFORE UPDATE ON warehouse_ground_layout_plans
                WHEN OLD.status = 'published'
                BEGIN
                  SELECT RAISE(ABORT, 'published ground layout plan is immutable');
                END
                """
            )
        )
        db.commit()
        return RevisionWorld(
            session_factory=session_factory,
            current_layout=current_layout,
            target_layout=target_layout,
            operator_id=operator.id,
            plan_ids=(plans[0].id, plans[1].id),
            policy_ids=(policies[0].id, policies[1].id),
        )


def _advance_policies(db, world: RevisionWorld) -> None:
    for policy_id in world.policy_ids:
        policy = db.get(WarehouseAreaStoragePolicy, policy_id)
        assert policy is not None
        policy.draft_map_revision = TARGET_REVISION
        policy.published_map_revision = TARGET_REVISION
        policy.version += 1
    db.flush()


def _trigger_sql(db) -> str:
    return str(
        db.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"
            ),
            {"name": TRIGGER_NAME},
        ).scalar_one_or_none()
        or ""
    )


def _plan_snapshot(db, world: RevisionWorld) -> list[tuple]:
    return list(
        db.execute(
            select(
                WarehouseGroundLayoutPlan.id,
                WarehouseGroundLayoutPlan.draft_map_revision,
                WarehouseGroundLayoutPlan.published_map_revision,
                WarehouseGroundLayoutPlan.preview_fingerprint,
                WarehouseGroundLayoutPlan.version,
            )
            .where(WarehouseGroundLayoutPlan.id.in_(world.plan_ids))
            .order_by(WarehouseGroundLayoutPlan.id)
        ).all()
    )


def test_unchanged_regions_sync_both_plans_and_replay_is_idempotent(
    revision_world: RevisionWorld,
) -> None:
    world = revision_world
    with world.session_factory() as db:
        before_slots = list(
            db.execute(
                select(
                    WarehouseGroundLayoutSlot.plan_id,
                    WarehouseGroundLayoutSlot.location_id,
                    WarehouseGroundLayoutSlot.route_sequence,
                    WarehouseGroundLayoutSlot.row_no,
                    WarehouseGroundLayoutSlot.slot_no,
                    WarehouseGroundLayoutSlot.x_mm,
                    WarehouseGroundLayoutSlot.y_mm,
                )
                .where(WarehouseGroundLayoutSlot.plan_id.in_(world.plan_ids))
                .order_by(
                    WarehouseGroundLayoutSlot.plan_id,
                    WarehouseGroundLayoutSlot.route_sequence,
                )
            ).all()
        )
        sync_plan = revision_sync.prepare_ground_plan_revision_sync(
            db,
            floor_code="4f",
            current_floor_layout=world.current_layout,
            proposed_floor_layout=world.target_layout,
        )
        assert len(sync_plan) == 2
        assert sum(len(row.location_ids) for row in sync_plan) == 24
        assert {row.area_code for row in sync_plan} == {"EDIT-001", "EDIT-002"}
        assert all(row.target_revision == TARGET_REVISION for row in sync_plan)
        assert all(row.target_plan_version == 3 for row in sync_plan)

        _advance_policies(db, world)
        result = revision_sync.apply_ground_plan_revision_sync(
            db,
            sync_plan=sync_plan,
            operator_id=world.operator_id,
            published_revision=TARGET_REVISION,
        )
        db.commit()

        assert len(result) == 2
        assert all(row["inventory_changed"] is False for row in result)
        assert all(row["map_geometry_changed"] is False for row in result)
        after = _plan_snapshot(db, world)
        assert all(row.draft_map_revision == TARGET_REVISION for row in after)
        assert all(row.published_map_revision == TARGET_REVISION for row in after)
        assert all(row.version == 3 for row in after)
        assert [row.preview_fingerprint for row in after] == [
            item.target_preview_fingerprint for item in sync_plan
        ]
        after_slots = list(
            db.execute(
                select(
                    WarehouseGroundLayoutSlot.plan_id,
                    WarehouseGroundLayoutSlot.location_id,
                    WarehouseGroundLayoutSlot.route_sequence,
                    WarehouseGroundLayoutSlot.row_no,
                    WarehouseGroundLayoutSlot.slot_no,
                    WarehouseGroundLayoutSlot.x_mm,
                    WarehouseGroundLayoutSlot.y_mm,
                )
                .where(WarehouseGroundLayoutSlot.plan_id.in_(world.plan_ids))
                .order_by(
                    WarehouseGroundLayoutSlot.plan_id,
                    WarehouseGroundLayoutSlot.route_sequence,
                )
            ).all()
        )
        assert after_slots == before_slots
        assert "published ground layout plan is immutable" in _trigger_sql(db)

        assert (
            revision_sync.prepare_ground_plan_revision_sync(
                db,
                floor_code="4F",
                current_floor_layout=world.target_layout,
                proposed_floor_layout=world.target_layout,
            )
            == []
        )
        assert (
            revision_sync.apply_ground_plan_revision_sync(
                db,
                sync_plan=[],
                operator_id=world.operator_id,
                published_revision=TARGET_REVISION,
            )
            == []
        )

        with pytest.raises(DatabaseError, match="published ground layout plan is immutable"):
            db.execute(
                text(
                    "UPDATE warehouse_ground_layout_plans "
                    "SET version=version + 1 WHERE id=:plan_id"
                ),
                {"plan_id": world.plan_ids[0]},
            )
        db.rollback()


def test_real_slot_geometry_drift_blocks_before_any_plan_change(
    revision_world: RevisionWorld,
) -> None:
    world = revision_world
    with world.session_factory() as db:
        before = _plan_snapshot(db, world)
        first_location_id = db.scalar(
            select(WarehouseGroundLayoutSlot.location_id)
            .where(WarehouseGroundLayoutSlot.plan_id == world.plan_ids[0])
            .order_by(WarehouseGroundLayoutSlot.route_sequence)
        )
        layout = db.scalar(
            select(Floor3LocationLayout).where(
                Floor3LocationLayout.location_id == first_location_id
            )
        )
        assert layout is not None
        layout.left_pct += Decimal("0.5000")
        db.commit()

        with pytest.raises(WarehouseGroundSlotError) as exc_info:
            revision_sync.prepare_ground_plan_revision_sync(
                db,
                floor_code="4F",
                current_floor_layout=world.current_layout,
                proposed_floor_layout=world.target_layout,
            )
        assert exc_info.value.code == "GROUND_PLAN_MAP_GEOMETRY_CHANGED"
        assert _plan_snapshot(db, world) == before
        assert "published ground layout plan is immutable" in _trigger_sql(db)


def test_feature_and_policy_drift_are_not_silently_relabelled(
    revision_world: RevisionWorld,
) -> None:
    world = revision_world
    with world.session_factory() as db:
        geometry_changed = deepcopy(world.target_layout)
        geometry_changed["features"][0]["points"][1][0] -= 1_000
        with pytest.raises(WarehouseGroundSlotError) as geometry_error:
            revision_sync.prepare_ground_plan_revision_sync(
                db,
                floor_code="4F",
                current_floor_layout=world.current_layout,
                proposed_floor_layout=geometry_changed,
            )
        assert geometry_error.value.code == "GROUND_PLAN_MAP_FEATURE_CHANGED"

        current_policy_drift = deepcopy(world.current_layout)
        proposed_policy_drift = deepcopy(world.target_layout)
        for layout in (current_policy_drift, proposed_policy_drift):
            layout["features"][0]["allowed_inventory_types"] = ["semi_finished"]
        with pytest.raises(WarehouseGroundSlotError) as policy_error:
            revision_sync.prepare_ground_plan_revision_sync(
                db,
                floor_code="4F",
                current_floor_layout=current_policy_drift,
                proposed_floor_layout=proposed_policy_drift,
            )
        assert policy_error.value.code == "GROUND_PLAN_POLICY_CHANGED"
        assert all(row.published_map_revision == CURRENT_REVISION for row in _plan_snapshot(db, world))


def test_second_plan_cas_failure_rolls_back_first_plan_and_restores_trigger(
    revision_world: RevisionWorld,
) -> None:
    world = revision_world
    with world.session_factory() as db:
        before = _plan_snapshot(db, world)
        sync_plan = revision_sync.prepare_ground_plan_revision_sync(
            db,
            floor_code="4F",
            current_floor_layout=world.current_layout,
            proposed_floor_layout=world.target_layout,
        )
        assert len(sync_plan) == 2
        _advance_policies(db, world)
        stale_second = replace(
            sync_plan[1], source_plan_version=sync_plan[1].source_plan_version + 99
        )

        with pytest.raises(WarehouseGroundSlotError) as exc_info:
            revision_sync.apply_ground_plan_revision_sync(
                db,
                sync_plan=[sync_plan[0], stale_second],
                operator_id=world.operator_id,
                published_revision=TARGET_REVISION,
            )
        assert exc_info.value.code == "GROUND_PLAN_REVISION_CAS_FAILED"
        # The API owns the transaction. Its rollback must undo the first plan,
        # policy revisions and the transactional trigger drop as one unit.
        db.rollback()

        assert _plan_snapshot(db, world) == before
        policies = [db.get(WarehouseAreaStoragePolicy, row_id) for row_id in world.policy_ids]
        assert all(policy is not None for policy in policies)
        assert all(policy.published_map_revision == CURRENT_REVISION for policy in policies)
        assert [policy.version for policy in policies] == [5, 3]
        assert "published ground layout plan is immutable" in _trigger_sql(db)


class _PublishDb:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def commit(self) -> None:
        self.calls.append("commit")

    def rollback(self) -> None:
        self.calls.append("rollback")


def _stub_publish_dependencies(monkeypatch, calls: list[str]) -> None:
    monkeypatch.setattr(
        warehouse_api,
        "_apply_archived_area_tombstones_before_validation",
        lambda *_args, **_kwargs: (TARGET_REVISION, None),
    )
    monkeypatch.setattr(
        warehouse_api,
        "_formal_area_publish_blockers",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        warehouse_api,
        "snapshot_warehouse_twin_publish_state",
        lambda: calls.append("snapshot") or "snapshot-token",
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_published_warehouse_twin_floor_for_edit",
        lambda _floor_code: {"revision": CURRENT_REVISION},
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_layout_draft",
        lambda _floor_code: {"revision": TARGET_REVISION},
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_floor",
        lambda _floor_code: {"revision": TARGET_REVISION, "features": []},
    )
    monkeypatch.setattr(
        warehouse_api,
        "prepare_ground_plan_revision_sync",
        lambda *_args, **_kwargs: calls.append("prepare") or ["sync-plan"],
    )
    monkeypatch.setattr(
        warehouse_api,
        "publish_warehouse_twin_layout_draft",
        lambda *_args, **_kwargs: calls.append("publish-map")
        or SimpleNamespace(
            applied=True,
            value={
                "published_revision": TARGET_REVISION,
                "backup_name": "p1-152-map-backup.json",
            },
        ),
    )
    monkeypatch.setattr(
        warehouse_api,
        "publish_floor_area_policies",
        lambda *_args, **_kwargs: calls.append("publish-policies") or [],
    )
    monkeypatch.setattr(
        warehouse_api,
        "sync_published_rack_cells",
        lambda *_args, **_kwargs: calls.append("sync-racks")
        or SimpleNamespace(
            created_location_ids=(),
            enabled_location_ids=(),
            disabled_location_ids=(),
            updated_location_ids=(),
            bound_legacy_location_ids=(),
        ),
    )
    monkeypatch.setattr(
        warehouse_api,
        "_validate_published_area_layouts_for_floor",
        lambda *_args, **_kwargs: calls.append("validate-published"),
    )
    monkeypatch.setattr(
        warehouse_api,
        "append_audit_event",
        lambda *_args, **_kwargs: calls.append("audit-plan-sync"),
    )
    monkeypatch.setattr(
        warehouse_api,
        "_twin_layout_asset_log",
        lambda *_args, **_kwargs: calls.append("audit-map-publish"),
    )


def _publish_payload() -> warehouse_api.TwinLayoutDraftPublishPayload:
    return warehouse_api.TwinLayoutDraftPublishPayload(
        expected_published_revision=CURRENT_REVISION,
        expected_draft_revision=TARGET_REVISION,
        operation_key="p1-152-api-publish",
    )


def test_api_publish_prepares_before_map_and_applies_after_policy_publish(
    monkeypatch,
) -> None:
    calls: list[str] = []
    _stub_publish_dependencies(monkeypatch, calls)

    def apply(*_args, **kwargs):
        assert kwargs["sync_plan"] == ["sync-plan"]
        assert kwargs["published_revision"] == TARGET_REVISION
        calls.append("apply-plan-sync")
        return [
            {
                "plan_id": 22,
                "area_code": "EDIT-001",
                "inventory_changed": False,
                "map_geometry_changed": False,
            }
        ]

    monkeypatch.setattr(
        warehouse_api,
        "apply_ground_plan_revision_sync",
        apply,
    )
    result = warehouse_api._publish_twin_layout_draft_locked(
        floor_code="4F",
        payload=_publish_payload(),
        request=None,
        db=_PublishDb(calls),
        user=SimpleNamespace(id=152),
        floor_projection_claimed=True,
    )

    assert result["applied"] is True
    assert result["ground_plan_revision_sync_count"] == 1
    assert calls.index("prepare") < calls.index("publish-map")
    assert calls.index("publish-map") < calls.index("publish-policies")
    assert calls.index("publish-policies") < calls.index("apply-plan-sync")
    assert calls.index("apply-plan-sync") < calls.index("sync-racks")
    assert calls[-1] == "commit"
    assert "rollback" not in calls


def test_api_publish_sync_failure_restores_map_then_rolls_back_database(
    monkeypatch,
) -> None:
    calls: list[str] = []
    _stub_publish_dependencies(monkeypatch, calls)

    def fail_sync(*_args, **_kwargs):
        calls.append("apply-plan-sync")
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_REVISION_CAS_FAILED",
            "second plan changed",
        )

    def restore(snapshot, *, backup_name=None):
        assert snapshot == "snapshot-token"
        assert backup_name == "p1-152-map-backup.json"
        calls.append("restore-map")

    monkeypatch.setattr(
        warehouse_api,
        "apply_ground_plan_revision_sync",
        fail_sync,
    )
    monkeypatch.setattr(
        warehouse_api,
        "restore_warehouse_twin_publish_state",
        restore,
    )

    with pytest.raises(HTTPException) as exc_info:
        warehouse_api._publish_twin_layout_draft_locked(
            floor_code="4F",
            payload=_publish_payload(),
            request=None,
            db=_PublishDb(calls),
            user=SimpleNamespace(id=152),
            floor_projection_claimed=True,
        )
    assert exc_info.value.status_code == 409
    assert calls.index("prepare") < calls.index("publish-map")
    assert calls.index("publish-map") < calls.index("publish-policies")
    assert calls.index("publish-policies") < calls.index("apply-plan-sync")
    assert calls[-2:] == ["restore-map", "rollback"]
    assert "commit" not in calls


def test_manual_ground_plan_publish_rebinds_fingerprint_to_incremented_policy(
    revision_world: RevisionWorld,
    monkeypatch,
) -> None:
    """A freshly published plan must be valid at the post-publish policy version."""

    world = revision_world
    plan_id = world.plan_ids[0]
    with world.session_factory() as setup_db:
        setup_db.execute(text(f'DROP TRIGGER "{TRIGGER_NAME}"'))
        plan = setup_db.get(WarehouseGroundLayoutPlan, plan_id)
        policy = setup_db.get(WarehouseAreaStoragePolicy, world.policy_ids[0])
        assert plan is not None and policy is not None
        area = setup_db.get(WarehouseArea, plan.area_id)
        assert area is not None
        area_id = area.id
        floor_id = area.floor_id
        preview_slots = revision_sync._rebuild_slots(plan, world.current_layout)
        initial_fingerprint = revision_sync.ground_preview_fingerprint(
            area_id=plan.area_id,
            policy_version=policy.version,
            map_revision=CURRENT_REVISION,
            configuration=revision_sync._configuration(plan),
            slots=preview_slots,
        )
        setup_db.execute(
            delete(WarehouseGroundLayoutSlot).where(
                WarehouseGroundLayoutSlot.plan_id == plan_id
            )
        )
        plan.status = "draft"
        plan.published_map_revision = None
        plan.publish_idempotency_key = None
        plan.publish_request_hash = None
        plan.published_by = None
        plan.published_at = None
        plan.preview_fingerprint = initial_fingerprint
        setup_db.commit()

    monkeypatch.setattr(
        warehouse_api,
        "_claim_floor_projection_for_layout_write",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        warehouse_api,
        "_ground_layout_context",
        lambda db, **_kwargs: (
            db.get(WarehouseFloor, floor_id),
            db.get(WarehouseArea, area_id),
            db.get(WarehouseAreaStoragePolicy, world.policy_ids[0]),
        ),
    )
    monkeypatch.setattr(
        warehouse_api,
        "load_warehouse_twin_floor",
        lambda _floor_code: world.current_layout,
    )
    monkeypatch.setattr(
        warehouse_api,
        "_ground_preview_for_plan",
        lambda *_args, **_kwargs: (preview_slots, initial_fingerprint),
    )
    monkeypatch.setattr(
        warehouse_api,
        "append_audit_event",
        lambda *_args, **_kwargs: None,
    )

    with world.session_factory() as db:
        result = warehouse_api.publish_ground_layout(
            "4F",
            "EDIT-001",
            warehouse_api.GroundLayoutPublishPayload(
                expected_plan_version=2,
                preview_fingerprint=initial_fingerprint,
                idempotency_key="p1-152-manual-publish",
            ),
            request=None,
            db=db,
            user=db.get(User, world.operator_id),
        )

        plan = db.get(WarehouseGroundLayoutPlan, plan_id)
        policy = db.get(WarehouseAreaStoragePolicy, world.policy_ids[0])
        assert plan is not None and policy is not None
        assert policy.version == 6
        expected_final_fingerprint = revision_sync.ground_preview_fingerprint(
            area_id=plan.area_id,
            policy_version=policy.version,
            map_revision=CURRENT_REVISION,
            configuration=revision_sync._configuration(plan),
            slots=preview_slots,
        )
        assert initial_fingerprint != expected_final_fingerprint
        assert plan.preview_fingerprint == expected_final_fingerprint
        assert result["preview_fingerprint"] == expected_final_fingerprint
