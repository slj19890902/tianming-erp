from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DELIVERY = (ROOT / "static" / "delivery-print.html").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


DELIVERY_FIELDS = {
    "document_title",
    "address_label",
    "phone_label",
    "fax_label",
    "customer_name_label",
    "delivery_number_label",
    "customer_phone_label",
    "delivery_date_label",
    "customer_address_label",
    "vehicle_number_label",
    "column_sequence",
    "column_customer_po",
    "column_product_code",
    "column_product_name",
    "column_specification",
    "column_unit",
    "column_quantity",
    "column_remarks",
    "page_quantity_label",
    "total_quantity_label",
    "long_remark_placeholder",
    "remark_notes_label",
    "copies_text",
    "delivery_person_label",
    "receiving_unit_label",
    "handler_label",
    "created_at_label",
}

SUPPLIER_FIELDS = {
    "document_title",
    "supplier_label",
    "number_label",
    "date_label",
    "column_sequence",
    "column_board_size",
    "column_crease",
    "column_material",
    "column_quantity",
    "column_remarks",
    "address_label",
    "phone_label",
}


def _inline_scripts(source: str) -> list[str]:
    return [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", source, re.DOTALL
        )
        if script.strip()
    ]


def _check_inline_javascript(source: str, tmp_path: Path, stem: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend syntax validation")
    for index, script in enumerate(_inline_scripts(source), start=1):
        path = tmp_path / f"{stem}-{index}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr


def _supplier_print_section() -> str:
    start = INDEX.index("<!-- 供应商报料单打印预览 -->")
    end = INDEX.index('<div v-else-if="modal.type === \'requisitionMaterial\'">', start)
    return INDEX[start:end]


def test_delivery_uses_shared_editor_and_complete_static_field_catalog() -> None:
    assert '<script src="/static/assets/print-template-editor.js"></script>' in DELIVERY
    assert 'templateKey: "delivery_note"' in DELIVERY
    assert 'root: document.getElementById("sheets")' in DELIVERY
    assert 'toolbar: document.getElementById("printTemplateToolbar")' in DELIVERY
    assert "if (currentDeliveryData) renderDelivery(currentDeliveryData);" in DELIVERY
    assert "onBeforePrint: () =>" in DELIVERY
    assert "window.print();" in DELIVERY

    fields = set(re.findall(r'data-print-edit-key="([a-z0-9_]+)"', DELIVERY))
    assert fields == DELIVERY_FIELDS
    assert 'data-field="senderCompanyName" class="company"' in DELIVERY
    assert 'data-field="senderCompanyName" data-print-edit-key' not in DELIVERY


def test_delivery_applies_saved_text_to_every_clone_before_fit_measurement() -> None:
    create_sheet = DELIVERY[DELIVERY.index("function createSheet()") : DELIVERY.index("function applyPrintProfile")]
    assert "deliveryPrintEditor.applyTo(sheet)" in create_sheet
    assert DELIVERY.index("function createSheet()") < DELIVERY.index("const pageFits =")
    assert "currentDeliveryData = data" in DELIVERY
    assert "if (currentDeliveryData) renderDelivery(currentDeliveryData)" in DELIVERY
    assert 'document.getElementById("errorBox")' in DELIVERY
    assert 'errorBox.hidden = true' in DELIVERY
    assert 'data-print-edit-key="long_remark_placeholder"' in DELIVERY
    assert "deliveryPrintEditor?.value().fields.long_remark_placeholder" in DELIVERY


def test_delivery_business_values_remain_locked() -> None:
    locked_fields = (
        "senderCompanyName",
        "customerName",
        "deliveryNumber",
        "customerPhone",
        "deliveryDate",
        "customerAddress",
        "vehicleNumber",
        "pageQuantity",
        "totalQuantity",
        "createdAt",
        "itemRows",
    )
    for field in locked_fields:
        assert not re.search(
            rf'data-field="{field}"[^>]*data-print-edit-key', DELIVERY
        )


def test_inline_supplier_and_stock_prints_use_separate_template_keys() -> None:
    assert '<script src="/static/assets/print-template-editor.js"></script>' in INDEX
    assert 'return "supplier_purchase_order"' in INDEX
    assert 'return "stock_replenishment_order"' in INDEX
    assert "PrintTemplateEditor.create({" in INDEX
    assert "onPrint: () => this.printSupplierOrderArea()" in INDEX
    assert "handleEscape: false" in INDEX
    assert "destroySupplierPrintTemplateEditor()" in INDEX
    assert "initializeSupplierPrintTemplateEditor()" in INDEX
    assert 'document.body.classList.add("supplier-print-native-active")' in INDEX
    assert 'document.body.classList.remove("supplier-print-native-active")' in INDEX
    assert "body.supplier-print-native-active #supplier-order-print-area" in INDEX

    section = _supplier_print_section()
    fields = set(re.findall(r'data-print-edit-key="([a-z0-9_]+)"', section))
    assert fields == SUPPLIER_FIELDS
    company_heading = re.search(r'<div class="supplier-print-title"><h2>(.*?)</h2>', section)
    assert company_heading
    assert "data-print-edit-key" not in company_heading.group(1)


def test_inline_print_keeps_business_rows_locked_and_window_open() -> None:
    section = _supplier_print_section()
    assert 'data-print-edit-key="supplier_label">TO：</span>{{ modal.data.supplier_name' in section
    assert 'data-print-edit-key="number_label">NO：</span>{{ modal.data.order_number' in section
    assert "line.report_length_mm" in section
    assert "line.requisition_qty" in section
    assert 'v-for="(line,index) in supplierOrderPrintLines(modal.data)"' in section

    method_start = INDEX.index("printSupplierOrderArea() {")
    method_end = INDEX.index("async voidSupplierOrder", method_start)
    method = INDEX[method_start:method_end]
    assert "w.print();" in method
    assert "w.close()" not in method
    assert "setTimeout" not in method


def test_inline_print_dirty_editor_is_guarded_on_close_escape_and_auth_loss() -> None:
    assert "supplierPrintTemplateEditorDirty()" in INDEX
    assert "confirmDiscardSupplierPrintTemplateChanges()" in INDEX
    assert "打印内容有未保存的修改，确定关闭并放弃这些修改吗？" in INDEX
    close_method = INDEX[INDEX.index("closeModal() {") : INDEX.index("async saveModal()", INDEX.index("closeModal() {"))]
    assert "if (!this.confirmDiscardSupplierPrintTemplateChanges()) return false" in close_method
    assert close_method.index("confirmDiscardSupplierPrintTemplateChanges") < close_method.index("destroySupplierPrintTemplateEditor")

    mounted = INDEX[INDEX.index("async mounted() {") : INDEX.index("methods: {", INDEX.index("async mounted() {"))]
    assert 'if (e.key !== "Escape") return' in mounted
    assert "this.closeModal()" in mounted
    assert 'document.addEventListener("keydown", this._modalEscapeHandler)' in mounted
    assert 'document.removeEventListener("keydown", this._modalEscapeHandler)' in mounted

    auth_handler = mounted[mounted.index("window.erpAuthRequired") : mounted.index("window.erpForbidden")]
    assert auth_handler.index("destroySupplierPrintTemplateEditor") < auth_handler.index("this.user = null")
    logout = INDEX[INDEX.index("async logout() {") : INDEX.index("resetPagePerformanceState() {", INDEX.index("async logout() {"))]
    assert logout.index("destroySupplierPrintTemplateEditor") < logout.index("this.user = null")
    assert "this.modal = null" in auth_handler
    assert "this.modal = null" in logout


def test_complex_print_page_inline_javascript_is_valid(tmp_path: Path) -> None:
    _check_inline_javascript(DELIVERY, tmp_path, "delivery-print")
    _check_inline_javascript(INDEX, tmp_path, "index")
