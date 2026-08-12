from app.services.mold_identity import mold_customer_short_name


def test_mold_customer_short_name_uses_explicit_label_prefix() -> None:
    assert (
        mold_customer_short_name(
            "聚晟达61452621",
            "苏州聚晟达电子材料科技有限公司",
            "JSD",
        )
        == "聚晟达"
    )


def test_mold_customer_short_name_does_not_guess_from_descriptive_mold_name() -> None:
    assert (
        mold_customer_short_name("手机查找测试模", "模具客户", "MJKH")
        == "模具客户"
    )
    assert mold_customer_short_name("MOLD-001", "", "JSD") == "JSD"
    assert mold_customer_short_name(None, None, None) is None


def test_mold_customer_short_name_rejects_unrelated_chinese_prefix() -> None:
    assert (
        mold_customer_short_name(
            "其他客户61452621",
            "苏州聚晟达电子材料科技有限公司",
            "JSD",
        )
        == "苏州聚晟达电子材料科技有限公司"
    )
