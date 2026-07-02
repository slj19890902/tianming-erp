from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
INCOMING = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")


def test_phase12_customer_and_product_uat_controls_are_present() -> None:
    assert "显示已停用客户" in INDEX
    assert "delivery_method" in INDEX
    assert "信用额度</label>" not in INDEX
    assert "customer-group-title" in INDEX
    assert "产品图纸" in INDEX
    # v0.19.2 下一轮：产品材质选择器统一为「材质供应商 + 可搜索材质下拉」，
    # 标签由「材质（克重）」改为「材质（代码｜供应商｜克重｜报价）」。
    assert "材质供应商" in INDEX
    assert "材质（代码｜供应商｜克重｜报价）" in INDEX


def test_product_inactive_switch_and_toggle_are_available() -> None:
    assert "showInactiveProducts" in INDEX
    assert "toggleProductStatus" in INDEX
    assert "显示已停用纸箱" in INDEX


def test_inactive_customer_has_reenable_action() -> None:
    assert "toggleCustomerStatus" in INDEX
    assert "/api/master/customers/${row.id}/status" in INDEX
    assert 'row.is_active ? "停用" : "启用"' in INDEX


def test_phase12_bulk_selection_and_exports_are_present() -> None:
    assert "toggleAllRequisition" in INDEX
    assert "toggleAllDelivery" in INDEX
    assert "toggleAllStatement" in INDEX
    assert "导出 Excel" in INDEX
    assert "/export" in INDEX


def test_supplier_schedule_ui_is_removed_and_wms_is_direct() -> None:
    assert "登记排单" not in INDEX
    assert "供应商预计到达" not in INCOMING
    assert "确认入库" in INCOMING


def test_modal_does_not_use_internal_scroll_container() -> None:
    assert "width: min(1400px, 98vw); overflow: visible;" in INDEX
    assert "max-height: 92vh; overflow: auto;" not in INDEX


def test_requisition_print_page_is_registered() -> None:
    from app.main import app

    assert any(
        route.path == "/requisition-print.html" for route in app.routes
    )


def test_incoming_pending_cards_show_cardboard_requisition_size() -> None:
    assert "报料尺寸" in INCOMING
    assert "item.cardboard_len" in INCOMING
    assert "item.cardboard_width" in INCOMING
    # v0.22.1 阶段 1A Task B：压线尺寸需与报料尺寸同样在卡片主视觉显示
    assert "压线尺寸" in INCOMING
    assert "item.snapshot_crease_left_mm" in INCOMING
    assert "item.snapshot_crease_middle_mm" in INCOMING
    assert "item.snapshot_crease_right_mm" in INCOMING


def test_product_drawing_upload_and_mobile_page_support_pdf() -> None:
    assert "application/pdf,.pdf" in INDEX
    assert "PDF 图纸" in INDEX
    assert "isPdfDrawing" in INDEX
    assert "打开 PDF 图纸" in INCOMING
    assert "item.drawing_is_pdf" in INCOMING
    assert "item.drawing_path" in INCOMING


def test_desktop_and_mobile_incoming_layout_support_editable_quantity() -> None:
    assert 'activePage === \'incoming\'' in INDEX
    assert "仓库来料入库" in INDEX
    assert "incomingPending" in INDEX
    assert "received_quantity:quantity" in INDEX
    assert "客户 · 存货编码 · 产品名称" in INCOMING
    assert "报料尺寸" in INCOMING
    assert "压线尺寸" in INCOMING
    assert 'data-quantity="${item.item_id}"' in INCOMING
    assert "item.requisition_date" in INCOMING
