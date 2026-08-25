from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLocationMovement,
    InventoryPallet,
    WarehouseArea,
    WarehouseFloor,
    WarehouseLocation,
    WarehouseLocationAddressMutation,
    WarehouseLocationAlias,
)
from app.api.warehouse import (
    WarehouseAddressConfirmPayload,
    confirm_warehouse_location_address_change,
)
from app.services.warehouse_twin_dashboard import inventory_search_matches
from app.services.warehouse_location_address import (
    AddressChangeCommand,
    WarehouseLocationAddressError,
    build_address_change_preview,
    confirm_address_change,
    employee_location_name,
    format_location_address,
    location_address_payload,
    location_alias_conflict,
    resolve_location_address,
)


@pytest.fixture
def session_factory(isolated_engine):
    Base.metadata.create_all(isolated_engine)
    return sessionmaker(bind=isolated_engine, expire_on_commit=False)


def _seed_floor_area_location(
    db: Session,
    *,
    floor_number: int,
    area_code: str,
    location_code: str,
) -> tuple[WarehouseFloor, WarehouseArea, WarehouseLocation]:
    floor = WarehouseFloor(
        floor_code=f"{floor_number}F",
        floor_name={1: "一楼", 3: "三楼"}.get(floor_number, f"{floor_number}楼"),
        floor_number=floor_number,
        construction_status="enabled",
    )
    db.add(floor)
    db.flush()
    area = WarehouseArea(
        floor_id=floor.id,
        area_code=area_code,
        area_name=f"{floor.floor_name}测试区",
        construction_status="enabled",
    )
    db.add(area)
    db.flush()
    location = WarehouseLocation(
        location_code=location_code,
        location_name=f"旧位置 {location_code}",
        warehouse_type="finished",
        warehouse_floor=floor_number,
        area_code=area_code,
        storage_type="rack",
        placement_status="placed",
        source_version="AREA-V1",
    )
    db.add(location)
    db.commit()
    return floor, area, location


def _confirm(
    db: Session,
    command: AddressChangeCommand,
    *,
    key: str,
    actor: int = 1,
) -> dict:
    preview = build_address_change_preview(db, command)
    result, replayed = confirm_address_change(
        db,
        command,
        preview_fingerprint=preview["preview_fingerprint"],
        idempotency_key=key,
        actor_user_id=actor,
    )
    assert replayed is False
    db.commit()
    return result


def _assign_area(
    db: Session,
    area: WarehouseArea,
    *,
    zone: str,
    subzone: int,
    key: str,
) -> None:
    _confirm(
        db,
        AddressChangeCommand(
            action_kind="area",
            area_id=area.id,
            new_zone_code=zone,
            new_subzone_no=subzone,
        ),
        key=key,
    )


def _assign_rack_location(
    db: Session,
    area: WarehouseArea,
    location: WarehouseLocation,
    *,
    rack: str = "A",
    level: int = 2,
    slot: int = 3,
    key: str,
) -> dict:
    return _confirm(
        db,
        AddressChangeCommand(
            action_kind="location",
            area_id=area.id,
            location_id=location.id,
            new_address_kind="rack_slot",
            new_rack_code=rack,
            new_level_no=level,
            new_slot_no=slot,
        ),
        key=key,
    )


@pytest.mark.parametrize(
    ("location_code", "storage_type", "level_no", "side_code", "expected_name"),
    [
        ("A1-R04", "ground", None, "R", "三楼 A1成品存放区·右侧第4位"),
        ("F2-S3-L05", "rack", 3, "L", "三楼 F2成品货架区·3层·左侧第5格"),
        ("F12-P01", "temporary_aisle", None, "P", "三楼 F12过道临放区·临放第1位"),
    ],
)
def test_v11_measured_map_location_uses_one_employee_projection(
    location_code: str,
    storage_type: str,
    level_no: int | None,
    side_code: str | None,
    expected_name: str,
) -> None:
    floor = WarehouseFloor(
        id=3,
        floor_code="3F",
        floor_name="三楼成品仓",
        floor_number=3,
        construction_status="enabled",
    )
    area_name = {
        "A1": "A1成品存放区",
        "F2": "F2成品货架区",
        "F12": "F12过道临放区",
    }[location_code.split("-", 1)[0]]
    area = WarehouseArea(
        id=31,
        floor_id=floor.id,
        area_code=location_code.split("-", 1)[0],
        area_name=area_name,
        construction_status="enabled",
    )
    location = WarehouseLocation(
        id=301,
        location_code=location_code,
        location_name=location_code,
        warehouse_type="finished",
        warehouse_floor=3,
        area_code=area.area_code,
        storage_type=storage_type,
        level_no=level_no,
        side_code=side_code,
        source_version="V11",
        placement_status="placed",
    )

    current_code, current_name = format_location_address(
        location,
        area=area,
        floor=floor,
    )
    payload = location_address_payload(location, area=area, floor=floor)
    published_payload = location_address_payload(
        location,
        area=area,
        floor=floor,
        position_status="mapped",
    )

    assert current_code == location_code
    assert current_name == expected_name
    assert employee_location_name(location, area=area, floor=floor) == expected_name
    assert payload["current_address_name"] == expected_name
    assert payload["employee_location_name"] == expected_name
    assert payload["projection_source"] == "measured_map_name_unpublished"
    assert published_payload["projection_source"] == "published_measured_map"


