from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_existing_customer_po_is_an_advisory_not_a_save_blocker() -> None:
    status_method = INDEX.split("importStatusText(draft) {", 1)[1].split(
        "importCustomerOptions(draft)", 1
    )[0]
    block_method = INDEX.split("importDraftBlockReasons(draft) {", 1)[1].split(
        "importItemMasterBlockReason(item, index)", 1
    )[0]

    assert 'draft.duplicate_status === "existing_po_found"' in status_method
    assert "同单号新来源待确认" in status_method
    assert "existing_po_found" not in block_method
    assert 'draft?.duplicate_status === "duplicate_skipped"' in block_method
    assert "确认保存将新建一张独立 ERP 订单" in INDEX
    assert "查看原订单" in INDEX
    assert "draft.duplicate_order_id" in INDEX
