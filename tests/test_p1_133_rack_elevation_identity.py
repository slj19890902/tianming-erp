from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import app.services.warehouse_twin_dashboard as warehouse_twin_dashboard


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TWIN_SOURCE = (
    PROJECT_ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")


def test_dashboard_location_payload_exposes_stable_rack_cell_identity(monkeypatch) -> None:
    row = SimpleNamespace(
        id=133,
        location_code="F1-S2-06",
        location_name="F1 西排第 2 层第 6 格",
        warehouse_floor=1,
        area_code="F1",
        warehouse_type="finished",
        storage_type="rack",
        address_kind="rack_slot",
        map_rack_id="rack-f1-west",
        rack_display_name="F1 西排货架",
        level_no=2,
        slot_no=6,
        address_version=7,
        source_version="TWIN_V1",
        is_temporary=False,
        is_active=True,
    )
    monkeypatch.setattr(
        warehouse_twin_dashboard,
        "warehouse_location_projection",
        lambda *_args, **_kwargs: {
            "position_status": "mapped",
            "map_position": None,
            "map_feature_id": "feature-f1-west",
            "published_map_revision": "revision-133",
            "map_status": "current_map_mapped",
            "map_issue": None,
        },
    )
    # Deliberately omit rack identity here.  The dashboard contract must expose
    # the formal WarehouseLocation identity rather than relying on an incidental
    # address serializer field set.
    monkeypatch.setattr(
        warehouse_twin_dashboard,
        "location_address_payload",
        lambda *_args, **_kwargs: {
            "employee_location_name": "F1 西排货架 第2层 第6格",
            "current_address_name": "F1 西排货架 第2层 第6格",
        },
    )

    payload = warehouse_twin_dashboard._location_payload(
        row,
        lots=[],
        pallets=[],
        as_of=date(2026, 9, 1),
    )

    assert payload["map_rack_id"] == "rack-f1-west"
    assert payload["rack_display_name"] == "F1 西排货架"
    assert payload["level_no"] == 2
    assert payload["slot_no"] == 6
    assert payload["address_kind"] == "rack_slot"
    assert payload["address_version"] == 7


def test_rack_elevation_uses_exact_identity_and_keeps_every_lot_visible() -> None:
    elevation = TWIN_SOURCE.split("function WarehouseRackElevation", 1)[1].split(
        "export function WarehouseTwinApp", 1
    )[0]
    focused_locations = TWIN_SOURCE.split(
        "const focusedRackLocations", 1
    )[1].split("const selectedStocktakeItem", 1)[0]

    assert "items[index]" not in elevation
    assert "emptyLocations[index - items.length]" not in elevation
    assert "rackCellIdentityKey(rack.id, level, bay + 1)" in elevation
    assert "cellItems.map((item, index)" in elevation
    assert "rackLocationInventoryItems(location)" in elevation

    assert "location.map_rack_id === focusedRack.id" in focused_locations
    assert 'location.address_kind === "rack_slot"' in focused_locations
    assert "Number.isInteger(location.level_no)" in focused_locations
    assert "Number.isInteger(location.slot_no)" in focused_locations
    assert "Math.hypot" not in focused_locations
    assert "nearest" not in focused_locations
    assert "!location.map_rack_id" in focused_locations
