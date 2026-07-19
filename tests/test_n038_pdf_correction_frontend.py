from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_pdf_draft_correction_entry_is_permission_gated_and_customer_warning_is_persistent() -> None:
    assert "canSubmitPdfCorrection" in INDEX
    assert 'hasPermission("pdf_training.manage")' in INDEX
    assert "识别有误？提交改进" in INDEX
    assert "尚未选择确认客户。请先选择客户；提交改进和保存正式订单都需要确认客户。" in INDEX
    assert "先选择确认客户后才能提交" in INDEX
    assert "draft._correction_error" in INDEX


def test_pdf_draft_correction_form_has_plain_fields_and_independent_multipart_submit() -> None:
    for label in ("客户名称", "客户单号", "下单日期", "交货日期", "改进说明", "存货编码", "产品名称", "规格", "数量", "单位", "单价", "金额"):
        assert label in INDEX
    assert "新增明细行" in INDEX
    assert "removePdfCorrectionItem" in INDEX
    assert "cancelPdfCorrection" in INDEX
    assert 'axios.post("/api/pdf-training/samples/submit-correction", form' in INDEX
    assert 'form.append("file", file, file.name)' in INDEX
    assert 'form.append("customer_id", String(draft.matched_customer_id))' in INDEX
    assert 'form.append("ground_truth_json", JSON.stringify(groundTruth))' in INDEX
    assert 'form.append("notes", String(formState.notes || "人工核对订单 PDF 识别结果")' in INDEX
    assert "待管理员复核；不会自动创建订单，管理员验证后才用于后续识别。" in INDEX

    submit_block = INDEX.split("async submitPdfCorrection(draft)", 1)[1].split(
        "onOrderPdfFileChange(event)", 1
    )[0]
    assert "draft.confirmed = true" not in submit_block
    assert "saveConfirmedImportDrafts" not in submit_block
    assert "_correction_success" in submit_block


def test_pdf_template_lifecycle_is_collapsed_without_removing_legacy_controls() -> None:
    assert '<details class="pdf-admin-advanced"' in INDEX
    assert "管理员高级设置（一般操作无需使用）" in INDEX
    assert "客户 PDF 模板生命周期" in INDEX
    for legacy_control in (
        "clonePdfTemplate(row)",
        "runPdfTemplateDryRun(row)",
        "activatePdfTemplate(row)",
        "retirePdfTemplate(row)",
        "deletePdfTemplate(row)",
    ):
        assert legacy_control in INDEX


def test_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend syntax validation"
    scripts = [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL) if script.strip()]
    assert len(scripts) == 1
    target = tmp_path / "n038-index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
