from app.services.warehouse_pick_route import recommend_pick_route


def _group(code: str, x: float, y: float) -> dict:
    return {
        "location_code": code,
        "warehouse_floor": 3,
        "map_status": "mapped",
        "map_point": {
            "left_pct": x,
            "top_pct": y,
            "width_pct": 0,
            "height_pct": 0,
        },
    }


def test_route_uses_map_proximity_not_rack_number_order() -> None:
    groups = [
        _group("R012", 24.0, 6.6),
        _group("R018", 24.0, 18.3),
        _group("R019", 23.0, 18.3),
        _group("R032", 20.7, 18.5),
        _group("R040", 24.0, 21.0),
    ]
    result = recommend_pick_route(groups)
    codes = [group["location_code"] for group in result]
    adjacent = {frozenset((codes[index - 1], codes[index])) for index in range(1, len(codes))}
    assert frozenset(("R019", "R032")) in adjacent
    assert frozenset(("R018", "R040")) in adjacent
    assert [group["recommended_sequence"] for group in result] == list(range(1, 6))
    assert all(group["route_basis"] == "published_measured_map" for group in result)


def test_unmapped_locations_are_kept_and_marked_as_fallback() -> None:
    result = recommend_pick_route(
        [
            _group("R012", 24.0, 6.6),
            {
                "location_code": "待核",
                "warehouse_floor": 3,
                "map_status": "unmapped",
                "priority": 3,
            },
        ]
    )
    assert result[-1]["location_code"] == "待核"
    assert result[-1]["route_basis"] == "text_fallback"
