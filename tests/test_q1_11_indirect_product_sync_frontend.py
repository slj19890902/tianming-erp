from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_order_and_requisition_sync_do_not_request_or_fabricate_reason() -> None:
    assert "同步原因 *" not in INDEX
    assert "同步本页其他常用箱资料时必须填写原因" not in INDEX
    assert 'product_change_reason: "报料人工修改材质并同步常用箱"' not in INDEX
    assert 'product_change_reason:syncProduct ? "报料人工修改材质并同步常用箱" : null' not in INDEX
    assert 'product_change_reason = "订单编辑修改箱型并同步常用箱"' not in INDEX


def test_abnormal_indirect_sync_confirms_once_and_retries_automatically() -> None:
    assert "async putIndirectProductUpdate(url, payload, form, actionLabel)" in INDEX
    assert 'detail?.code === "MASTER_CHANGE_CONFIRMATION_REQUIRED"' in INDEX
    assert "return await send({...payload, product_confirmation_token:token})" in INDEX
    assert "请先勾选已核对异常修改" not in INDEX
    assert "当前单据和关联常用箱" in INDEX


def test_indirect_sync_keeps_version_and_source_selection_fields() -> None:
    assert "product_expected_version = Number(this.orderItemForm.product_version)" in INDEX
    assert "product_expected_version:syncProduct ? Number(form.product_version) : null" in INDEX
    assert "selection_reason:String(form.selection_reason || \"\").trim() || null" in INDEX
    assert 'detail?.code === "MASTER_VERSION_CONFLICT"' in INDEX
