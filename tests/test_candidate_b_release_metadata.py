from app.version import APP_CHANGES, APP_EXTERNAL_ACCEPTANCE_REQUIRED, APP_VERIFICATION_STEPS


def test_current_release_is_visible_in_release_acceptance_card() -> None:
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    assert any(
        "SEMI-011" in item
        and "5.30×1.20米" in item
        and "4个标准栈板位" in item
        for item in APP_CHANGES
    )
    assert any(
        "零散区" in item
        and "不得出现可选栈板位" in item
        for item in APP_VERIFICATION_STEPS
    )
