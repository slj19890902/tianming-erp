import pytest
from pydantic import ValidationError

from factory_twin.backend.schemas import RackCreate
from factory_twin.scripts.configure_floor3_modular_racks import build_summary, rack_specs


def test_owner_confirmed_physical_rack_modules_are_independently_addressable() -> None:
    specs = rack_specs()
    assert len(specs) == 17
    assert len({rack["rack_code"] for rack in specs}) == 17
    d2 = [rack for rack in specs if rack["rack_code"].startswith("RACK-3F-D2-")]
    f = [rack for rack in specs if rack["rack_code"].startswith("RACK-3F-F")]
    assert len(d2) == 5
    assert len(f) == 12
    special = next(rack for rack in d2 if "SPECIAL" in rack["rack_code"])
    assert (special["width_mm"], special["depth_mm"]) == (3000, 1500)
    assert all(
        (rack["width_mm"], rack["depth_mm"]) == (2800, 1100)
        for rack in specs if rack is not special
    )


def test_every_module_has_adjustable_levels_and_operator_cargo_rows() -> None:
    for rack in rack_specs():
        assert rack["height_mm"] == 2200
        assert rack["levels"] == 3
        assert rack["level_heights_mm"] == [750, 1500]
        assert 3 <= rack["cargo_rows"] <= 5
        assert rack["bays"] == 1


def test_f1_and_f4_are_two_rows_with_two_modules_long() -> None:
    specs = rack_specs()
    f1 = [rack for rack in specs if "RACK-3F-F1-" in rack["rack_code"]]
    f4 = [rack for rack in specs if "RACK-3F-F4-" in rack["rack_code"]]
    assert len(f1) == 4 and len({rack["x_mm"] for rack in f1}) == 2
    assert len(f4) == 4 and len({rack["x_mm"] for rack in f4}) == 2
    for racks in (f1, f4):
        assert len({rack["y_mm"] for rack in racks}) == 2
        assert max(rack["y_mm"] for rack in racks) - min(rack["y_mm"] for rack in racks) == 2800


def test_summary_replaces_the_nine_old_visual_racks_once() -> None:
    old = [
        {"rack_code": code, "is_locked": False}
        for code in (
            "RACK-3F-D2-EAST-001", "RACK-3F-D2-SOUTH-001", "RACK-3F-D2-WEST-001",
            "RACK-3F-F1-EAST-001", "RACK-3F-F1-WEST-001", "RACK-3F-F2-001",
            "RACK-3F-F3-001", "RACK-3F-F4-EAST-001", "RACK-3F-F4-WEST-001",
        )
    ]
    assert build_summary({"racks": old}) == {
        "delete_old_racks": 9,
        "create_modules": 17,
        "update_modules": 0,
    }


def test_existing_owner_adjustments_are_never_reported_for_overwrite() -> None:
    adjusted = rack_specs()[0] | {"x_mm": 99999, "width_mm": 3250}
    summary = build_summary({"racks": [adjusted]})
    assert summary["update_modules"] == 0


def test_rack_schema_rejects_invalid_operator_level_heights_and_cargo_rows() -> None:
    common = {
        "rack_code": "RACK-3F-TEST-001", "name": "测试货架",
        "x_mm": 0, "y_mm": 0, "width_mm": 2800, "depth_mm": 1100,
        "height_mm": 2200, "levels": 3, "bays": 1,
    }
    with pytest.raises(ValidationError, match="层板高度数量必须等于层数减一"):
        RackCreate(**common, level_heights_mm=[1000], cargo_rows=4)
    with pytest.raises(ValidationError):
        RackCreate(**common, level_heights_mm=[750, 1500], cargo_rows=6)


def test_rack_schema_accepts_two_adjustable_shelves_and_three_to_five_rows() -> None:
    rack = RackCreate(
        rack_code="RACK-3F-TEST-001", name="测试货架", x_mm=0, y_mm=0,
        width_mm=2800, depth_mm=1100, height_mm=2200, levels=3,
        level_heights_mm=[800, 1550], cargo_rows=5, bays=1,
    )
    assert rack.level_heights_mm == [800, 1550]
    assert rack.cargo_rows == 5