def test_address_lookup_never_treats_null_placement_as_published(
    session_factory,
) -> None:
    with session_factory() as db:
        _floor, _area, location = _seed_floor_area_location(
            db,
            floor_number=3,
            area_code="A1",
            location_code="NULL-PLACEMENT-01",
        )
        location.placement_status = None
        db.commit()

        result = resolve_location_address(db, "NULL-PLACEMENT-01")

    assert result["placement_status"] == "unplaced"
    assert result["current"]["projection_source"] != "published_measured_map"


def test_floor_scoped_paths_keep_same_short_rack_address_distinct(session_factory) -> None:
    with session_factory() as db:
        _floor1, area1, location1 = _seed_floor_area_location(
            db,
            floor_number=1,
            area_code="OLD-A",
            location_code="LEGACY-1F-A",
        )
        _floor3, area3, location3 = _seed_floor_area_location(
            db,
            floor_number=3,
            area_code="OLD-A3",
            location_code="LEGACY-3F-A",
        )
        _assign_area(db, area1, zone="A", subzone=1, key="p186-area-1f-a01")
        _assign_area(db, area3, zone="A", subzone=1, key="p186-area-3f-a01")
        _assign_rack_location(db, area1, location1, key="p186-loc-1f-a")
        _assign_rack_location(db, area3, location3, key="p186-loc-3f-a")

        db.refresh(location1)
        db.refresh(location3)
        assert location1.location_code == "1F-A01-A-02-03"
        assert location3.location_code == "3F-A01-A-02-03"
        assert location1.location_name == "一楼 A1区·A架·2层·3格"
        assert location3.location_name == "三楼 A1区·A架·2层·3格"
        assert location1.id != location3.id
        assert db.scalar(select(func.count(WarehouseLocationAlias.id))) == 4


def test_old_code_resolves_same_stable_location_and_cannot_be_reused(session_factory) -> None:
    with session_factory() as db:
        _floor, area, location = _seed_floor_area_location(
            db,
            floor_number=3,
            area_code="D2-OLD",
            location_code="F2-S3-R05",
        )
        stable_id = location.id
        _assign_area(db, area, zone="D", subzone=2, key="p186-area-d02")
        _assign_rack_location(db, area, location, key="p186-location-d02")

        old_lookup = resolve_location_address(db, "f2-s3-r05")
        old_name_lookup = resolve_location_address(db, "旧位置 f2-s3-r05")
        current_lookup = resolve_location_address(db, "3F-D02-A-02-03")
        chinese_lookup = resolve_location_address(db, "三楼 d2区·a架·2层·3格")
        assert old_lookup["location_id"] == stable_id
        assert old_name_lookup["location_id"] == stable_id
        assert current_lookup["location_id"] == stable_id
        assert chinese_lookup["location_id"] == stable_id
        assert old_lookup["matched_by"] == "legacy_alias"
        assert old_name_lookup["matched_by"] == "legacy_alias"
        assert old_lookup["current"]["current_address_name"] == "三楼 D2区·A架·2层·3格"
        assert location_alias_conflict(db, "F2-S3-R05", location_id=999) is True
        assert location_alias_conflict(db, "F2-S3-R05", location_id=stable_id) is False


def test_current_lookup_and_conflicts_use_the_same_nfkc_whitespace_contract(
    session_factory,
) -> None:
    with session_factory() as db:
        _floor, area, location = _seed_floor_area_location(
            db,
            floor_number=3,
            area_code="D2-OLD",
            location_code="  mixed   code  ",
        )
        assert resolve_location_address(db, "ＭＩＸＥＤ code")["location_id"] == location.id

        competing = WarehouseLocation(
            location_code="OTHER-P186",
            location_name="三楼   D2区·A架·2层·3格",
            warehouse_type="finished",
            warehouse_floor=3,
            area_code="OTHER",
            storage_type="rack",
            placement_status="placed",
            source_version="AREA-V1",
        )
        db.add(competing)
        db.commit()
        _assign_area(db, area, zone="D", subzone=2, key="p186-area-normalized")
        with pytest.raises(WarehouseLocationAddressError) as conflict:
            _assign_rack_location(
                db,
                area,
                location,
                key="p186-location-normalized-conflict",
            )
        assert conflict.value.code == "WAREHOUSE_ADDRESS_CURRENT_CONFLICT"


