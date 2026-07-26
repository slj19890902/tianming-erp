from types import SimpleNamespace

from app.services.product_readiness import material_comparison, product_readiness


def _product(**changes):
    material = SimpleNamespace(code="W535A", supplier_name="鸣朋", layer_count=5, is_active=True)
    values = dict(
        product_code="UAT-001", product_name="测试外箱",
        material=material, layer_count=5, flute_type="AB", box_style="A1",
        report_length_mm=600, report_width_mm=500, crease_type="压线",
        crease_left_mm=100, crease_middle_mm=300, crease_right_mm=100,
        base_report_length_mm=None, base_report_width_mm=None, base_crease_type=None,
        base_crease_left_mm=None, base_crease_middle_mm=None, base_crease_right_mm=None,
        manual_modified=False,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def test_readiness_ignores_manual_modified_and_requires_active_master_data():
    assert product_readiness(_product(manual_modified=True))["ready"] is True
    inactive = _product(material=SimpleNamespace(code="W535A", supplier_name="鸣朋", layer_count=5, is_active=False))
    result = product_readiness(inactive)
    assert result["ready"] is False
    assert "material" in result["missing_fields"]


def test_readiness_requires_complete_crease_and_a3_base_data():
    invalid = product_readiness(_product(crease_right_mm=90))
    assert invalid["status"] == "待完善"
    assert "crease" in invalid["missing_fields"]
    lid = product_readiness(_product(box_style="A3 天地盖"))
    assert lid["ready"] is False
    assert "base_report_length_mm" in lid["missing_fields"]


def test_readiness_requires_product_identity_layer_and_flute():
    result = product_readiness(
        _product(product_code="", product_name="", layer_count=None, flute_type=None)
    )
    assert result["ready"] is False
    assert {"product_code", "product_name", "layer_count", "flute_type"} <= set(
        result["missing_fields"]
    )


def test_material_comparison_uses_first_code_token_and_unknown_for_empty_values():
    assert material_comparison("W535A/AB", "W535A") == "same"
    assert material_comparison("W535A/AB", "K535A") == "different"
    assert material_comparison("", "W535A") == "unknown"
