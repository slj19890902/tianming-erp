from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
IMPORT_SCRIPT = (
    ROOT / "scripts" / "admin" / "import_yl_yke_kew_common_boxes.py"
).read_text(encoding="utf-8")


def test_shared_drawing_preview_is_global_for_new_order_common_box_import() -> None:
    marker = "共用图纸预览必须位于业务页面分支之外"
    assert INDEX.count(marker) == 1
    assert INDEX.count('v-if="showDrawingPreview"') == 1
    assert INDEX.index(marker) < INDEX.index(
        '<div v-if="pdfWarehouseLocator.visible"'
    )
    business_modal = INDEX.index('<div v-if="modal" class="modal-mask"')
    business_modal_end = INDEX.index(marker)
    assert '<div v-if="modal?.type !== \'product\'" class="modal-foot">' in INDEX[business_modal:business_modal_end]

    import_start = INDEX.index("async importSelectedOrderCommonBoxes()")
    import_end = INDEX.index("openOrderPdfImport()", import_start)
    import_logic = INDEX[import_start:import_end]
    assert "await this.selectOrderProduct(index, product.id)" in import_logic

    select_start = INDEX.index("async selectOrderProduct(index, productId)")
    select_end = INDEX.index("async selectOrderMaterial(index)", select_start)
    assert "item._product_drawings = data.drawings || []" in INDEX[
        select_start:select_end
    ]
    assert "this.showDrawingPreview = true" in INDEX


def test_controlled_yke_kew_import_does_not_write_report_notes() -> None:
    target_start = IMPORT_SCRIPT.index("def _target_product_values(")
    target_end = IMPORT_SCRIPT.index("def _comparable(", target_start)
    target_logic = IMPORT_SCRIPT[target_start:target_end]
    assert '"report_notes": None' in target_logic
    assert '"base_report_notes": None' in target_logic
    assert 'row.get("report_notes")' not in target_logic