def test_rack_rename_preserves_pallet_map_and_movement_identity(session_factory) -> None:
    with session_factory() as db:
        _floor, area, location = _seed_floor_area_location(
            db,
            floor_number=3,
            area_code="D2-OLD",
            location_code="1F-D-001",
        )
        _assign_area(db, area, zone="D", subzone=2, key="p186-area-rack")
        _assign_rack_location(db, area, location, key="p186-location-rack")
        pallet = InventoryPallet(
            pallet_code="PALLET-P186-001",
            location_id=location.id,
            is_current=True,
            status="active",
        )
        layout = Floor3LocationLayout(
            location_id=location.id,
            left_pct=Decimal("10"),
            top_pct=Decimal("10"),
            width_pct=Decimal("5"),
            height_pct=Decimal("5"),
            version=1,
            source_type="manual",
            layout_kind="logical_anchor",
        )
        db.add_all([pallet, layout])
        db.flush()
        movement = InventoryLocationMovement(
            pallet_id=pallet.id,
            from_location_id=None,
            to_location_id=location.id,
            movement_type="create",
            idempotency_key="p186-existing-movement",
        )
        db.add(movement)
        db.commit()
        stable_location_id = location.id
        stable_pallet_id = pallet.id
        stable_layout_id = layout.id
        movement_count = int(db.scalar(select(func.count(InventoryLocationMovement.id))) or 0)

        command = AddressChangeCommand(
            action_kind="rack",
            area_id=area.id,
            current_rack_code="A",
            new_rack_code="B",
        )
        preview = build_address_change_preview(db, command)
        assert preview["impacts"]["current_pallets"] == 1
        assert preview["impacts"]["inventory_movements"] == 1
        assert preview["writes_inventory"] is False
        assert preview["writes_movements"] is False
        result, replayed = confirm_address_change(
            db,
            command,
            preview_fingerprint=preview["preview_fingerprint"],
            idempotency_key="p186-rack-a-to-b",
            actor_user_id=1,
        )
        assert replayed is False
        db.commit()
        db.refresh(location)
        db.refresh(pallet)
        db.refresh(layout)
        assert location.id == stable_location_id
        assert location.location_code == "3F-D02-B-02-03"
        assert pallet.id == stable_pallet_id
        assert pallet.location_id == stable_location_id
        assert layout.id == stable_layout_id
        assert layout.location_id == stable_location_id
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == movement_count
        assert result["stable_location_ids"] == [stable_location_id]
        assert resolve_location_address(db, "3F-D02-A-02-03")["location_id"] == stable_location_id


def test_confirm_replays_same_actor_payload_and_rejects_stale_or_changed_payload(session_factory) -> None:
    with session_factory() as db:
        _floor, area, location = _seed_floor_area_location(
            db,
            floor_number=3,
            area_code="D2-OLD",
            location_code="LEGACY-D02",
        )
        _assign_area(db, area, zone="D", subzone=2, key="p186-area-idem")
        command = AddressChangeCommand(
            action_kind="location",
            area_id=area.id,
            location_id=location.id,
            new_address_kind="rack_slot",
            new_rack_code="A",
            new_level_no=1,
            new_slot_no=1,
        )
        preview = build_address_change_preview(db, command)
        first, replayed = confirm_address_change(
            db,
            command,
            preview_fingerprint=preview["preview_fingerprint"],
            idempotency_key="p186-location-idempotency",
            actor_user_id=1,
        )
        assert replayed is False
        db.commit()
        replay, replayed = confirm_address_change(
            db,
            command,
            preview_fingerprint=preview["preview_fingerprint"],
            idempotency_key="p186-location-idempotency",
            actor_user_id=1,
        )
        assert replayed is True
        assert replay["stable_location_ids"] == first["stable_location_ids"]
        assert db.scalar(select(func.count(WarehouseLocationAddressMutation.id))) == 2

        changed = AddressChangeCommand(
            action_kind="location",
            area_id=area.id,
            location_id=location.id,
            new_address_kind="rack_slot",
            new_rack_code="A",
            new_level_no=1,
            new_slot_no=2,
        )
        with pytest.raises(WarehouseLocationAddressError) as changed_error:
            confirm_address_change(
                db,
                changed,
                preview_fingerprint=preview["preview_fingerprint"],
                idempotency_key="p186-location-idempotency",
                actor_user_id=1,
            )
        assert changed_error.value.code == "WAREHOUSE_ADDRESS_IDEMPOTENCY_CONFLICT"

        stale_preview = build_address_change_preview(db, changed)
        location.address_version += 1
        db.commit()
        with pytest.raises(WarehouseLocationAddressError) as stale_error:
            confirm_address_change(
                db,
                changed,
                preview_fingerprint=stale_preview["preview_fingerprint"],
                idempotency_key="p186-stale-attempt",
                actor_user_id=1,
            )
        assert stale_error.value.code == "WAREHOUSE_ADDRESS_STALE"


