from __future__ import annotations

from math import hypot


def _point(group: dict) -> tuple[float, float] | None:
    value = group.get("map_point") or {}
    try:
        return (
            float(value["left_pct"]) + float(value.get("width_pct") or 0) / 2,
            float(value["top_pct"]) + float(value.get("height_pct") or 0) / 2,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _distance(left: dict, right: dict) -> float:
    a, b = _point(left), _point(right)
    if a is None or b is None:
        return 1_000_000.0
    return hypot(a[0] - b[0], a[1] - b[1])


def _length(path: list[dict]) -> float:
    return sum(_distance(path[index - 1], path[index]) for index in range(1, len(path)))


def _nearest_path(groups: list[dict], start: int) -> list[dict]:
    remaining = list(groups)
    current = remaining.pop(start)
    path = [current]
    while remaining:
        next_group = min(
            remaining,
            key=lambda candidate: (
                _distance(current, candidate),
                str(candidate.get("location_code") or ""),
            ),
        )
        remaining.remove(next_group)
        path.append(next_group)
        current = next_group
    return path


def _two_opt(path: list[dict]) -> list[dict]:
    if len(path) < 4:
        return path
    best = list(path)
    improved = True
    while improved:
        improved = False
        for left in range(1, len(best) - 2):
            for right in range(left + 1, len(best)):
                candidate = best[:left] + list(reversed(best[left:right])) + best[right:]
                if _length(candidate) + 1e-9 < _length(best):
                    best = candidate
                    improved = True
    return best


def recommend_pick_route(groups: list[dict]) -> list[dict]:
    """Return a stable open route using formal map positions.

    This is deliberately an open route: the employee does not have to return to
    the first rack.  It derives proximity from the published measured map, so
    adjacent racks such as R019/R032 and R018/R040 can remain adjacent even when
    their rack numbers are not consecutive.
    """

    mapped_by_floor: dict[int, list[dict]] = {}
    fallback: list[dict] = []
    for group in groups:
        if group.get("map_status") == "mapped" and _point(group) is not None:
            mapped_by_floor.setdefault(int(group.get("warehouse_floor") or 999), []).append(group)
        else:
            fallback.append(group)
    ordered: list[dict] = []
    for floor in sorted(mapped_by_floor):
        floor_groups = mapped_by_floor[floor]
        candidates = [_two_opt(_nearest_path(floor_groups, start)) for start in range(len(floor_groups))]
        route = min(
            candidates,
            key=lambda path: (
                _length(path),
                tuple(str(group.get("location_code") or "") for group in path),
            ),
        )
        # Either orientation has the same distance.  Keep a deterministic end.
        reverse = list(reversed(route))
        route = min(
            (route, reverse),
            key=lambda path: tuple(str(group.get("location_code") or "") for group in path),
        )
        ordered.extend(route)
    fallback.sort(
        key=lambda group: (
            int(group.get("priority") or 0),
            int(group.get("warehouse_floor") or 999),
            str(group.get("area_code") or ""),
            int(group.get("location_sort_order") or 0),
            str(group.get("location_code") or ""),
        )
    )
    ordered.extend(fallback)
    for sequence, group in enumerate(ordered, start=1):
        group["recommended_sequence"] = sequence
        group["route_basis"] = (
            "published_measured_map" if group.get("map_status") == "mapped" else "text_fallback"
        )
    return ordered
