from factory_twin.scripts.refine_floor3_storage_usage import build_plan


def _feature(
    code: str,
    *,
    name: str = "旧名称",
    subtype: str = "finished_storage",
    storage_mode: str = "floor",
) -> dict:
    return {
        "id": code,
        "feature_code": code,
        "name": name,
        "feature_kind": "zone",
        "subtype": subtype,
        "points": [[-1022, -12074], [78, -12074], [78, -6173], [-1022, -6173]],
        "width_mm": None,
        "direction": None,
        "no_stacking": False,
        "storage_mode": storage_mode,
        "elevation_mm": 0,
        "storage_height_mm": 1000,
        "color": "#3b82f6",
        "version": 1,
    }


def _rack(code: str, x_mm: float, y_mm: float) -> dict:
    return {"id": code, "rack_code": code, "x_mm": x_mm, "y_mm": y_mm, "is_locked": False}


def test_confirmed_storage_usage_keeps_existing_racks_and_adds_only_f1_west_pair() -> None:
    features = [
        _feature("ZONE-3F-ERP-D1"),
        _feature("ZONE-3F-ERP-D2", subtype="rack_storage", storage_mode="rack"),
        _feature("ZONE-3F-ERP-F1", subtype="rack_storage"),
        _feature("ZONE-3F-FG-001"),
        _feature("ZONE-3F-FIN-003"),
        _feature("ZONE-3F-FIN-004"),
    ]
    racks = [
        *[_rack(f"RACK-3F-D2-{index}", 0, index * 1000) for index in range(5)],
        _rack("RACK-3F-F1-EAST-SOUTH-001", -490, -10676),
        _rack("RACK-3F-F1-EAST-NORTH-001", -543, -7606),
    ]
    plan = build_plan({"id": "3f", "floor_code": "3F", "features": features, "racks": racks})

    assert plan["summary"]["f1_rack_creates"] == 2
    assert {rack["rack_code"] for rack in plan["rack_creates"]} == {
        "RACK-3F-F1-WEST-SOUTH-001",
        "RACK-3F-F1-WEST-NORTH-001",
    }
    assert plan["summary"]["equipment_moves"] == 0
    assert plan["summary"]["erp_inventory_writes"] == 0


def test_surplus_semantics_and_f1_width_are_explicit() -> None:
    features = [
        _feature("ZONE-3F-ERP-D1"),
        _feature("ZONE-3F-ERP-D2", subtype="rack_storage", storage_mode="rack"),
        _feature("ZONE-3F-ERP-F1", subtype="rack_storage"),
        _feature("ZONE-3F-FG-001"),
        _feature("ZONE-3F-FIN-003"),
        _feature("ZONE-3F-FIN-004"),
    ]
    racks = [
        *[_rack(f"RACK-3F-D2-{index}", 0, index * 1000) for index in range(5)],
        _rack("RACK-3F-F1-EAST-SOUTH-001", -490, -10676),
        _rack("RACK-3F-F1-EAST-NORTH-001", -543, -7606),
    ]
    plan = build_plan({"id": "3f", "floor_code": "3F", "features": features, "racks": racks})
    updates = {row["feature"]["feature_code"]: row["payload"] for row in plan["feature_updates"]}

    for code in ("ZONE-3F-FG-001", "ZONE-3F-FIN-003", "ZONE-3F-FIN-004"):
        assert updates[code]["subtype"] == "delivery_surplus"
        assert "长期存放" in updates[code]["name"]
        assert "同订单续配" in updates[code]["name"]
    f1_points = updates["ZONE-3F-ERP-F1"]["points"]
    assert max(point[0] for point in f1_points) - min(point[0] for point in f1_points) == 2200
    assert updates["ZONE-3F-ERP-D1"]["name"] == "D1 栈板成品存放区（无货架）"
    assert updates["ZONE-3F-ERP-D2"]["storage_mode"] == "floor"
