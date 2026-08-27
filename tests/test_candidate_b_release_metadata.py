from app.version import APP_CHANGES, APP_EXTERNAL_ACCEPTANCE_REQUIRED, APP_VERIFICATION_STEPS


def test_current_release_is_visible_in_release_acceptance_card() -> None:
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    assert any(
        "RAW-001四个原料货架位" in item
        and "不增加库存" in item
        for item in APP_CHANGES
    )
    assert any(
        "1200×1000" in item
        and "安全阻断" in item
        for item in APP_CHANGES
    )
    assert any(
        "v0.22.192" in item
        and "fj45v8x9z34" in item
        for item in APP_VERIFICATION_STEPS
    )
