from __future__ import annotations

from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_material_update_previews_before_one_confirmation() -> None:
    assert "prepareMaterialChangeConfirmation" in INDEX
    assert "/api/master/materials/${encodeURIComponent(materialId)}/update-preview" in INDEX
    assert 'else if (masterEntity === "material") await this.prepareMaterialChangeConfirmation(changes);' in INDEX
    assert 'entity:"material"' in INDEX
    assert "confirmationToken:data.confirmation_token || null" in INDEX
    assert "requireAcknowledgement:(Array.isArray(data.warnings) ? data.warnings.length : 0) > 0" in INDEX


def test_material_confirmation_has_no_reason_prompt_or_fabricated_reason() -> None:
    assert '["customer","material"].includes(state.entity)' in INDEX
    assert "masterSingleConfirmNoReasonUpdate" in INDEX
    assert 'url:`/api/master/materials/${row.id}`,row,reasonLabel:false' in INDEX
    assert "if (reason) mutationPayload.change_reason = reason;" in INDEX
    assert 'change_reason:"确认"' not in INDEX
    assert 'change_reason:"同意"' not in INDEX


def test_material_deactivate_keeps_one_visible_confirmation() -> None:
    assert 'confirm(`确认停用材质“${row.code}”吗？`)' in INDEX
    assert 'method:"delete",url:`/api/master/materials/${row.id}`,row,reasonLabel:false' in INDEX
