from app.version import APP_CHANGES, APP_EXTERNAL_ACCEPTANCE_REQUIRED, APP_VERIFICATION_STEPS


def test_current_release_is_visible_in_release_acceptance_card() -> None:
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    assert any(
        "86箱不是新增实物" in item
        and "规范批次投影" in item
        for item in APP_CHANGES
    )
    assert any(
        "物理栈板、库位、地堆占位" in item
        and "不移动" in item
        for item in APP_CHANGES
    )
    assert any(
        "v0.22.191" in item
        and "fi44v8x9z33" in item
        for item in APP_VERIFICATION_STEPS
    )
