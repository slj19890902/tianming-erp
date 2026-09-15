from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_batch_preserves_all_created_orders_and_independent_retries():
    page = (ROOT / 'static/index.html').read_text(encoding='utf-8')
    save = page.split('async saveSupplierRequisitionDraft() {')[1].split('supplierRequisitionSelectionSignature(selections) {')[0]
    assert 'openSupplierOrderBatch(data.created_orders || [])' in save
    assert 'const first = (data.created_orders || [])[0]' not in save
    assert 'supplierPreview.orders' in page
    assert 'supplierPreview.showInternal' in page
    assert 'exportSupplierOrder("pdf")' in page
    assert 'exportSupplierOrder("xlsx")' in page
