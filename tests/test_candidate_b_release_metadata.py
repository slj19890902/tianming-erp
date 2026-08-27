from app.version import APP_CHANGELOG, APP_EXTERNAL_ACCEPTANCE_REQUIRED


def test_v022194_release_remains_visible_in_release_changelog() -> None:
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    assert any(
        item.startswith("v0.22.194：本次更新｜")
        and
        "SRO-20260810-0001" in item
        and "202+301+202" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.194：本次更新｜")
        and
        "稳定身份" in item
        and "当前产品主档" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.194：如何验证｜")
        and
        "v0.22.194" in item
        and "fj45v8x9z34" in item
        for item in APP_CHANGELOG
    )
