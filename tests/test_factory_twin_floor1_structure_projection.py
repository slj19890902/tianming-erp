from __future__ import annotations

import json

import pytest

from factory_twin.scripts.project_floor1_structure_from_floor3 import (
    FRAME_MARKER,
    build_plan,
)


def _feature(
    feature_id: str,
    code: str,
    subtype: str,
    points: list[list[float]],
    *,
    status: str = "candidate",
    width: float = 700,
    height: float = 3000,
) -> dict:
    return {
        "id": feature_id,
        "layout_id": "layout",
        "feature_code": code,
        "name": subtype,
        "feature_kind": "structure",
        "subtype": subtype,
        "points_json": json.dumps(points),
        "width_mm": width,
        "storage_height_mm": height,
        "status": status,
    }


def _layouts() -> tuple[dict, dict]:
    floor1 = {
        "id": "one",
        "floor_code": "1F",
        "warnings_json": json.dumps([FRAME_MARKER]),
        "structures_json": json.dumps(
            [
                {"id": "old-wall", "kind": "wall"},
                {"id": "old-column", "kind": "column"},
                {"id": "keep-door", "kind": "door"},
                {"id": "keep-note", "kind": "unknown"},
            ]
        ),
    }
    floor3 = {
        "id": "three",
        "floor_code": "3F",
        "warnings_json": "[]",
        "structures_json": json.dumps(
            [
                {
                    "id": "DXF-W1",
                    "source_handle": "W1",
                    "kind": "wall",
                    "geometry": {"points": [[0, 0], [10000, 0]]},
                },
                {"id": "door", "kind": "door", "geometry": {"points": [[1, 1], [2, 2]]}},
            ]
        ),
    }
    return floor1, floor3


def test_projection_replaces_readonly_one_floor_columns_and_walls_but_preserves_manual_walls() -> None:
    floor1, floor3 = _layouts()
    common_points = [[-4614, -1605], [-2614, -1585]]
    one_features = [
        _feature("lift-one", "LIFT-002", "freight_elevator", common_points, width=3000, height=3500),
        _feature("manual-one", "WALL-1F-MANUAL-001", "custom_wall", [[0, 5000], [5000, 5000]], width=120),
    ]
    three_features = [
        _feature("lift-three", "LIFT-002", "freight_elevator", common_points, width=3000, height=3500),
        _feature("col-three", "COL-3F-002", "custom_column", [[1000, 1000], [1700, 1000]], status="confirmed"),
        _feature("wall-three", "STRUCT-3F-WALL-001", "custom_wall", [[2000, 3000], [8000, 3000]], width=120),
    ]

    plan = build_plan(
        floor1_layout=floor1,
        floor3_layout=floor3,
        floor1_features=one_features,
        floor3_features=three_features,
    )

    assert {item["kind"] for item in plan["retained_structures"]} == {"door", "unknown"}
    created = {item["feature_code"]: item for item in plan["create_features"]}
    assert created["COL-1F-002"]["status"] == "confirmed"
    assert created["COL-1F-002"]["points_json"] == [[1000, 1000], [1700, 1000]]
    assert "WALL-1F-3F-DXF-W1" in created
    assert "WALL-1F-3F-MANUAL-WALL-001" in created
    assert plan["preserved_existing_manual_walls"] == ["WALL-1F-MANUAL-001"]
    assert set(plan["lock_feature_ids"]) == {"lift-one", "lift-three"}
    assert plan["erp_inventory_writes"] == 0


def test_projection_requires_exact_shared_lift_alignment() -> None:
    floor1, floor3 = _layouts()
    one = [_feature("one", "LIFT-002", "freight_elevator", [[0, 0], [2000, 0]], width=3000, height=3500)]
    three = [
        _feature("three", "LIFT-002", "freight_elevator", [[10, 0], [2010, 0]], width=3000, height=3500),
        _feature("col", "COL-3F-001", "custom_column", [[0, 0], [700, 0]], status="confirmed"),
    ]
    with pytest.raises(RuntimeError, match="精确重合"):
        build_plan(
            floor1_layout=floor1,
            floor3_layout=floor3,
            floor1_features=one,
            floor3_features=three,
        )
