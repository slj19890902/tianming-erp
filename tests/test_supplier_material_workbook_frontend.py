from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def test_supplier_material_workbook_ui_is_paused_but_page_maintenance_remains() -> None:
    for removed in (
        "下载材质模板",
        "导出材质 Excel",
        "导入材质 Excel",
        "Excel 预检通过",
        "确认批量导入",
        '"/api/master/materials/import-template.xlsx"',
        '"/api/master/materials/import/preview"',
        '"/api/master/materials/import/apply"',
        "supplierMaterialWorkbook",
    ):
        assert removed not in INDEX

    assert "材质维护" in INDEX
    assert "供应商材质规则维护" in INDEX
    assert "新增代码" in INDEX
    assert "保存修改" in INDEX
    assert "供应商调价" in INDEX


def test_supplier_aliases_are_only_used_for_display() -> None:
    assert 'if (name.includes("嘉林亿")) return "嘉林亿"' in INDEX
    assert 'if (name.includes("鸣朋")) return "鸣朋"' in INDEX
    assert "return name;" in INDEX
    assert ":value=\"supplier\"" in INDEX
    assert "{{ supplierDisplayName(supplier) }}" in INDEX
    assert ":value=\"s\">{{ supplierDisplayName(s) }}</option>" in INDEX


def test_other_order_pdf_and_excel_entries_are_not_removed() -> None:
    assert "识别PDF订单" in INDEX
    assert "导入常用箱" in INDEX
    assert "导出 Excel" in INDEX
