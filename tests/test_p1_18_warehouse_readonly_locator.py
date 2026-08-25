from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import warehouse


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_HTML = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def _location(
    *,
    floor: int | None,
    source_version: str | None = None,
    placement_status: str | None = None,
    is_active: bool = True,
    layout: object | None = None,
    storage_type: str = "rack",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        location_code="A1-R01",
        area_code="A1",
        storage_type=storage_type,
        warehouse_floor=floor,
        source_version=source_version,
        placement_status=placement_status,
        is_active=is_active,
        floor3_layout=layout,
    )


def _projection_context(row: SimpleNamespace) -> dict:
    layout = row.floor3_layout
    if layout is not None:
        layout = SimpleNamespace(
            left_pct=10,
            top_pct=20,
            width_pct=5,
            height_pct=6,
            z_index=1,
            version=1,
            source_type="manual",
            layout_kind="physical_pallet",
        )
    return {
        "floor": SimpleNamespace(
            id=3,
            floor_number=row.warehouse_floor,
            construction_status="enabled",
        ),
        "area": SimpleNamespace(
            id=31,
            area_code="A1",
            construction_status="enabled",
        ),
        "policy": None,
        "published_floor_identity": {
            "revision": "test-map-v1",
            "zones_by_id": {"zone-a1": "A1"},
            "zone_ids_by_area": {"A1": ("zone-a1",)},
        },
        "ground_layout": None,
        "layout": layout,
    }


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            _location(
                floor=3,
                source_version="V11",
                placement_status="placed",
                layout=object(),
            ),
            "floor3_mapped",
        ),
        (
            _location(
                floor=1,
                source_version=None,
                placement_status="placed",
                layout=None,
            ),
            "ledger_only",
        ),
        (
            _location(
                floor=3,
                source_version="V11",
                placement_status="unplaced",
                layout=object(),
            ),
            "unplaced",
        ),
        (
            _location(
                floor=3,
                source_version="V11",
                placement_status="placed",
                layout=None,
            ),
            "unplaced",
        ),
        (
            _location(
                floor=3,
                source_version="V11",
                placement_status="placed",
                is_active=False,
                layout=object(),
            ),
            "unplaced",
        ),
        (
            _location(
                floor=3,
                source_version=None,
                placement_status="placed",
                layout=object(),
            ),
            "ledger_only",
        ),
    ],
)
def test_location_map_status_is_server_derived_and_requires_real_layout(
    row: SimpleNamespace,
    expected: str,
) -> None:
    assert warehouse._location_map_status(row, _projection_context(row)) == expected


def test_candidate_locations_publish_the_server_derived_map_status() -> None:
    source = (ROOT / "app" / "api" / "warehouse.py").read_text(encoding="utf-8")

    assert "warehouse_location_projection(" in source
    assert "load_warehouse_location_projection_contexts(db, rows)" in source
    assert 'projection["position_status"] != "mapped"' in source


def test_v11_ground_position_needs_a_current_published_plan_slot() -> None:
    row = _location(
        floor=3,
        source_version="V11",
        placement_status="placed",
        layout=object(),
        storage_type="ground",
    )

    assert warehouse._location_map_status(row, _projection_context(row)) == "unplaced"


def test_readonly_locator_blocks_writes_and_hides_all_write_entry_points() -> None:
    assert (
        'const warehouseReadonlyMode = warehouseSearchParams.get("readonly") === "1";'
        in WAREHOUSE_HTML
    )
    assert "readOnly:warehouseReadonlyMode" in WAREHOUSE_HTML
    assert (
        'if(state.readOnly&&!["GET","HEAD"].includes(method))'
        'throw new Error("当前为订单库存只读定位，不能修改仓库数据")'
        in WAREHOUSE_HTML
    )
    assert (
        'const canOperate=()=>!state.readOnly&&hasPermission("warehouse.execute")'
        in WAREHOUSE_HTML
    )
    assert (
        'function canManageLocations(){return !state.readOnly&&state.user?.role==="admin"}'
        in WAREHOUSE_HTML
    )
    assert (
        'function floor3CanEditLayout(){return !state.readOnly&&state.user?.role==="admin"}'
        in WAREHOUSE_HTML
    )
    assert (
        'if(state.user.role!=="admin"||state.readOnly)'
        'document.querySelectorAll(".admin-only")'
        in WAREHOUSE_HTML
    )
    assert "if(state.readOnly){" in WAREHOUSE_HTML
    assert (
        'document.querySelectorAll("[data-tab]").forEach(button=>'
        'button.classList.toggle("hidden",button.dataset.tab!=="finished"))'
        in WAREHOUSE_HTML
    )
    assert (
        'if(state.readOnly)throw new Error("当前为订单库存只读定位，不能修改仓库数据")'
        in WAREHOUSE_HTML
    )
    assert (
        'if(state.readOnly&&warehouseEmbeddedMode){'
        in WAREHOUSE_HTML
    )
    assert "只读库存位置登录已失效，请返回 PDF 订单后重新登录" in WAREHOUSE_HTML


