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
    assert 'if (!this.pageAllowed("warehouse"))' in INDEX
    assert "当前账号没有访问仓库库存管理的权限" in INDEX
    assert 'params.set("lot_id"' in INDEX
    assert 'new URLSearchParams({ embedded:"1", tab:"finished" })' in INDEX
    assert 'this.ensureWarehouseFrame(`/warehouse.html?${params.toString()}`)' in INDEX
    assert 'window.location.href = `/warehouse.html?${params.toString()}`' not in INDEX


def test_order_group_detail_keeps_active_page_else_if_chain_adjacent() -> None:
    detail_start = INDEX.index('<template v-for="row in group.orders" :key="row.id">')
    detail_body_end = INDEX.index("</tbody>", detail_start)
    detail_fragment = INDEX[detail_start:detail_body_end]

    # An extra closing template here closes the active-page branch early. Vue then
    # rejects every following v-else-if with compiler error 30 and leaves #app blank.
    assert detail_fragment.count("</template>") == 1
    assert '<template v-else-if="activePage === \'orders_legacy\'">' in INDEX


def test_warehouse_uses_area_then_location_and_keeps_layout_separate() -> None:
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
    assert "楼层与区域建设进度" in WAREHOUSE
    assert "待布局，禁止入库" in WAREHOUSE
    assert "不会自动出现在三楼或其他平面图" in WAREHOUSE
    assert "applyWarehouseDeepLink" in WAREHOUSE
    assert "选择客户、存货编码和数量后加入当前栈板。" in WAREHOUSE
