from factory_twin.scripts.optimize_floor1_operational_layout import (
    ROUTE_POINTS,
    ROUTE_WIDTH_MM,
    ZONE_TARGETS,
    _is_orthogonal,
    _polygon_bounds,
    _rects_intersect,
    _segment_rect,
    _segments_cross_non_adjacent,
    polygon_area_mm2,
)


def test_floor1_zones_are_orthogonal_non_overlapping_blocks() -> None:
    assert len(ZONE_TARGETS) == 19
    assert all(_is_orthogonal(points, closed=True) for points in ZONE_TARGETS.values())
    items = list(ZONE_TARGETS.items())
    overlaps = [
        (first_code, second_code)
        for index, (first_code, first_points) in enumerate(items)
        for second_code, second_points in items[index + 1 :]
        if _rects_intersect(_polygon_bounds(first_points), _polygon_bounds(second_points))
    ]
    assert overlaps == []


def test_floor1_route_is_one_continuous_1500_mm_polyline() -> None:
    assert ROUTE_WIDTH_MM == 1500
    assert ROUTE_POINTS == [
        [-14900, -8000],
        [-4940, -8000],
        [-4940, -24000],
        [-1500, -24000],
    ]
    assert _is_orthogonal(ROUTE_POINTS, closed=False)
    assert not _segments_cross_non_adjacent(ROUTE_POINTS)


def test_floor1_storage_blocks_do_not_enter_the_continuous_route() -> None:
    route_rects = [
        _segment_rect(start, end, ROUTE_WIDTH_MM)
        for start, end in zip(ROUTE_POINTS, ROUTE_POINTS[1:])
    ]
    hits = [
        code
        for code, points in ZONE_TARGETS.items()
        if any(_rects_intersect(_polygon_bounds(points), segment) for segment in route_rects)
    ]
    assert hits == []


def test_floor1_tidy_preserves_the_manual_storage_allocation() -> None:
    area_m2 = sum(polygon_area_mm2(points) for points in ZONE_TARGETS.values()) / 1_000_000
    assert round(area_m2, 3) == 174.399
    assert polygon_area_mm2(ZONE_TARGETS["ZONE-1F-TEMP-002"]) / 1_000_000 == 2.38
