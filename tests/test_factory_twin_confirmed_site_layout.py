import json
from pathlib import Path

from factory_twin.scripts.apply_confirmed_site_layout import build_layout, summarize


LAYOUT_PATH = Path("static/factory_maps/twin_layout_v1.json")


def _source() -> dict:
    return json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))


def test_confirmed_site_layout_corrects_d2_without_moving_existing_racks() -> None:
    source = _source()
    before = {rack["rack_code"]: rack for rack in source["floors"]["3F"]["racks"] if rack["rack_code"].startswith("RACK-3F-D2-")}
    result = build_layout(source, edited_at="2026-08-08T00:00:00+00:00")
    after = {rack["rack_code"]: rack for rack in result["floors"]["3F"]["racks"] if rack["rack_code"].startswith("RACK-3F-D2-")}

    assert after.keys() == before.keys()
    assert all((after[code]["x_mm"], after[code]["y_mm"], after[code]["rotation_deg"]) == (before[code]["x_mm"], before[code]["y_mm"], before[code]["rotation_deg"]) for code in after)
    special = after["RACK-3F-D2-SPECIAL-001"]
    assert (special["width_mm"], special["depth_mm"], special["height_mm"], special["levels"]) == (2000, 2000, 2600, 3)
    standard = [rack for code, rack in after.items() if code != "RACK-3F-D2-SPECIAL-001"]
    assert len(standard) == 4
    assert all((rack["width_mm"], rack["depth_mm"], rack["height_mm"], rack["levels"]) == (1800, 1100, 2600, 3) for rack in standard)
    assert all(rack["formal_location_mapping"] is False for rack in after.values())
    assert all(rack["cell_plan_status"] == "pending_admin_configuration" for rack in after.values())


def test_confirmed_site_layout_adds_confirmed_mold_numbering_without_inventory_locations() -> None:
    result = build_layout(_source(), edited_at="2026-08-08T00:00:00+00:00")
    floor1 = result["floors"]["1F"]
    racks = {rack["rack_code"]: rack for rack in floor1["racks"]}
    assert set(racks) >= {
        "RACK-1F-MOLD-R01-001",
        "RACK-1F-MOLD-R02-001",
        "RACK-1F-MOLD-R03-001",
        "RACK-1F-PLATE-002-001",
    }
    assert racks["RACK-1F-MOLD-R01-001"]["area_code"] == "ZONE-1F-MOLD-002"
    assert racks["RACK-1F-MOLD-R01-001"]["mold_rack_code"] == "R01"
    assert racks["RACK-1F-MOLD-R02-001"]["levels"] == 2
    assert racks["RACK-1F-MOLD-R03-001"]["level_usage"][0] == "第一层：大模板"
    plate = racks["RACK-1F-PLATE-002-001"]
    assert (plate["width_mm"], plate["depth_mm"], plate["height_mm"], plate["levels"]) == (2000, 1600, 3500, 2)
    assert all(rack["formal_location_mapping"] is False for rack in racks.values())

    wall_positions = [feature for feature in floor1["features"] if feature.get("subtype") == "mold_wall_storage"]
    assert {feature["feature_code"] for feature in wall_positions} == {
        "MOLD-1F-R04-L1-SOUTH-001",
        "MOLD-1F-R04-L1-WEST-001",
    }
    assert all(feature["feature_kind"] == "structure" for feature in wall_positions)
    assert all(feature["mold_location_family"] == "1F-M-R04-L1-V" for feature in wall_positions)
    assert all(feature["erp_area_code"] is None and feature["formal_location_mapping"] is False for feature in wall_positions)


def test_layout_transform_is_idempotent_and_never_reports_database_or_inventory_writes() -> None:
    once = build_layout(_source(), edited_at="2026-08-08T00:00:00+00:00")
    twice = build_layout(once, edited_at="2026-08-08T00:00:00+00:00")
    assert twice == once
    floor1_codes = [rack["rack_code"] for rack in twice["floors"]["1F"]["racks"]]
    assert len(floor1_codes) == len(set(floor1_codes))
    wall_codes = [feature["feature_code"] for feature in twice["floors"]["1F"]["features"] if feature.get("subtype") == "mold_wall_storage"]
    assert len(wall_codes) == 2
    report = summarize(_source(), twice)
    assert report["database_connected"] is False
    assert report["inventory_written"] is False
