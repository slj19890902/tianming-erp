from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def test_product_workbook_import_controls_are_admin_and_customer_scoped() -> None:
    assert (
        'v-if="canAdmin && productTab === \'products\' && selectedProductCustomer"'
        in INDEX
    )
    assert "下载导入模板" in INDEX
    assert "批量导入 Excel" in INDEX
    assert "只上传这一个 Excel" in INDEX
    assert "ERP 自动提取图纸并生成缩略图" in INDEX
    assert "无需重复选择图片" in INDEX
    assert "选择图纸原文件（旧模板）" in INDEX
    assert "都会整批阻断且不写库" in INDEX


def test_product_workbook_import_uses_preview_then_apply_contract() -> None:
    assert '"/api/master/products/import-template.xlsx"' in INDEX
    assert '"/api/master/products/import/preview"' in INDEX
    assert '"/api/master/products/import/apply"' in INDEX
    assert "form.append(\"customer_id\",String(this.selectedProductCustomer.id))" in INDEX
    assert 'state.drawings.forEach(file=>form.append("drawings",file))' in INDEX
    assert "待完善 {{ productWorkbookImport.preview.summary?.needs_completion || 0 }}" in INDEX
    assert "待完善行允许先建档" in INDEX
    assert 'split(/[，、,;；]/)' in INDEX
    assert "{preview_token:state.preview.preview_token}" in INDEX


def test_product_process_editor_keeps_imported_structured_processes() -> None:
    for value in ("印刷", "开槽", "模切", "打钉", "粘合", "二次粘合"):
        assert f'value="{value}"' in INDEX
    assert '"粘贴":"粘合"' in INDEX
