from app.version import APP_CHANGES, APP_EXTERNAL_ACCEPTANCE_REQUIRED, APP_VERIFICATION_STEPS


def test_current_release_is_visible_in_release_acceptance_card() -> None:
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    assert any(
        "TM20260825004" in item
        and "胜源供应商" in item
        and "不带时区" in item
        for item in APP_CHANGES
    )
    assert any(
        "TM20260825004" in item
        and "G527A" in item
        for item in APP_VERIFICATION_STEPS
    )
