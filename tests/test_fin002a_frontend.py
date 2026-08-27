from __future__ import annotations

from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_customer_profile_exposes_price_tax_mode_and_complete_tax_fields() -> None:
    for label in (
        "常用箱与订单单价口径",
        "含税价",
        "未税价",
        "默认税率",
        "开票地址",
        "开票电话",
        "开户行",
        "确认状态",
    ):
        assert label in INDEX
    assert 'v-model="customerInvoiceProfile.price_tax_mode"' in INDEX
    assert "price_tax_mode:form.price_tax_mode" in INDEX
    assert "default_tax_rate:Number(form.default_tax_rate)" in INDEX


def test_common_box_price_label_uses_selected_customer_mode_without_conversion() -> None:
    assert "commonBoxPriceLabel" in INDEX
    assert "含税单价（元/PCS）" in INDEX
    assert "未税单价（元/PCS）" in INDEX
    assert "selectedProductCustomer?.price_tax_mode" in INDEX
    assert 'v-model="productForm.sale_unit_price"' in INDEX
    assert "sale_unit_price_no_tax" not in INDEX[INDEX.index("commonBoxPriceLabel") : INDEX.index("commonBoxPriceLabel") + 1200]


def test_default_invoice_rule_uses_backend_field_names_and_product_code_source() -> None:
    assert 'tax_classification_code:"1060105010000000000"' in INDEX
    assert 'unit:"PCS"' in INDEX
    assert 'spec_source:"product_code_snapshot"' in INDEX
    assert "tax_classification_code:String(form.tax_classification_code).trim()" in INDEX
    assert "tax_category_code:String(form.tax_category_code).trim()" not in INDEX


def test_seller_maintenance_uses_complete_backend_contract() -> None:
    for model in (
        'invoiceSellerForm.seller_code',
        'invoiceSellerForm.address',
        'invoiceSellerForm.phone',
        'invoiceSellerForm.bank_name',
        'invoiceSellerForm.bank_account',
        'invoiceSellerForm.is_enabled',
        'invoiceSellerForm.confirmation_status',
    ):
        assert model in INDEX
    assert "seller_code:String(form.seller_code).trim()" in INDEX
    assert "is_enabled:form.is_enabled !== false" in INDEX
    assert "address_phone:String(form.address_phone" not in INDEX
