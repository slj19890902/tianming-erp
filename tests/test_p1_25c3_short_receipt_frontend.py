from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_inventory_backed_short_receipt_requires_cascading_return_location() -> None:
    assert "退回放哪里" in INDEX
    assert "receiptLineNeedsReturnLocation" in INDEX
    assert "receiptLocationFloors" in INDEX
    assert "receiptAreasForFloor" in INDEX
    assert "receiptLocationsForArea" in INDEX
    assert "请选择退回货物实际存放库位" in INDEX
    assert "return_location_id:line.return_location_id || null" in INDEX


def test_receipt_ui_keeps_unordered_return_automatic_and_no_extra_confirmation() -> None:
    assert "自动退回原批次" in INDEX
    assert "客户短收退回是否确认" not in INDEX
    assert "退回原因必填" not in INDEX
