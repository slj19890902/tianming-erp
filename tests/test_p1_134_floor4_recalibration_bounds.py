from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from app.services import warehouse_twin_layout_editor as editor


TARGET_POINTS = [[0.0, 1500.0], [2000.0, -1500.0], [2000.0, 1500.0]]


def _rotate(points: list[list[float]], degrees: float) -> list[list[float]]:
    radians = math.radians(degrees)
    cosine, sine = math.cos(radians), math.sin(radians)
    return [
        [
            cosine * point[0] - sine * point[1],
            sine * point[0] + cosine * point[1],
        ]
        for point in points
    ]


def _floor(code: str, *, features: list[dict] | None = None) -> dict:
    floor = {
        "layout_id": f"layout-{code.lower()}",
        "floor_code": code,
        "name": code,
        "bounds_mm": {
            "min_x": 0.0,
            "min_y": 0.0,
            "max_x": 2000.0,
            "max_y": 1000.0,
        },
        "structures": [],
        "features": features or [],
        "placements": [],
        "racks": [],
        "pallets": [],
        "assets": [],
    }
    floor["revision"] = editor._floor_revision(floor)
    return floor


def _document() -> dict:
    authority = {
        "id": "lift-authority-3f",
        "layout_id": "layout-3f",
        "feature_code": "LIFT-002",
        "feature_kind": "structure",
        "points": [[0.0, 0.0], [2000.0, 0.0]],
        "width_mm": 3000.0,
        "is_locked": True,
        "version": 17,
    }
    floor4 = _floor(
        "4F",
        features=[
            {
                "id": "stable-zone-4f-a1",
                "feature_code": "ZONE-4F-A1",
                "feature_kind": "zone",
                "points": [[100.0, 100.0], [500.0, 100.0], [500.0, 400.0]],
                "stable_location_id": 401,
            }
        ],
    )
    floor4.update(
        {
            "operational_status": "planning_only",
            "calibration": {"status": "requires_site_points", "applied": False},
            "inventory_snapshot": {
                "lot_id": 9001,
                "location_id": 401,
                "quantity": 88,
            },
        }
    )
    floor4["revision"] = editor._floor_revision(floor4)
    return {
        "schema_version": 1,
        "generated_at": "test",
        "floors": {
            "1F": _floor("1F"),
            "3F": _floor("3F", features=[authority]),
            "4F": floor4,
        },
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_floor4_rotate_then_inverse_restores_oriented_bounds_without_other_fact_drift(
    tmp_path: Path,
) -> None:
    published = tmp_path / "published.json"
    draft = tmp_path / "draft.json"
    document = _document()
    original_floor4 = deepcopy(document["floors"]["4F"])
    protected_floors = deepcopy(
        {code: document["floors"][code] for code in ("1F", "3F")}
    )
    _write(published, document)

    first = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=original_floor4["revision"],
        operation_key="floor4-bounds-initial-001",
        source_points=TARGET_POINTS,
        published_path=published,
        draft_path=draft,
    )
    rotated = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=first.floor_revision,
        operation_key="floor4-bounds-rotate-001",
        source_points=_rotate(TARGET_POINTS, -30.0),
        published_path=published,
        draft_path=draft,
    )
    restored = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=rotated.floor_revision,
        operation_key="floor4-bounds-inverse-001",
        source_points=_rotate(TARGET_POINTS, 30.0),
        published_path=published,
        draft_path=draft,
    )

    saved = json.loads(draft.read_text(encoding="utf-8"))
    assert {code: saved["floors"][code] for code in ("1F", "3F")} == protected_floors
    floor4 = saved["floors"]["4F"]
    assert floor4["bounds_mm"] == pytest.approx(original_floor4["bounds_mm"], abs=0.002)
    polygon = floor4["metadata"][editor.FLOOR4_SPATIAL_BOUNDS_POLYGON_KEY]
    expected_polygon = [
        [0.0, 0.0],
        [0.0, 1000.0],
        [2000.0, 0.0],
        [2000.0, 1000.0],
    ]
    for actual, expected in zip(polygon, expected_polygon, strict=True):
        assert actual == pytest.approx(expected, abs=0.002)
    zone = next(item for item in floor4["features"] if item.get("feature_code") == "ZONE-4F-A1")
    assert zone["id"] == "stable-zone-4f-a1"
    assert zone["stable_location_id"] == 401
    for actual, expected in zip(
        zone["points"], original_floor4["features"][0]["points"], strict=True
    ):
        assert actual == pytest.approx(expected, abs=0.002)
    assert floor4["inventory_snapshot"] == original_floor4["inventory_snapshot"]
    lifts = [item for item in floor4["features"] if item.get("feature_code") == "LIFT-002"]
    assert len(lifts) == 1
    assert lifts[0]["id"] == editor.FLOOR4_CANONICAL_FREIGHT_ELEVATOR_ID
    assert restored.value["inventory_changed"] is False


def test_floor4_recalibration_rejects_tampered_persisted_bounds_without_writing(
    tmp_path: Path,
) -> None:
    published = tmp_path / "published.json"
    draft = tmp_path / "draft.json"
    document = _document()
    _write(published, document)
    first = editor.calibrate_floor4_freight_elevator(
        "4F",
        expected_revision=document["floors"]["4F"]["revision"],
        operation_key="floor4-bounds-tamper-seed",
        source_points=TARGET_POINTS,
        published_path=published,
        draft_path=draft,
    )
    tampered = json.loads(draft.read_text(encoding="utf-8"))
    tampered["floors"]["4F"]["metadata"][editor.FLOOR4_SPATIAL_BOUNDS_POLYGON_KEY][0][0] = -100.0
    tampered["floors"]["4F"]["revision"] = editor._floor_revision(tampered["floors"]["4F"])
    _write(draft, tampered)
    before = draft.read_bytes()

    with pytest.raises(
        editor.WarehouseTwinLayoutEditConflictError,
        match="持久边界与当前地图边界不一致",
    ):
        editor.calibrate_floor4_freight_elevator(
            "4F",
            expected_revision=tampered["floors"]["4F"]["revision"],
            operation_key="floor4-bounds-tamper-reject",
            source_points=_rotate(TARGET_POINTS, -10.0),
            published_path=published,
            draft_path=draft,
        )
    assert draft.read_bytes() == before
    assert first.applied is True
