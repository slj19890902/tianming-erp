from pathlib import Path

from app.api.pdf_training import TemplateActivationPayload, TemplateRetirePayload


ROOT = Path(__file__).resolve().parents[1]
PDF_API = (ROOT / "app" / "api" / "pdf_training.py").read_text(encoding="utf-8")


def test_template_lifecycle_payloads_accept_missing_and_legacy_reason() -> None:
    assert TemplateActivationPayload().reason is None
    assert TemplateRetirePayload().reason is None
    assert TemplateActivationPayload(reason="完成客户样本核对").reason == "完成客户样本核对"


def test_template_lifecycle_keeps_permissions_dry_run_status_and_audit() -> None:
    assert "require_pdf_training_manage" in PDF_API
    assert "_require_draft(candidate, \"激活\")" in PDF_API
    assert "activation_dry_run(db, candidate)" in PDF_API
    assert 'if not evidence["can_activate"]' in PDF_API
    assert 'template.status != "active"' in PDF_API
    assert 'or "激活PDF订单模板（系统记录）"' in PDF_API
    assert 'or "退役PDF订单模板（系统记录）"' in PDF_API
    assert '"pdf_training.template.activate"' in PDF_API
    assert '"pdf_training.template.retire"' in PDF_API
    assert "_commit_template_change" in PDF_API
