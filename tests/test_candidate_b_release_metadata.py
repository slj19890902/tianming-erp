from app.version import APP_CHANGES, APP_EXTERNAL_ACCEPTANCE_REQUIRED, APP_VERIFICATION_STEPS


def test_current_release_is_visible_in_release_acceptance_card() -> None:
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    assert any(
        "SO383" in item
        and "已绑定其它业务栈板" in item
        and "不是员工操作错误" in item
        for item in APP_CHANGES
    )
    assert any(
        "SO383" in item
        and "87张" in item
        for item in APP_VERIFICATION_STEPS
    )
