from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.api.warehouse import list_location_candidates, list_locations
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    WarehouseArea,
    WarehouseFloor,
    WarehouseLocation,
)


WAREHOUSE_HTML = Path("static/warehouse.html").read_text(encoding="utf-8")


def _function_source(start: str, end: str) -> str:
    return WAREHOUSE_HTML.split(start, 1)[1].split(end, 1)[0]


def test_default_floor3_first_paint_does_not_load_inventory_ledgers() -> None:
    init = _function_source("async function init(){", "async function locateWarehouseLedger")

    assert "loadWarehouseSpace" in init
    assert "loadCustomers" in init
    assert "applyWarehouseDeepLink" in init
    assert "loadLocations" not in init
    assert "loadLots" not in init


def test_inventory_tabs_lazily_load_required_references_before_lots() -> None:
    switch_tab = _function_source("async function switchTab(tab){", "async function loadInsights")

    assert "await ensureLocations()" in switch_tab
    assert "await ensureCustomers()" in switch_tab
    assert "await loadLots()" in switch_tab
    assert "库存页面加载失败" in switch_tab


def test_admin_ledger_and_inventory_deep_links_keep_their_existing_load_paths() -> None:
    switch_location = _function_source(
        "async function switchLocationView(view){",
        "async function switchTab(tab){",
    )
    deep_link = _function_source(
        "async function applyWarehouseDeepLink(){",
        "function bind(){",
    )

    assert "await loadLocations(true)" in switch_location
    assert 'requestedTab&&["finished","semi_finished","molds"].includes(requestedTab)' in deep_link
    assert "await switchTab(requestedTab)" in deep_link
    assert "if(locationId||lotId||keyword)await locateWarehouseLedger" in deep_link


def test_warehouse_only_requests_flat_location_candidates() -> None:
    assert (
        "/api/warehouse/location-candidates?inventory_type=finished&include_hierarchy=false"
        in WAREHOUSE_HTML
    )
    assert (
        "inventory_type=finished&empty_only=true&pallet_storage_only=true&include_hierarchy=false"
        in WAREHOUSE_HTML
    )


def _location_db(tmp_path):
    engine = create_sqlite_engine(tmp_path / "warehouse-first-paint.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        floor = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼成品仓",
            floor_number=3,
            construction_status="enabled",
        )
        db.add(floor)
        db.flush()
        db.add(
            WarehouseArea(
                floor_id=floor.id,
                area_code="E1",
                area_name="E1 区",
                construction_status="enabled",
            )
        )
        for index in range(1, 31):
            location = WarehouseLocation(
                location_code=f"E1-L{index:02d}",
                location_name=f"E1 第 {index} 位",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="E1",
                storage_type="rack",
                source_version="V11",
                placement_status="placed",
                sort_order=index,
                is_active=True,
            )
            db.add(location)
            db.flush()
            db.add(
                Floor3LocationLayout(
                    location_id=location.id,
                    left_pct=Decimal(index % 10 * 8),
                    top_pct=Decimal(index // 10 * 12),
                    width_pct=Decimal("5"),
                    height_pct=Decimal("5"),
                    source_type="manual",
                )
            )
        db.commit()
    return engine, factory


def test_location_candidates_compact_mode_preserves_flat_items(tmp_path) -> None:
    _engine, factory = _location_db(tmp_path)
    with factory() as db:
        legacy = list_location_candidates(db=db, _user=None)
        compact = list_location_candidates(
            include_hierarchy=False,
            db=db,
            _user=None,
        )

    assert len(legacy["items"]) == 30
    assert legacy["items"] == compact["items"]
    assert legacy["floors"]
    assert "floors" not in compact


def test_location_ledger_eager_loads_current_map_status_without_n_plus_one(tmp_path) -> None:
    engine, factory = _location_db(tmp_path)
    selects = 0

    def count_selects(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal selects
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        with factory() as db:
            payload = list_locations(db=db, _user=None)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert len(payload["items"]) == 30
    assert {row["map_status"] for row in payload["items"]} == {"current_map_mapped"}
    # One ledger query plus five fixed-size projection preload queries.  The
    # count must not grow with the 30 locations.
    assert selects == 6
