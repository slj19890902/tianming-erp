from __future__ import annotations

from pathlib import Path
import sqlite3

from factory_twin.scripts.plan_floor3_overlay import (
    AE_HALL_ANCHOR_MM,
    AE_RECALIBRATED_BOUNDS_MM,
    AE_TRANSLATION_MM,
    DXF_BOUNDS_MM,
    EXPECTED_AREA_CODES,
    F_CONNECTOR_ANCHOR_MM,
    F_TRANSLATION_MM,
    TARGET_ROOM_MM,
    build_candidate_features,
    should_create_missing_candidate,
    verify_legacy_area_source,
)


def test_floor3_overlay_keeps_legacy_codes_as_candidates_without_locations_or_equipment() -> None:
    features = build_candidate_features()
    codes = {item["feature_code"] for item in features}
    legacy_zones = [item for item in features if item["feature_code"].startswith("ZONE-3F-ERP-")]
    assert len(legacy_zones) == 22
    assert len(features) == 24
    assert len(codes) == len(features)
    assert {item["feature_code"].removeprefix("ZONE-3F-ERP-") for item in legacy_zones} == EXPECTED_AREA_CODES
    assert all(item["source"] == "ai" for item in features)
    assert all(item["feature_kind"] in {"zone", "aisle"} for item in features)
    assert "ZONE-3F-RAW-SEMI-PLAN-001" in codes
    assert "AISLE-3F-ERP-MAIN-001" in codes

    for feature in legacy_zones:
        for x, y in feature["points"]:
            assert DXF_BOUNDS_MM["min_x"] <= x <= DXF_BOUNDS_MM["max_x"]
            assert DXF_BOUNDS_MM["min_y"] <= y <= DXF_BOUNDS_MM["max_y"]


def test_floor3_overlay_splits_f_into_connector_and_moves_ae_right_and_down() -> None:
    legacy = {
        item["feature_code"]: item
        for item in build_candidate_features(alignment="legacy_room")
    }
    calibrated = {
        item["feature_code"]: item
        for item in build_candidate_features(alignment="dxf_recalibrated")
    }

    f_points = [
        point
        for code, feature in calibrated.items()
        if code.removeprefix("ZONE-3F-ERP-").startswith("F")
        for point in feature["points"]
    ]
    ae_points = [
        point
        for code, feature in calibrated.items()
        if code.startswith("ZONE-3F-ERP-")
        and not code.removeprefix("ZONE-3F-ERP-").startswith("F")
        for point in feature["points"]
    ]
    assert min(point[0] for point in f_points) == F_CONNECTOR_ANCHOR_MM["min_x"]
    assert min(point[1] for point in f_points) == F_CONNECTOR_ANCHOR_MM["min_y"]
    assert max(point[0] for point in f_points) < 8_700
    assert min(point[0] for point in ae_points) == AE_HALL_ANCHOR_MM["min_x"]
    assert max(point[1] for point in ae_points) == AE_HALL_ANCHOR_MM["max_y"]
    assert F_TRANSLATION_MM["x"] < 0
    assert AE_TRANSLATION_MM["x"] > 0
    assert AE_TRANSLATION_MM["y"] > 0

    old_a2 = legacy["ZONE-3F-ERP-A2"]["points"][0]
    new_a2 = calibrated["ZONE-3F-ERP-A2"]["points"][0]
    assert new_a2[0] > old_a2[0]
    assert new_a2[1] > old_a2[1]

    aisle = calibrated["AISLE-3F-ERP-MAIN-001"]
    assert aisle["points"][0][0] == F_CONNECTOR_ANCHOR_MM["min_x"]
    assert aisle["points"][1][0] == max(point[0] for point in ae_points)


def test_floor3_overlay_mirrors_ae_up_down_along_aisle_without_swapping_columns() -> None:
    unmirrored = {
        item["feature_code"]: item
        for item in build_candidate_features(alignment="dxf_recalibrated")
    }
    mirrored = {
        item["feature_code"]: item
        for item in build_candidate_features(alignment="dxf_recalibrated_aisle_vertical_mirror")
    }
    axis_sum = AE_RECALIBRATED_BOUNDS_MM["min_x"] + AE_RECALIBRATED_BOUNDS_MM["max_x"]

    for code, original in unmirrored.items():
        if code.startswith("ZONE-3F-ERP-F") or code in {"ZONE-3F-RAW-SEMI-PLAN-001", "AISLE-3F-ERP-MAIN-001"}:
            assert mirrored[code]["points"] == original["points"]
        elif code.startswith("ZONE-3F-ERP-"):
            expected = [[round(axis_sum - point[0], 3), point[1]] for point in original["points"]]
            assert mirrored[code]["points"] == expected

    original_ae_points = [
        point
        for code, feature in unmirrored.items()
        if code.startswith("ZONE-3F-ERP-") and not code.startswith("ZONE-3F-ERP-F")
        for point in feature["points"]
    ]
    mirrored_ae_points = [
        point
        for code, feature in mirrored.items()
        if code.startswith("ZONE-3F-ERP-") and not code.startswith("ZONE-3F-ERP-F")
        for point in feature["points"]
    ]
    assert (min(point[0] for point in mirrored_ae_points), max(point[0] for point in mirrored_ae_points)) == (
        min(point[0] for point in original_ae_points),
        max(point[0] for point in original_ae_points),
    )
    assert mirrored["AISLE-3F-ERP-MAIN-001"]["points"] == unmirrored["AISLE-3F-ERP-MAIN-001"]["points"]


def test_recalibration_preserves_manual_deletions_unless_the_code_is_explicitly_restored() -> None:
    code = "ZONE-3F-ERP-CD1"
    assert not should_create_missing_candidate(
        recalibrate_existing=True,
        feature_code=code,
        restore_missing_codes=set(),
    )
    assert should_create_missing_candidate(
        recalibrate_existing=True,
        feature_code=code,
        restore_missing_codes={code},
    )
    assert should_create_missing_candidate(
        recalibrate_existing=False,
        feature_code=code,
        restore_missing_codes=set(),
    )


def test_floor3_overlay_requires_the_verified_22_area_398_layout_source(tmp_path: Path) -> None:
    database = tmp_path / "legacy-floor3.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE warehouse_areas (area_code TEXT PRIMARY KEY, planned_location_count INTEGER NOT NULL);
            CREATE TABLE floor3_location_layouts (id INTEGER PRIMARY KEY);
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            INSERT INTO alembic_version VALUES ('cr74v8x9z63');
            """
        )
        for code in sorted(EXPECTED_AREA_CODES):
            connection.execute(
                "INSERT INTO warehouse_areas VALUES (?, ?)",
                (code, 398 if code == "A1" else 0),
            )
        connection.executemany(
            "INSERT INTO floor3_location_layouts(id) VALUES (?)",
            [(index,) for index in range(1, 399)],
        )
    result = verify_legacy_area_source(database)
    assert result == {
        "area_count": 22,
        "planned_location_count": 398,
        "layout_count": 398,
        "revision": "cr74v8x9z63",
        "integrity": "ok",
    }

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM floor3_location_layouts WHERE id = 398")
    try:
        verify_legacy_area_source(database)
    except RuntimeError as error:
        assert "不符合已验收基线" in str(error)
    else:
        raise AssertionError("mismatched legacy source must fail closed")
