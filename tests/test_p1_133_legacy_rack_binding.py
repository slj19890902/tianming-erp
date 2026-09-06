from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api.warehouse import TwinLayoutDraftPublishPayload
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryPallet,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.warehouse_rack_cells import (
    WarehouseRackCellSyncError,
    preview_legacy_rack_cell_bindings,
    sync_published_rack_cells,
)


ROOT = Path(__file__).resolve().parents[1]


def _layout(*, west_counts: list[int] | None = None) -> dict:
    counts = west_counts or [4, 8]
    return {
        "floor_code": "3F",
        "revision": "p1-133-bind-rev-1",
        "bounds_mm": {
            "min_x": 0,
            "min_y": 0,
            "max_x": 10000,
            "max_y": 10000,
        },
        "features": [
            {
                "id": "zone-3f-f1",
                "feature_code": "ZONE-3F-F1",
                "feature_kind": "zone",
                "erp_area_code": "F1",
                "formal_area_id": 1,
                "formal_floor_id": 1,
            }
        ],
        "racks": [
            {
                "id": "rack-f1-east",
                "rack_code": "RACK-3F-F1-EAST",
                "name": "F1东排货架",
                "area_feature_id": "zone-3f-f1",
                "x_mm": 3000,
                "y_mm": 5000,
                "width_mm": 3000,
                "depth_mm": 1000,
                "rotation_deg": 0,
                "levels": 2,
                "level_cell_counts": [4, 8],
            },
            {
                "id": "rack-f1-west",
                "rack_code": "RACK-3F-F1-WEST",
                "name": "F1西排货架",
                "area_feature_id": "zone-3f-f1",
                "x_mm": 7000,
                "y_mm": 5000,
                "width_mm": 3000,
                "depth_mm": 1000,
                "rotation_deg": 0,
                "levels": 2,
                "level_cell_counts": counts,
            },
        ],
    }


@pytest.fixture()
def legacy_rack_factory(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "p1-133-legacy-rack.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="p1-133-rack-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="货架绑定管理员",
            must_change_password=False,
        )
        floor = WarehouseFloor(
            id=1,
            floor_code="3F",
            floor_name="三楼成品仓",
            floor_number=3,
            construction_status="enabled",
            planning_reference_pallet_capacity=0,
        )
        db.add_all([user, floor])
        db.flush()
        area = WarehouseArea(
            id=1,
            floor_id=floor.id,
            area_code="F1",
            area_name="F1 两排货架区",
            planned_location_count=12,
            planned_pallet_capacity=0,
            construction_status="enabled",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        db.add(area)
        db.flush()
        area.storage_policy = WarehouseAreaStoragePolicy(
            map_feature_id="zone-3f-f1",
            allowed_inventory_types_json=json.dumps(["finished"]),
            storage_layout="rack",
            status="published",
            draft_map_revision="p1-133-bind-rev-1",
            published_map_revision="p1-133-bind-rev-1",
            version=1,
            updated_by=user.id,
        )
        locations = []
        for level_no, slot_count in enumerate((4, 8), start=1):
            for slot_no in range(1, slot_count + 1):
                locations.append(
                    WarehouseLocation(
                        location_code=f"F1-S{level_no}-{slot_no:02d}",
                        location_name=f"F1-S{level_no}-{slot_no:02d}",
                        warehouse_type="finished",
                        is_active=True,
                        warehouse_floor=3,
                        area_code="F1",
                        storage_type="rack",
                        level_no=level_no,
                        sort_order=level_no * 100 + slot_no,
                        is_temporary=False,
                        source_version="CURRENT_MAP",
                        address_kind="rack_slot",
                        address_area_id=area.id,
                        rack_code="A",
                        slot_no=slot_no,
                        address_version=1,
                        placement_status="placed",
                    )
                )
        db.add_all(locations)
        db.flush()
        occupied = next(row for row in locations if row.location_code == "F1-S2-06")
        db.add(
            InventoryPallet(
                pallet_code="P1-133-F1-S2-06",
                location_id=occupied.id,
                status="active",
                is_current=True,
                version=1,
            )
        )
        db.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def test_preview_is_zero_write_and_ambiguous_racks_are_never_guessed(
    legacy_rack_factory,
) -> None:
    with legacy_rack_factory() as db:
        before = [
            (row.id, row.map_rack_id, row.address_version)
            for row in db.scalars(select(WarehouseLocation).order_by(WarehouseLocation.id))
        ]
        preview = preview_legacy_rack_cell_bindings(db, floor_layout=_layout())
        after = [
            (row.id, row.map_rack_id, row.address_version)
            for row in db.scalars(select(WarehouseLocation).order_by(WarehouseLocation.id))
        ]
        assert before == after
        assert preview["requires_confirmation"] is True
        assert preview["unresolved_count"] == 0
        assert len(preview["groups"]) == 1
        group = preview["groups"][0]
        assert group["binding_key"] == "1:A"
        assert group["location_count"] == 12
        assert group["occupied_location_count"] == 1
        assert group["suggested_map_rack_id"] is None
        assert {item["map_rack_id"] for item in group["candidates"]} == {
            "rack-f1-east",
            "rack-f1-west",
        }


def test_publish_confirmation_reuses_real_location_ids_and_keeps_inventory(
    legacy_rack_factory,
) -> None:
    with legacy_rack_factory() as db:
        original = list(
            db.scalars(
                select(WarehouseLocation).order_by(
                    WarehouseLocation.level_no, WarehouseLocation.slot_no
                )
            )
        )
        original_ids = [row.id for row in original]
        original_codes = [row.location_code for row in original]
        occupied = next(row for row in original if row.location_code == "F1-S2-06")
        preview = preview_legacy_rack_cell_bindings(db, floor_layout=_layout())

        with pytest.raises(WarehouseRackCellSyncError, match="有货旧货位"):
            sync_published_rack_cells(db, floor_layout=_layout(), operator_id=1)
        db.rollback()

        result = sync_published_rack_cells(
            db,
            floor_layout=_layout(),
            operator_id=1,
            expected_legacy_binding_fingerprint=preview["fingerprint"],
            confirmed_legacy_bindings=[
                {"binding_key": "1:A", "map_rack_id": "rack-f1-west"}
            ],
        )
        db.commit()

        rebound = list(
            db.scalars(
                select(WarehouseLocation)
                .where(WarehouseLocation.map_rack_id == "rack-f1-west")
                .order_by(WarehouseLocation.level_no, WarehouseLocation.slot_no)
            )
        )
        assert [row.id for row in rebound] == original_ids
        assert [row.location_code for row in rebound] == original_codes
        assert {row.address_version for row in rebound} == {2}
        assert result.bound_legacy_location_ids == tuple(original_ids)
        pallet = db.scalar(
            select(InventoryPallet).where(
                InventoryPallet.pallet_code == "P1-133-F1-S2-06"
            )
        )
        assert pallet.location_id == occupied.id
        assert db.get(WarehouseLocation, occupied.id).map_rack_id == "rack-f1-west"

        replay = sync_published_rack_cells(
            db,
            floor_layout=_layout(),
            operator_id=1,
            expected_legacy_binding_fingerprint=preview["fingerprint"],
            confirmed_legacy_bindings=[
                {"binding_key": "1:A", "map_rack_id": "rack-f1-west"}
            ],
        )
        db.commit()
        assert replay.bound_legacy_location_ids == ()
        assert db.get(InventoryPallet, pallet.id).location_id == occupied.id
        assert {
            row.address_version
            for row in db.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.map_rack_id == "rack-f1-west"
                )
            )
        } == {2}