def test_locations_deep_link_opens_real_floor3_map_or_explicit_ledger_fallback() -> None:
    start = WAREHOUSE_HTML.index("async function applyWarehouseDeepLink(){")
    end = WAREHOUSE_HTML.index("    function bind(){", start)
    deep_link = WAREHOUSE_HTML[start:end]

    assert (
        'lot=await api(`/api/warehouse/lots/${lotId}`)'
        in deep_link
    )
    assert "actualLocation=lot?.location||null" in deep_link
    assert "库存位置已变化，已按当前正式位置重新定位" in deep_link
    assert 'requestedTab==="locations"&&locationId' in deep_link
    assert '!bindingMismatch&&declaredPositionStatus==="mapped"' in deep_link
    assert "await openTwinFloor(" in deep_link
    assert "state.floor3.deepLinkLotId=lotId||null" in deep_link

    assert (
        "await locateWarehouseLedger({lot,locationId,keyword,"
        "reason:`${driftPrefix}${reason}`})"
        in deep_link
    )
    assert "该库位尚未映射到当前已发布实测地图，已定位库存台账" in deep_link
    assert "库存定位链接缺少批次信息" in deep_link
    assert "批次地图绑定与当前库位不一致" in deep_link


def test_unlaid_location_cannot_be_rendered_as_a_synthetic_deep_link_map_point() -> None:
    start = WAREHOUSE_HTML.index("async function applyWarehouseDeepLink(){")
    end = WAREHOUSE_HTML.index("    function bind(){", start)
    deep_link = WAREHOUSE_HTML[start:end]

    assert 'declaredPositionStatus==="mapped"' in deep_link
    assert "floor3LocationLayout(" not in deep_link
    assert (
        "state.floor3.mapLocations=state.readOnly?"
        'state.floor3.locations.filter(row=>row.position_status==="mapped"&&row.layout):state.floor3.locations'
        in WAREHOUSE_HTML
    )
    row = _location(
        floor=3,
        source_version="V11",
        placement_status="placed",
        layout=None,
    )
    assert warehouse._location_map_status(row, _projection_context(row)) == "unplaced"


def test_inventory_location_selects_only_consume_canonical_mapped_positions() -> None:
    assert (
        'const semiLocations=active.filter(x=>x.position_status==="mapped"'
        in WAREHOUSE_HTML
    )
    assert (
        'row.is_active!==false&&row.position_status==="mapped"&&allowed.includes'
        in WAREHOUSE_HTML
    )


def test_lot_serializer_keeps_null_location_in_unlocated_queue_instead_of_500() -> None:
    from app.models.warehouse_inventory import InventoryLot

    lot = InventoryLot(
        lot_number="FG-NULL-LOCATION",
        inventory_type="finished",
        warehouse_location_id=None,
        quantity_available=3,
        quantity_reserved=0,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit="boxes",
        status="active",
        source_type="manual",
        stock_date=date(2026, 8, 25),
        stock_date_accuracy="exact",
        last_movement_at=datetime(2026, 8, 25, 8, 0, 0),
    )

    payload = warehouse._lot_dict(lot)

    assert payload["location"]["id"] is None
    assert payload["location"]["position_status"] == "unlocated"
    assert payload["location"]["map_position"] is None
