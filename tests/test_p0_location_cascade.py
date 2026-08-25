from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLot,
    InventoryPallet,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseLocation,
)
from app.services.floor3_locations import Floor3LocationError, move_pallet
from app.services.location_candidates import (
    list_operational_locations,
    operational_location_payload,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    _production_direct_staging_location,
    _production_stock_location,
    list_temporary_locations,
)
from app.services.stocktake import (
    StocktakeError,
    _get_countable_location,
    list_locations as list_stocktake_locations,
)


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_HTML = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STOCKTAKE_HTML = (ROOT / "static" / "mobile_stocktake.html").read_text(
    encoding="utf-8"
)


@pytest.fixture()
def location_db(tmp_path: Path, monkeypatch):
    from app.services import location_candidates

    identities = {
        1: {
            "revision": "p1-101-test-map",
            "zones_by_id": {
                "zone-1f-a1": "A1",
                "zone-1f-draft": "DRAFT",
            },
            "zone_ids_by_area": {
                "A1": ("zone-1f-a1",),
                "DRAFT": ("zone-1f-draft",),
            },
        },
        3: {
            "revision": "p1-101-test-map",
            "zones_by_id": {"zone-3f-c1": "C1"},
            "zone_ids_by_area": {"C1": ("zone-3f-c1",)},
        },
    }
    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        lambda floor_number: identities.get(
            int(floor_number),
            {
                "revision": "p1-101-test-map",
                "zones_by_id": {},
                "zone_ids_by_area": {},
            },
        ),
    )
    engine = create_sqlite_engine(tmp_path / "p0-location-cascade.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        yield db
    engine.dispose()


def _location(
    code: str,
    *,
    floor: int | None,
    area: str | None,
    warehouse_type: str = "finished",
    storage_type: str = "ground",
    placement_status: str = "placed",
    active: bool = True,
) -> WarehouseLocation:
    return WarehouseLocation(
        location_code=code,
        location_name=f"测试库位 {code}",
        warehouse_type=warehouse_type,
        warehouse_floor=floor,
        area_code=area,
        storage_type=storage_type,
        placement_status=placement_status,
        source_version="V11" if floor == 3 else "TWIN_V1",
        is_active=active,
    )


def _seed_space(db: Session) -> dict[str, WarehouseLocation]:
    from app.models.user import User

    operator = User(
        username="p0-location-cascade",
        password_hash="not-used",
        role="admin",
        real_name="匿名位置级联测试",
        must_change_password=False,
    )
    db.add(operator)
    db.flush()
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
        construction_status="layout_complete",
    )
    floor3 = WarehouseFloor(
        floor_code="3F",
        floor_name="三楼",
        floor_number=3,
        construction_status="enabled",
    )
    db.add_all([floor1, floor2, floor3])
    db.flush()
    area_a1 = WarehouseArea(
        floor_id=floor1.id,
        area_code="A1",
        area_name="一楼 A1",
        construction_status="enabled",
    )
    area_b1 = WarehouseArea(
        floor_id=floor2.id,
        area_code="B1",
        area_name="二楼 B1",
        construction_status="enabled",
    )
    area_c1 = WarehouseArea(
        floor_id=floor3.id,
        area_code="C1",
        area_name="三楼 C1",
        construction_status="enabled",
    )
    area_c2 = WarehouseArea(
        floor_id=floor3.id,
        area_code="C2",
        area_name="三楼 C2",
        construction_status="layout_complete",
    )
    db.add_all([area_a1, area_b1, area_c1, area_c2])
    db.flush()
    rows = {
        "valid_1f": _location("A1-L01", floor=1, area="A1"),
        "valid_3f": _location("C1-L01", floor=3, area="C1"),
        "unplaced": _location(
            "C1-L02",
            floor=3,
            area="C1",
            placement_status="unplaced",
        ),
        "disabled_area": _location("C2-L01", floor=3, area="C2"),
        "disabled_floor": _location("B1-L01", floor=2, area="B1"),
        "unregistered_area": _location("X1-L01", floor=3, area="X1"),
        "unregistered_floor": _location("Z1-L01", floor=9, area="Z1"),
        "inactive": _location("C1-L03", floor=3, area="C1", active=False),
        "wrong_type": _location(
            "C1-SF01",
            floor=3,
            area="C1",
            warehouse_type="semi_finished",
        ),
        "rack": _location(
            "C1-S01-01",
            floor=3,
            area="C1",
            storage_type="rack",
        ),
    }
    db.add_all(rows.values())
    db.flush()
    for index, location in enumerate(
        (rows["valid_1f"], rows["valid_3f"], rows["rack"]), start=1
    ):
        db.add(
            Floor3LocationLayout(
                location_id=location.id,
                left_pct=Decimal(index),
                top_pct=Decimal("1"),
                width_pct=Decimal("4"),
                height_pct=Decimal("4"),
                version=1,
                source_type="seeded",
                layout_kind="physical_pallet",
            )
        )
    db.add(
        WarehouseAreaStoragePolicy(
            area_id=area_a1.id,
            map_feature_id="zone-1f-a1",
            allowed_inventory_types_json='["finished"]',
            storage_layout="pallet_ground",
            status="published",
            published_map_revision="p1-101-test-map",
            version=1,
        )
    )
    ground_plan = WarehouseGroundLayoutPlan(
        area_id=area_a1.id,
        status="published",
        target_slot_count=1,
        numbering_origin="south",
        row_direction="from_aisle_inward",
        slot_direction="left_to_right",
        row_start_no=1,
        slot_start_no=1,
        draft_map_revision="p1-101-test-map",
        published_map_revision="p1-101-test-map",
        preview_fingerprint="a" * 64,
        version=1,
        publish_idempotency_key="p0-location-cascade-ground",
        publish_request_hash="b" * 64,
        updated_by=operator.id,
        published_by=operator.id,
        published_at=datetime.now(),
    )
    db.add(ground_plan)
    db.flush()
    db.add(
        WarehouseGroundLayoutSlot(
            plan_id=ground_plan.id,
            location_id=rows["valid_1f"].id,
            route_sequence=1,
            row_no=1,
            slot_no=1,
            x_mm=Decimal("1000"),
            y_mm=Decimal("1000"),
            width_mm=1200,
            depth_mm=1000,
        )
    )
    floor3_ground_plan = WarehouseGroundLayoutPlan(
        area_id=area_c1.id,
        status="published",
        target_slot_count=1,
        numbering_origin="south",
        row_direction="from_aisle_inward",
        slot_direction="left_to_right",
        row_start_no=1,
        slot_start_no=1,
        draft_map_revision="p1-101-test-map",
        published_map_revision="p1-101-test-map",
        preview_fingerprint="c" * 64,
        version=1,
        publish_idempotency_key="p0-location-cascade-floor3-ground",
        publish_request_hash="d" * 64,
        updated_by=operator.id,
        published_by=operator.id,
        published_at=datetime.now(),
    )
    db.add(floor3_ground_plan)
    db.flush()
    db.add(
        WarehouseGroundLayoutSlot(
            plan_id=floor3_ground_plan.id,
            location_id=rows["valid_3f"].id,
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
    return rows


def test_one_operational_rule_drives_candidates_production_stocktake_and_move(
    location_db: Session,
) -> None:
    rows = _seed_space(location_db)
    candidates = list_operational_locations(
        location_db,
        warehouse_types={"finished", "shared"},
    )
    candidate_ids = {row.location.id for row in candidates}
    assert candidate_ids == {
        rows["valid_1f"].id,
        rows["valid_3f"].id,
        rows["rack"].id,
    }
    payloads = [operational_location_payload(row) for row in candidates]
    assert {
        (row["warehouse_floor"], row["area_code"], row["location_code"])
        for row in payloads
    } == {
        (1, "A1", "A1-L01"),
        (3, "C1", "C1-L01"),
        (3, "C1", "C1-S01-01"),
    }

    stocktake_ids = {row["id"] for row in list_stocktake_locations(location_db)}
    assert stocktake_ids == candidate_ids
    assert _get_countable_location(location_db, rows["valid_1f"].id).id == rows[
        "valid_1f"
    ].id
    with pytest.raises(StocktakeError, match="区域尚未启用"):
        _get_countable_location(location_db, rows["disabled_area"].id)

    assert (
        _production_stock_location(
            location_db,
            rows["valid_1f"].id,
            pallet_id=None,
        ).id
        == rows["valid_1f"].id
    )
    with pytest.raises(ProductionWorkflowError, match="楼层尚未启用"):
        _production_stock_location(
            location_db,
            rows["disabled_floor"].id,
            pallet_id=None,
        )
    with pytest.raises(ProductionWorkflowError, match="空间放置"):
        _production_stock_location(
            location_db,
            rows["unplaced"].id,
            pallet_id=None,
        )

    pallet = InventoryPallet(
        pallet_code="P0-CASCADE-PALLET",
        location_id=rows["valid_3f"].id,
        status="active",
        is_current=True,
        version=1,
    )
    location_db.add(pallet)
    location_db.flush()
    moved = move_pallet(
        location_db,
        pallet_id=pallet.id,
        expected_version=1,
        to_location_id=rows["valid_1f"].id,
        remarks="跨楼层三级联动验证",
        operator_id=None,
        idempotency_key="p0-location-cascade-move",
        expected_target_layout_version=1,
    )
    assert moved.pallet.location_id == rows["valid_1f"].id
    assert moved.pallet.version == 2
    with pytest.raises(Floor3LocationError, match="区域尚未启用"):
        move_pallet(
            location_db,
            pallet_id=pallet.id,
            expected_version=2,
            to_location_id=rows["disabled_area"].id,
            remarks=None,
            operator_id=None,
            idempotency_key="p0-location-cascade-invalid-move",
        )
    correction = InventoryPallet(
        pallet_code="P0-CORRECTION-PALLET",
        location_id=rows["disabled_area"].id,
        status="active",
        is_current=True,
        version=1,
    )
    location_db.add(correction)
    location_db.flush()
    corrected = move_pallet(
        location_db,
        pallet_id=correction.id,
        expected_version=1,
        to_location_id=rows["valid_3f"].id,
        remarks="把旧错误位置移入正式库位",
        operator_id=None,
        idempotency_key="p0-location-cascade-correct-source",
        expected_target_layout_version=1,
    )
    assert corrected.pallet.location_id == rows["valid_3f"].id


def test_empty_pallet_candidates_exclude_occupied_and_rack(location_db: Session) -> None:
    rows = _seed_space(location_db)
    location_db.add(
        InventoryPallet(
            pallet_code="P0-OCCUPIED",
            location_id=rows["valid_3f"].id,
            status="active",
            is_current=True,
            version=1,
        )
    )
    location_db.flush()
    candidates = list_operational_locations(
        location_db,
        warehouse_types={"finished", "shared"},
        pallet_storage_only=True,
        empty_only=True,
    )
    assert {row.location.id for row in candidates} == {rows["valid_1f"].id}

    location_db.add(
        InventoryLot(
            lot_number="P0-LIVE-LOT-WITHOUT-PALLET",
            inventory_type="finished",
            warehouse_location_id=rows["valid_1f"].id,
            quantity_available=7,
            quantity_reserved=2,
            quantity_consumed=0,
            quantity_damaged=1,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 8, 10),
            last_movement_at=datetime(2026, 8, 10, 9, 0, 0),
            version=1,
        )
    )
    location_db.flush()
    candidates = list_operational_locations(
        location_db,
        warehouse_types={"finished", "shared"},
        pallet_storage_only=True,
        empty_only=True,
    )
    assert candidates == []
    occupied = list_operational_locations(
        location_db,
        warehouse_types={"finished", "shared"},
    )
    assert {
        row.location.id for row in occupied if row.occupied
    } == {rows["valid_1f"].id, rows["valid_3f"].id}


def test_new_production_and_map_targets_require_publication_but_old_sources_can_leave(
    location_db: Session,
) -> None:
    rows = _seed_space(location_db)
    floor1 = location_db.scalar(
        select(WarehouseFloor).where(WarehouseFloor.floor_number == 1)
    )
    assert floor1 is not None
    draft_area = WarehouseArea(
        floor_id=floor1.id,
        area_code="DRAFT",
        area_name="一楼草稿区",
        construction_status="enabled",
    )
    location_db.add(draft_area)
    location_db.flush()
    draft_location = _location("DRAFT-L01", floor=1, area="DRAFT")
    location_db.add(draft_location)
    location_db.flush()
    location_db.add_all(
        [
            Floor3LocationLayout(
                location_id=draft_location.id,
                left_pct=Decimal("10"),
                top_pct=Decimal("10"),
                width_pct=Decimal("4"),
                height_pct=Decimal("4"),
                version=1,
                source_type="manual",
                layout_kind="physical_pallet",
            ),
            WarehouseAreaStoragePolicy(
                area_id=draft_area.id,
                map_feature_id="zone-1f-draft",
                allowed_inventory_types_json='["finished"]',
                storage_layout="pallet_ground",
                status="draft",
                draft_map_revision="p1-101-draft-map",
                version=1,
            ),
        ]
    )
    source = InventoryPallet(
        pallet_code="P1-101-PUBLISHED-SOURCE",
        location_id=rows["valid_3f"].id,
        status="active",
        is_current=True,
        version=1,
    )
    old_source = InventoryPallet(
        pallet_code="P1-101-OLD-DRAFT-SOURCE",
        location_id=draft_location.id,
        status="active",
        is_current=True,
        version=1,
    )
    location_db.add_all([source, old_source])
    location_db.flush()

    assert draft_location.location_code not in {
        row["location_code"] for row in list_temporary_locations(location_db)
    }
    with pytest.raises(Floor3LocationError, match="所属区域尚未发布"):
        move_pallet(
            location_db,
            pallet_id=source.id,
            expected_version=1,
            to_location_id=draft_location.id,
            remarks="不得移入草稿位置",
            operator_id=None,
            idempotency_key="p1-101-reject-draft-target",
            require_published_target=True,
            expected_target_layout_version=1,
        )

    corrected = move_pallet(
        location_db,
        pallet_id=old_source.id,
        expected_version=1,
        to_location_id=rows["valid_1f"].id,
        remarks="旧位置库存迁入已发布位置",
        operator_id=None,
        idempotency_key="p1-101-leave-old-source",
        require_published_target=True,
        expected_target_layout_version=1,
    )
    assert corrected.pallet.location_id == rows["valid_1f"].id


def test_general_production_excludes_dispatch_but_direct_delivery_keeps_it(
    location_db: Session,
) -> None:
    rows = _seed_space(location_db)
    floor1 = location_db.scalar(
        select(WarehouseFloor).where(WarehouseFloor.floor_number == 1)
    )
    location_db.add(
        WarehouseArea(
            floor_id=floor1.id,
            area_code="DISPATCH",
            area_name="一楼待送区",
            construction_status="enabled",
        )
    )
    dispatch = _location(
        "F1-DISPATCH-01",
        floor=1,
        area="DISPATCH",
        storage_type="temporary_aisle",
    )
    dispatch.source_version = "P1-25C"
    dispatch.location_name = "一楼待送区"
    location_db.add(dispatch)
    location_db.flush()

    general_codes = {row["location_code"] for row in list_temporary_locations(location_db)}
    assert "F1-DISPATCH-01" not in general_codes
    assert {rows["valid_1f"].location_code, rows["valid_3f"].location_code}.issubset(
        general_codes
    )
    assert _production_direct_staging_location(location_db).id == dispatch.id
    with pytest.raises(ProductionWorkflowError, match="只供直接待送"):
        _production_stock_location(location_db, dispatch.id, pallet_id=None)


def test_stage_c_frontends_use_floor_area_location_without_extra_migration() -> None:
    for marker in (
        'id="floorFilter"',
        'id="fgFloor"',
        'id="fgArea"',
        'id="fgLocation"',
        'id="lotEditFloor"',
        "refreshInventoryFloorOptions",
        "/api/warehouse/location-candidates?inventory_type=finished",
        "empty_only=true&pallet_storage_only=true",
    ):
        assert marker in WAREHOUSE_HTML
    for marker in (
        "location_floor_number",
        "transfer_floor_number",
        "productionLocationFloors",
        "productionAreasForFloor",
        "productionLocationsForArea(row.location_floor_number,row.location_area_code)",
    ):
        assert marker in INDEX_HTML
    for marker in (
        'id="floorFilter"',
        "renderFloors",
        "locationFloor",
        "请先选择楼层",
    ):
        assert marker in STOCKTAKE_HTML
    assert not list((ROOT / "alembic" / "versions").glob("*location_cascade*"))
