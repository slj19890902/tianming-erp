from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")
WAREHOUSE = Path("static/warehouse.html").read_text(encoding="utf-8")


def test_pdf_import_shows_common_box_adaptation_and_inline_full_editor() -> None:
    assert "常用箱已修改" in INDEX
    assert "常用箱未修改" in INDEX
    assert "立即编辑常用箱" in INDEX
    assert "openCommonBoxEditorFromPdf" in INDEX
    assert "returnFromCommonBoxEditor" in INDEX
    assert "识别结果：{{ draft.items?.length || 0 }} 行" in INDEX
    assert "请核对行数、存货编码和数量" in INDEX
    assert "高级匹配详情" in INDEX
    assert 'v-if="draft._show_advanced_details"' in INDEX


def test_production_history_has_admin_reversal_and_inventory_deep_link() -> None:
    assert "撤销生产确认" in INDEX
    assert 'user?.role===\'admin\' && row.can_revert' in INDEX
    assert "/api/production/completions/${row.id}/revert" in INDEX
    assert "任务已退回待生产确认" in INDEX
    assert "openProductionInventory" in INDEX
    assert 'params.set("lot_id"' in INDEX


def test_warehouse_uses_area_then_location_and_ledger_map_linkage() -> None:
    for element_id in (
        'id="areaFilter"',
        'id="fgArea"',
        'id="siArea"',
        'id="lotEditArea"',
        'id="locationFloor"',
        'id="locationArea"',
    ):
        assert element_id in WAREHOUSE
    assert "refreshLocationCascade" in WAREHOUSE
    assert "selectLocationCascade" in WAREHOUSE
    assert "已联动三楼平面图" in WAREHOUSE
    assert "applyWarehouseDeepLink" in WAREHOUSE
    assert "每行只需选择客户、存货编码和数量" in WAREHOUSE
