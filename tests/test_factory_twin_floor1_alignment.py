import math

from factory_twin.scripts.apply_floor1_alignment import (
    AxisControl,
    Calibration,
    legacy_1f_point_to_reference,
    reference_point_to_legacy_1f,
    warp_axis,
)


BOUNDS_3F = {"min_x": -23924.0, "min_y": -30276.0, "max_x": 34705.0, "max_y": 14691.0}
ACCEPTED_CALIBRATION = Calibration(
    offset_x_mm=11500,
    offset_y_mm=5100,
    scale_x=1,
    scale_y=1,
    mirror_x=False,
    mirror_y=False,
    rotation_deg=-90,
)


def test_saved_browser_reference_transform_has_an_exact_inverse() -> None:
    for point in ((-23924.0, -30276.0), (-3614.0, -1595.0), (34705.0, 14691.0)):
        legacy = reference_point_to_legacy_1f(point, BOUNDS_3F, ACCEPTED_CALIBRATION)
        returned = legacy_1f_point_to_reference(legacy, BOUNDS_3F, ACCEPTED_CALIBRATION)
        assert math.dist(point, returned) < 1e-6


def test_axis_warp_hits_column_controls_and_interpolates_monotonically() -> None:
    controls = [
        AxisControl(-24040.0, -24030.0),
        AxisControl(-16706.0, -16410.0),
        AxisControl(-9089.0, -9135.0),
        AxisControl(-2504.0, -2064.0),
    ]
    for control in controls:
        assert warp_axis(control.source_mm, controls) == control.target_mm
    samples = [warp_axis(value, controls) for value in (-26000, -22000, -14000, -6000, 0)]
    assert samples == sorted(samples)
