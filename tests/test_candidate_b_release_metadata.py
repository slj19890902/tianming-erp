from app.version import APP_CHANGES, APP_EXTERNAL_ACCEPTANCE_REQUIRED, APP_VERIFICATION_STEPS


def test_current_release_is_visible_in_release_acceptance_card() -> None:
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    assert any(
        "客户全称、中文简称和客户缩写" in item
        and "既有搜索字段保持不变" in item
        for item in APP_CHANGES
    )
    assert any(
        "后端分页前" in item
        and "不区分大小写" in item
        for item in APP_CHANGES
    )
    assert any(
        "v0.22.193" in item
        and "fj45v8x9z34" in item
        for item in APP_VERIFICATION_STEPS
    )
