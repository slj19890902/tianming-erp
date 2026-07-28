from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def test_supplier_material_workbook_has_simple_preview_then_apply_ui() -> None:
    assert "导出材质 Excel" in INDEX
    assert "导入材质 Excel" in INDEX
    assert "Excel 预检通过" in INDEX
    assert "确认批量导入" in INDEX
    assert "整批回滚" in INDEX
    assert '"/api/master/materials/import-template.xlsx"' in INDEX
    assert '"/api/master/materials/import/preview"' in INDEX
    assert '"/api/master/materials/import/apply"' in INDEX
    assert "{preview_token:preview.preview_token}" in INDEX


def test_supplier_aliases_are_only_used_for_display() -> None:
    assert 'if (name.includes("嘉林亿")) return "嘉林亿"' in INDEX
    assert 'if (name.includes("鸣朋")) return "鸣朋"' in INDEX
    assert "return name;" in INDEX
    assert ":value=\"supplier\"" in INDEX
    assert "{{ supplierDisplayName(supplier) }}" in INDEX
    assert ":value=\"s\">{{ supplierDisplayName(s) }}</option>" in INDEX


def test_import_refreshes_materials_and_supplier_options() -> None:
    method = INDEX.split("async applySupplierMaterialWorkbook()", 1)[1].split(
        "resetPaperCodeForm()", 1
    )[0]
    assert "this.allMaterials = []" in method
    assert "this.materialSuppliers = []" in method
    assert "await this.loadMaterials()" in method
    assert "await this.loadPaperCodes()" in method
