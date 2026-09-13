"""Targeted guards for BOMUI001's readable combination relationship summary."""

from pathlib import Path


HTML_PATH = Path("static/index.html")


def _product_bom_editor() -> str:
    html = HTML_PATH.read_text(encoding="utf-8")
    return html.split('<details class="product-secondary-disclosure">', 1)[1].split(
        "</fieldset>", 1
    )[0]


def test_common_box_bom_explains_dynamic_parent_child_relationship() -> None:
    html = HTML_PATH.read_text(encoding="utf-8")
    editor = _product_bom_editor()

    assert 'class="bom-relationship-summary"' in editor
    assert "bomRelationshipSummary()" in html
    assert "{{ bomRelationshipSummary() }}" in editor
    assert "productForm.unit" in editor
    assert "quantity_per_set" in editor


def test_common_box_bom_keeps_identity_and_three_independent_modes_clear() -> None:
    html = HTML_PATH.read_text(encoding="utf-8")
    editor = _product_bom_editor()

    assert 'class="bom-component-identity"' in editor
    assert "编码：" in editor
    assert "名称：" in editor
    assert "规格：" in editor
    assert "库存来源" in editor
    assert "计价方式" in editor
    assert "交付方式" in editor
    assert "每套用量" not in editor

    # Existing persistence/permission flow remains the owner of BOM writes.
    assert "_productBomSaveFields" in html
    assert "saveProductBom" in html
    assert "canEditProducts" in editor
