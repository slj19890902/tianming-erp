from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_n016_gold_review_status_and_admin_actions_are_present() -> None:
    assert "gold_review_status" in INDEX
    assert "待复核" in INDEX
    assert "已批准" in INDEX
    assert "已拒绝" in INDEX
    assert "gold_reviewed_by" in INDEX
    assert "gold_reviewed_at" in INDEX
    assert "gold_review_note" in INDEX
    assert "批准为金样本" in INDEX
    assert "完整人工标注" in INDEX
    assert "/api/pdf-training/samples/${safeSampleId}/gold-review" in INDEX
    assert "status === \"approved\"" in INDEX
    assert "status === \"rejected\"" in INDEX
    assert "保存人工标注后，金样本复核状态会刷新为“待复核”" in INDEX


def test_n016_template_lifecycle_list_has_daily_filter_and_audit_columns() -> None:
    assert "/api/pdf-training/templates" in INDEX
    assert "客户 PDF 模板生命周期" in INDEX
    assert "active（生效）" in INDEX
    assert "draft（草稿）" in INDEX
    assert "retired（已退役）" in INDEX
    assert "更新时间" in INDEX
    assert "操作者" in INDEX
    assert "日常：active + draft" in INDEX
    assert "默认隐藏历史 retired 模板" in INDEX
    assert "pdfTemplateRows()" in INDEX
    assert "字段映射 JSON（明细正则捕获组）" in INDEX
    assert '{"field_mapping":{"product_code":"sku","quantity":"qty","unit_price":6}}' in INDEX


def test_n016_template_operation_matrix_uses_one_confirmation_without_reason() -> None:
    assert "@click=\"openPdfTemplateEditor(row)\"" in INDEX
    assert "@click=\"clonePdfTemplate(row)\"" in INDEX
    assert "@click=\"runPdfTemplateDryRun(row)\"" in INDEX
    assert "@click=\"activatePdfTemplate(row)\"" in INDEX
    assert "@click=\"retirePdfTemplate(row)\"" in INDEX
    assert "@click=\"deletePdfTemplate(row)\"" in INDEX
    assert "row.status !== 'draft'" in INDEX
    assert "pdfTemplateFormReadonly" in INDEX
    assert "只有 draft 模板可以激活" in INDEX
    activate = INDEX.split("async activatePdfTemplate(template)", 1)[1].split(
        "async retirePdfTemplate(template)", 1
    )[0]
    retire = INDEX.split("async retirePdfTemplate(template)", 1)[1].split(
        "async deletePdfTemplate(template)", 1
    )[0]
    assert activate.count("window.confirm(") == 1
    assert retire.count("window.confirm(") == 1
    assert "window.prompt(" not in activate
    assert "window.prompt(" not in retire
    assert "reason:" not in activate
    assert "reason:" not in retire
    assert "确认激活模板" in INDEX
    assert "确认退役 active 模板" in INDEX
    assert "/clone-draft" in INDEX
    assert "/replay" in INDEX
    assert "/activate" in INDEX
    assert "/retire" in INDEX
    assert "axios.delete(`/api/pdf-training/templates/${template.id}`)" in INDEX
    assert "canAdmin && row.status === 'draft'" in INDEX
    assert "if (!this.canAdmin) return;" in INDEX
    assert 'wasCreating ? "draft 模板已创建" : "draft 模板已保存"' in INDEX


def test_n016_dry_run_renders_sample_scores_errors_average_and_activation_gate() -> None:
    assert "只读验证结果" in INDEX
    assert "样本数：" in INDEX
    assert "平均分：" in INDEX
    assert "逐样本" not in INDEX or "sample_results" in INDEX
    assert "sample_results" in INDEX
    assert "错误 / 关键字段" in INDEX
    assert "可激活" in INDEX
    assert "不可激活原因" in INDEX
    assert "pdfTemplateCanActivate(row)" in INDEX
    assert "回放同客户金样本" in INDEX
    assert "pdfTemplateErrorMessage" in INDEX


def test_gold_learning_loop_and_ground_truth_source_fields_are_connected() -> None:
    assert "持续学习闭环" in INDEX
    assert "生成/刷新规则草稿并回放" in INDEX
    assert "一键启用" in INDEX
    assert "/learning-loop" in INDEX
    assert "learning_replay?.can_activate" in INDEX
    assert "不会自动创建、保存或覆盖正式订单" in INDEX
    build = INDEX.split("buildPdfGroundTruthJsonFromForm()", 1)[1].split(
        "pdfGroundTruthPreviewText()", 1
    )[0]
    assert "Number.parseInt(item.line_no, 10) || index + 1" in build
    assert "delivery_date: String(item.delivery_date || \"\").trim()" in build


def test_n016_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend syntax validation"
    scripts = [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL) if script.strip()]
    assert len(scripts) == 1
    target = tmp_path / "n016-index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
