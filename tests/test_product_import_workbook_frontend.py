from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def test_product_workbook_import_controls_are_admin_and_customer_scoped() -> None:
    assert (
        'v-if="canAdmin && productTab === \'products\' && selectedProductCustomer"'
        in INDEX
    )
    assert "下载样品 Excel" in INDEX
    assert "样品 Excel 导入" in INDEX
    assert "当前客户：{{ selectedProductCustomer.name }}" in INDEX
    assert "ERP 自动合并重复产品行和跨卷图片" in INDEX


def test_product_workbook_import_accepts_multiple_volumes_and_previews_first() -> None:
    assert 'accept=".xlsx" multiple' in INDEX
    assert "选择一卷或多卷 Excel" in INDEX
    assert '"/api/master/products/import-template.xlsx"' in INDEX
    assert '"/api/master/products/import/preview"' in INDEX
    assert '"/api/master/products/import/apply"' in INDEX
    assert 'form.append("customer_id",String(this.selectedProductCustomer.id))' in INDEX
    assert 'state.files.forEach(file=>form.append("files",file))' in INDEX
    assert 'state.drawings.forEach(file=>form.append("drawings",file))' in INDEX


def test_preview_is_card_based_and_apply_is_one_click() -> None:
    assert "只补图 {{ productWorkbookImport.preview.summary?.drawing_only_products || 0 }} 款" in INDEX
    assert "待完善 {{ productWorkbookImport.preview.summary?.needs_completion || 0 }} 款" in INDEX
    assert "预检未通过，ERP 没有写入数据" in INDEX
    assert '@click="applyProductWorkbookImport">确认录入</button>' in INDEX
    assert "grid-template-columns:repeat(auto-fit,minmax(260px,1fr))" in INDEX
    assert "确认原因" not in INDEX[INDEX.index("样品 Excel 多卷导入") : INDEX.index("正在读取该客户的常用箱")]
