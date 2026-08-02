from __future__ import annotations

import inspect
from pathlib import Path

from app.api.products import ProductBOMUpdatePayload
from app.services.composite_bom import replace_product_bom


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_bom_payload_accepts_missing_blank_or_null_reason() -> None:
    base = {"expected_version": 1, "components": []}
    assert ProductBOMUpdatePayload(**base).change_reason is None
    assert ProductBOMUpdatePayload(**base, change_reason="   ").change_reason is None
    assert ProductBOMUpdatePayload(**base, change_reason=None).change_reason is None


def test_bom_editor_uses_one_confirmation_without_fabricated_reason() -> None:
    assert "confirmProductBomSave()" in INDEX
    assert "只影响以后新建订单，不修改旧订单和旧报料" in INDEX
    assert "this.masterPendingSaveOptions = {_bom_confirmed:true}" in INDEX
    assert 'change_reason: "维护父产品内部 BOM"' not in INDEX
    assert "bomPayload(expectedVersion=null)" in INDEX


def test_product_and_bom_combined_save_passes_latest_product_version() -> None:
    assert "saveProductBom(saved.id, saved.version ?? null)" in INDEX
    assert "expected_version: expectedVersion ?? fields.expected_version ?? 1" in INDEX


def test_bom_service_keeps_no_change_zero_write_and_optional_audit_reason() -> None:
    source = inspect.getsource(replace_product_bom)
    assert 'bool(before["is_composite"]) == bool(normalized)' in source
    assert "return before" in source
    assert 'reason=(change_reason or "").strip() or None' in source
    assert '"reason": (change_reason or "").strip() or None' in source
