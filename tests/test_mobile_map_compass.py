from app.api.mobile_erp import _mobile_map_compass


def test_mobile_compass_matches_published_desktop_calibration():
    for code in ('1F', '3F'):
        assert _mobile_map_compass({'floor_code': code})['code'] == 'E'
    assert _mobile_map_compass({'floor_code': '4F'})['code'] == 'N'
    assert _mobile_map_compass({'floor_code': '4F', 'metadata': {'calibration': {'status': 'aligned', 'applied': False}}})['code'] == 'N'
    assert _mobile_map_compass({'floor_code': '4F', 'metadata': {'calibration': {'status': 'aligned', 'applied': True}}})['code'] == 'E'
    assert _mobile_map_compass({'floor_code': '4F', 'alignment_status': 'aligned', 'alignment_applied': True})['code'] == 'E'
    assert _mobile_map_compass(None)['code'] == 'N'
