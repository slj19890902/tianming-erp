from factory_twin.scripts.finalize_floor1_aisle_network import (
    AISLE_TARGETS,
    _is_orthogonal,
    _network_connected,
    _positive_colinear_overlap,
    _segments,
)


def test_owner_drawn_floor1_aisles_become_one_connected_network() -> None:
    assert len(AISLE_TARGETS) == 8
    assert all(_is_orthogonal(points) for points in AISLE_TARGETS.values())
    assert _network_connected()


def test_floor1_aisle_network_has_no_duplicate_colinear_segments() -> None:
    all_segments = [
        (code, index, segment)
        for code, points in AISLE_TARGETS.items()
        for index, segment in enumerate(_segments(points))
    ]
    overlaps = []
    for index, (first_code, first_index, first) in enumerate(all_segments):
        for second_code, second_index, second in all_segments[index + 1 :]:
            if first_code == second_code and abs(first_index - second_index) <= 1:
                continue
            if _positive_colinear_overlap(first, second):
                overlaps.append((first_code, second_code))
    assert overlaps == []


def test_east_short_route_uses_a_right_angle_to_clear_the_machine() -> None:
    assert AISLE_TARGETS["AISLE-1F-SECONDARY-004"] == [
        [-4940, -8000],
        [-4940, -7300],
        [-3350, -7300],
    ]
    assert AISLE_TARGETS["AISLE-1F-SECONDARY-005"][0] == [-3350, -7300]


def test_new_branches_join_the_existing_route_at_exact_centrelines() -> None:
    assert AISLE_TARGETS["AISLE-1F-PED-006"][0] == [-15300, -8000]
    assert AISLE_TARGETS["AISLE-1F-SECONDARY-001"][0] == [-15300, -8000]
    assert AISLE_TARGETS["AISLE-1F-MAIN-001"][-1] == [-13300, -8000]
    assert AISLE_TARGETS["AISLE-1F-SECONDARY-003"][0] == [-15300, -25700]
