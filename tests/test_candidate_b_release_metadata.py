from app.version import APP_CHANGES, APP_EXTERNAL_ACCEPTANCE_REQUIRED, APP_VERIFICATION_STEPS


def test_candidate_b_is_visible_in_release_acceptance_card() -> None:
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    assert any(
        "送货拿货员" in item and "通用订单" in item and "销售金额" in item
        for item in APP_CHANGES
    )
    assert any(
        "送货拿货员账号" in item
        and "本人" in item
        and "单价" in item
        and "付款状态" in item
        for item in APP_VERIFICATION_STEPS
    )
