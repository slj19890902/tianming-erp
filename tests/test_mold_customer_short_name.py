from app.services.mold_identity import (
    mold_customer_short_name,
    mold_label_display_identity,
    mold_label_display_number,
)


def test_mold_customer_short_name_uses_explicit_label_prefix() -> None:
    assert (
        mold_customer_short_name(
            "聚晟达61452621",
            "苏州聚晟达电子材料科技有限公司",
            "JSD",
        )
        == "聚晟达"
    )


def test_mold_customer_short_name_accepts_hash_separated_mold_number() -> None:
    assert (
        mold_customer_short_name(
            "瑞明#9",
            "苏州瑞明香氛科技股份有限公司",
            "RM",
        )
        == "瑞明"
    )
    assert (
        mold_label_display_number(
            "瑞明#9",
            "RM-9",
            "苏州瑞明香氛科技股份有限公司",
            "RM",
        )
        == "9"
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


def test_mold_label_identity_prefers_maintained_chinese_name_number() -> None:
    assert (
        mold_label_display_number(
            "聚晟达61452621",
            "JCD-61452621",
            "苏州聚晟达电子材料科技有限公司",
            "JSD",
        )
        == "61452621"
    )
    assert (
        mold_label_display_identity(
            "聚晟达61452621",
            "JCD-61452621",
            "苏州聚晟达电子材料科技有限公司",
            "JSD",
        )
        == "聚晟达61452621"
    )


def test_mold_label_number_only_strips_a_proven_customer_code_prefix() -> None:
    assert (
        mold_label_display_number(
            "旧模具名称",
            "JSD-A-001",
            "聚晟达",
            "JSD",
        )
        == "A-001"
    )
    assert (
        mold_label_display_number(
            "旧模具名称",
            "JCD-61452621",
            "聚晟达",
            "JSD",
        )
        == "JCD-61452621"
    )
