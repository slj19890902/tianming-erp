from factory_twin.scripts.optimize_floor3_layout import build_column_plan, build_semantic_plan


def _column(feature_id: str, code: str, x: float, y: float) -> dict:
    return {
        "id": feature_id, "feature_code": code, "feature_kind": "structure", "subtype": "custom_column",
        "points": [[x - 300, y], [x + 300, y]], "version": 1,
    }


def test_column_grid_alignment_snaps_rows_and_columns_without_resizing() -> None:
    features = [
        _column("a", "COL-3F-001", 1000, 2000),
        _column("b", "COL-3F-002", 1100, 8100),
        _column("c", "COL-3F-003", 9000, 1900),
        _column("d", "COL-3F-004", 9100, 8200),
    ]
    updates = build_column_plan(features, tolerance_mm=500)
    payloads = {item["feature"]["id"]: item["payload"]["points"] for item in updates}
    assert payloads["a"] == [[750, 1950], [1350, 1950]]
    assert payloads["b"] == [[750, 8150], [1350, 8150]]
    assert payloads["c"] == [[8750, 1950], [9350, 1950]]
    assert payloads["d"] == [[8750, 8150], [9350, 8150]]


def test_floor3_semantic_plan_removes_false_finished_waiting_meaning() -> None:
    base = {"feature_kind": "zone", "subtype": "finished_wait_delivery", "version": 1}
    features = [
        {**base, "id": "a", "feature_code": "ZONE-3F-ERP-A1", "name": "A1 成品地线区域"},
        {**base, "id": "b", "feature_code": "ZONE-3F-ERP-F1", "name": "F1 货架区域"},
        {**base, "id": "c", "feature_code": "ZONE-3F-FIN-003", "name": "成品待送堆放区"},
        {**base, "id": "d", "feature_code": "ZONE-3F-FIN-001", "name": "成品待送堆放区"},
    ]
    plan = {item["feature"]["id"]: item["payload"]["subtype"] for item in build_semantic_plan(features)}
    assert plan == {
        "a": "floor_marked_storage", "b": "rack_storage",
        "c": "delivery_surplus", "d": "unassigned_storage",
    }