def test_preview_and_transaction_rollback_write_nothing(session_factory) -> None:
    with session_factory() as db:
        _floor, area, location = _seed_floor_area_location(
            db,
            floor_number=1,
            area_code="A-OLD",
            location_code="OLD-A-01",
        )
        command = AddressChangeCommand(
            action_kind="area",
            area_id=area.id,
            new_zone_code="A",
            new_subzone_no=1,
        )
        before = {
            "aliases": int(db.scalar(select(func.count(WarehouseLocationAlias.id))) or 0),
            "mutations": int(
                db.scalar(select(func.count(WarehouseLocationAddressMutation.id))) or 0
            ),
            "movements": int(
                db.scalar(select(func.count(InventoryLocationMovement.id))) or 0
            ),
        }
        preview = build_address_change_preview(db, command)
        assert area.address_zone_code is None
        assert location.address_area_id is None
        assert before["aliases"] == 0
        assert before["mutations"] == 0

        confirm_address_change(
            db,
            command,
            preview_fingerprint=preview["preview_fingerprint"],
            idempotency_key="p186-rollback-attempt",
            actor_user_id=1,
        )
        db.rollback()
        db.refresh(area)
        db.refresh(location)
        assert area.address_zone_code is None
        assert location.address_area_id is None
        assert db.scalar(select(func.count(WarehouseLocationAlias.id))) == 0
        assert db.scalar(select(func.count(WarehouseLocationAddressMutation.id))) == 0
        assert db.scalar(select(func.count(InventoryLocationMovement.id))) == before["movements"]


def test_inventory_search_accepts_old_alias_and_current_chinese_name(session_factory) -> None:
    with session_factory() as db:
        _floor, area, location = _seed_floor_area_location(
            db,
            floor_number=3,
            area_code="D2-OLD",
            location_code="OLD-PRINTED-LABEL",
        )
        _assign_area(db, area, zone="D", subzone=2, key="p186-search-area")
        _assign_rack_location(db, area, location, key="p186-search-location")
        db.refresh(location)
        assert location.address_aliases
        lot = SimpleNamespace(
            id=1,
            lot_number="LOT-P186",
            inventory_type="finished",
            quantity_available=10,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            stock_date=date.today(),
            stock_date_accuracy="exact",
            status="active",
            version=1,
            finished_detail=None,
            semi_finished_detail=None,
            pallet_item=None,
            location=location,
        )
        assert inventory_search_matches(lot, "old-printed-label", date.today()) is True
        assert inventory_search_matches(lot, "三楼 D2区·A架·2层·3格", date.today()) is True


def test_api_audit_failure_rolls_back_address_alias_and_mutation(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session_factory() as db:
        _floor, area, location = _seed_floor_area_location(
            db,
            floor_number=1,
            area_code="A-OLD",
            location_code="OLD-AUDIT-ADDRESS",
        )
        command = AddressChangeCommand(
            action_kind="area",
            area_id=area.id,
            new_zone_code="A",
            new_subzone_no=1,
        )
        preview = build_address_change_preview(db, command)
        area_id = int(area.id)
        location_id = int(location.id)
        payload = WarehouseAddressConfirmPayload(
            **command.__dict__,
            preview_fingerprint=preview["preview_fingerprint"],
            idempotency_key="p186-api-audit-rollback",
        )

        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("P1-86 injected audit failure")

        monkeypatch.setattr("app.api.warehouse.append_audit_event", fail_audit)
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
        with pytest.raises(RuntimeError, match="injected audit failure"):
            confirm_warehouse_location_address_change(
                payload,
                request,
                db=db,
                user=SimpleNamespace(id=1),
            )

    with session_factory() as verification:
        persisted_area = verification.get(WarehouseArea, area_id)
        persisted_location = verification.get(WarehouseLocation, location_id)
        assert persisted_area.address_zone_code is None
        assert persisted_location.location_code == "OLD-AUDIT-ADDRESS"
        assert verification.scalar(select(func.count(WarehouseLocationAlias.id))) == 0
        assert (
            verification.scalar(select(func.count(WarehouseLocationAddressMutation.id)))
            == 0
        )
