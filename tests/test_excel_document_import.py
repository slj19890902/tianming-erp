from collections import Counter
from pathlib import Path

import pytest

from app.services.excel_document_import import ExcelImportError, parse_excel_document


SAMPLE_ROOT = Path("C:/Users/Administrator/Documents/xwechat_files/kimiyf_0342/msg/file/2026-09")
SAMPLES = {
    "yg_delivery": ("捷太格特送货单20260928.xls", "pre_delivery", 42, 7895),
    "yg_order": ("20260922天明订单.xlsx", "order", 42, 7895),
    "gy_delivery": ("光洋送货单20260922.xls", "pre_delivery", 21, 18224),
    "yg_order_2": ("20260916同昌订单.xlsx", "order", 37, None),
}


@pytest.mark.parametrize("key", SAMPLES)
def test_real_excel_samples_are_structurally_read_only(key):
    filename, document_type, expected_rows, expected_total = SAMPLES[key]
    path = SAMPLE_ROOT / filename
    before = path.read_bytes()
    parsed = parse_excel_document(before, filename, document_type=document_type)
    assert len(parsed.rows) == expected_rows
    assert parsed.customer_code == ("GY" if key == "gy_delivery" else "YG")
    if expected_total is not None:
        assert sum(row.order_quantity or row.requested_quantity for row in parsed.rows) == expected_total
    assert path.read_bytes() == before


def test_yanguang_order_and_predelivery_correspond_without_creating_business_data():
    order_name, _, _, _ = SAMPLES["yg_order"]
    delivery_name, _, _, _ = SAMPLES["yg_delivery"]
    order = parse_excel_document((SAMPLE_ROOT / order_name).read_bytes(), order_name, document_type="order")
    delivery = parse_excel_document((SAMPLE_ROOT / delivery_name).read_bytes(), delivery_name, document_type="pre_delivery")
    assert Counter((row.stock_code, row.order_quantity) for row in order.rows) == Counter(
        (row.stock_code, row.requested_quantity) for row in delivery.rows
    )
    assert sum(row.order_quantity or 0 for row in order.rows) == 7895


def test_codes_and_source_rows_are_not_coerced_or_deduplicated():
    order_name, _, _, _ = SAMPLES["yg_order"]
    gy_name, _, _, _ = SAMPLES["gy_delivery"]
    order = parse_excel_document((SAMPLE_ROOT / order_name).read_bytes(), order_name, document_type="order")
    gy = parse_excel_document((SAMPLE_ROOT / gy_name).read_bytes(), gy_name, document_type="pre_delivery")
    assert len({row.source_row for row in order.rows}) == 42
    assert len({row.source_no for row in order.rows}) < 42  # duplicate NO is evidence, not identity
    assert any((row.drawing_number or "").startswith("0") for row in order.rows)
    assert any(row.stock_code == "C80010080" for row in gy.rows)
    assert all(row.source_row > 0 and row.sheet for row in order.rows + gy.rows)


def test_customer_is_inferred_from_workbook_not_filename_and_po_is_not_invented():
    filename, _, _, _ = SAMPLES["yg_order_2"]
    parsed = parse_excel_document((SAMPLE_ROOT / filename).read_bytes(), filename, document_type="order")
    assert parsed.customer_code == "YG"
    assert parsed.customer_po is None
    assert all(row.customer_po is None for row in parsed.rows)


def test_wrong_customer_and_fake_extension_are_rejected():
    filename, _, _, _ = SAMPLES["yg_order"]
    content = (SAMPLE_ROOT / filename).read_bytes()
    with pytest.raises(ExcelImportError, match="不一致"):
        parse_excel_document(content, filename, document_type="order", expected_customer_code="GY")
    with pytest.raises(ExcelImportError, match="文件签名"):
        parse_excel_document(b"not an xlsx", "fake.xlsx", document_type="order")
