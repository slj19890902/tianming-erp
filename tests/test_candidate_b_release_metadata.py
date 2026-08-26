from app.version import APP_CHANGES, APP_EXTERNAL_ACCEPTANCE_REQUIRED, APP_VERIFICATION_STEPS


def test_current_release_is_visible_in_release_acceptance_card() -> None:
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    assert any(
        "模具40×80" in item and "中文简写" in item and "模具标签名称" in item
        for item in APP_CHANGES
    )
    assert any(
        "实体40×80" in item
        and "二维码可扫" in item
        for item in APP_VERIFICATION_STEPS
    )
