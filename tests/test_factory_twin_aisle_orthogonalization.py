from __future__ import annotations

from factory_twin.scripts.orthogonalize_confirmed_aisles import orthogonalize_polyline


def test_two_point_aisles_keep_their_length_axis_and_snap_the_short_axis() -> None:
    assert orthogonalize_polyline([[-519, -4589], [-12692, -4653]]) == [
        [-519, -4621],
        [-12692, -4621],
    ]
    assert orthogonalize_polyline([[-5064, -5436], [-4940, -24129]]) == [
        [-5002, -5436],
        [-5002, -24129],
    ]


def test_u_shaped_aisle_preserves_topology_and_creates_right_angle_corners() -> None:
    assert orthogonalize_polyline(
        [[27440, -21451], [27519, -26249], [31451, -26013], [31687, -21687]]
    ) == [
        [27480, -21451],
        [27480, -26131],
        [31569, -26131],
        [31569, -21687],
    ]
