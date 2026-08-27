from app.version import APP_CHANGES, APP_EXTERNAL_ACCEPTANCE_REQUIRED, APP_VERIFICATION_STEPS


def test_current_release_is_visible_in_release_acceptance_card() -> None:
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    assert any(
        "SRO-20260810-0001" in item
        and "202+301+202" in item
        for item in APP_CHANGES
    )
    assert any(
        "稳定身份" in item
        and "当前产品主档" in item
        for item in APP_CHANGES
    )
    assert any(
        "v0.22.194" in item
        and "fj45v8x9z34" in item
        for item in APP_VERIFICATION_STEPS
    )
