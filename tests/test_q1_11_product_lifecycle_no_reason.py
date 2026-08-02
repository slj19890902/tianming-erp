from __future__ import annotations

from pathlib import Path

from app.api.products import (
    ProductMutationPayload,
    ProductTrashEmptyPayload,
    SyncFieldsPayload,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_product_lifecycle_payloads_accept_missing_or_blank_reason() -> None:
    assert ProductMutationPayload(expected_version=1).change_reason is None
    assert ProductMutationPayload(expected_version=1, change_reason="   ").change_reason is None
    assert ProductTrashEmptyPayload(expected_versions={1: 2}).change_reason is None
    assert SyncFieldsPayload(fields={"product_name": "新名称"}, expected_version=3).change_reason is None


def test_product_lifecycle_frontend_has_one_confirmation_without_reason_prompt() -> None:
    assert "askMasterMutationReason(" not in INDEX
    assert "请填写本次修改原因" not in INDEX
    assert 'axios.post("/api/master/products/trash/empty",{expected_versions})' in INDEX
    assert "askMasterMutationReason(" not in INDEX
    for label in (
        "确认将产品",
        "确认恢复",
        "确定彻底清理",
        "确定清空产品垃圾站",
        "确认${action}纸箱",
    ):
        assert label in INDEX


def test_product_lifecycle_keeps_version_and_confirmation_gates() -> None:
    assert "expected_version:expectedVersion" in INDEX
    assert "confirmation_token:confirmationToken" in INDEX
    assert 'detail?.code === "MASTER_VERSION_CONFLICT"' in INDEX
    assert 'detail?.code === "MASTER_CHANGE_CONFIRMATION_REQUIRED"' in INDEX