def test_empty_third_floor_f_cells_retire_before_new_racks_are_created(
    legacy_rack_factory,
) -> None:
    with legacy_rack_factory() as db:
        pallet = db.scalar(
            select(InventoryPallet).where(
                InventoryPallet.pallet_code == "P1-133-F1-S2-06"
            )
        )
        pallet.is_current = False
        pallet.status = "closed"
        db.commit()

        old_rows = list(
            db.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.map_rack_id.is_(None),
                    WarehouseLocation.address_kind == "rack_slot",
                )
            )
        )
        old_ids = {row.id for row in old_rows}
        result = sync_published_rack_cells(
            db, floor_layout=_layout(), operator_id=1
        )
        db.commit()

        assert old_ids == set(result.disabled_location_ids)
        assert all(
            db.get(WarehouseLocation, row_id).is_active is False
            for row_id in old_ids
        )
        assert len(result.created_location_ids) == 24
        assert len(
            db.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.map_rack_id.is_not(None),
                    WarehouseLocation.is_active.is_(True),
                )
            ).all()
        ) == 24


def test_out_of_range_or_stale_binding_is_fail_closed(legacy_rack_factory) -> None:
    with legacy_rack_factory() as db:
        too_small = _layout(west_counts=[4, 5])
        preview = preview_legacy_rack_cell_bindings(db, floor_layout=too_small)
        group = preview["groups"][0]
        assert {item["map_rack_id"] for item in group["candidates"]} == {
            "rack-f1-east"
        }
        with pytest.raises(WarehouseRackCellSyncError, match="不再适用"):
            sync_published_rack_cells(
                db,
                floor_layout=too_small,
                operator_id=1,
                expected_legacy_binding_fingerprint=preview["fingerprint"],
                confirmed_legacy_bindings=[
                    {"binding_key": "1:A", "map_rack_id": "rack-f1-west"}
                ],
            )
        db.rollback()

        current = preview_legacy_rack_cell_bindings(db, floor_layout=_layout())
        row = db.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "F1-S2-06"
            )
        )
        row.address_version += 1
        db.flush()
        with pytest.raises(WarehouseRackCellSyncError, match="预览已变化"):
            sync_published_rack_cells(
                db,
                floor_layout=_layout(),
                operator_id=1,
                expected_legacy_binding_fingerprint=current["fingerprint"],
                confirmed_legacy_bindings=[
                    {"binding_key": "1:A", "map_rack_id": "rack-f1-west"}
                ],
            )


def test_publish_payload_requires_explicit_admin_confirmation() -> None:
    with pytest.raises(ValidationError, match="必须由管理员明确确认"):
        TwinLayoutDraftPublishPayload(
            expected_published_revision="published-1",
            expected_draft_revision="draft-1",
            operation_key="p1-133-binding-publish",
            legacy_rack_binding_fingerprint="a" * 64,
            legacy_rack_bindings=[
                {"binding_key": "1:A", "map_rack_id": "rack-f1-west"}
            ],
        )


def test_region_planning_reuses_publish_action_for_legacy_binding() -> None:
    source = (ROOT / "factory_twin/frontend/src/WarehouseTwinApp.tsx").read_text(
        encoding="utf-8"
    )
    assert "旧货位对应当前货架" in source
    assert "legacy_rack_bindings_confirmed: true" in source
    assert "再次点“完成并应用”" in source
    assert 'priorSelections[group.binding_key] || ""' in source
    assert "priorSelections[group.binding_key] || group.suggested_map_rack_id" not in source
    assert "legacy_rack_code}架 →" in source
    assert "绑定旧货位" not in source
