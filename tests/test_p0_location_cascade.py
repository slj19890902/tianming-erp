from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.warehouse_inventory import (
    InventoryPallet,
    WarehouseArea,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.floor3_locations import Floor3LocationError, move_pallet
from app.services.location_candidates import (
    list_operational_locations,
    operational_location_payload,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    _production_stock_location,
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
def location_db(tmp_path: Path):
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
        source_version="V11" if floor == 3 else "P0-TEST",
        is_active=active,
    )


def _seed_space(db: Session) -> dict[str, WarehouseLocation]:
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
    db.add_all(
        [
            WarehouseArea(
                floor_id=floor1.id,
                area_code="A1",
                area_name="一楼 A1",
                construction_status="enabled",
            ),
            WarehouseArea(
                floor_id=floor2.id,
                area_code="B1",
                area_name="二楼 B1",
                construction_status="enabled",
            ),
            WarehouseArea(
                floor_id=floor3.id,
                area_code="C1",
                area_name="三楼 C1",
                construction_status="enabled",
            ),
            WarehouseArea(
                floor_id=floor3.id,
                area_code="C2",
                area_name="三楼 C2",
                construction_status="layout_complete",
            ),
        ]
    )
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
    assert not list((ROOT / "alembic" / "versions").glob("cs75v8x9z64*"))
