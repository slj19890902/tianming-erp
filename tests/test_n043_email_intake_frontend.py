from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    assert start in INDEX, f"missing start marker: {start}"
    assert end in INDEX, f"missing end marker: {end}"
    return INDEX.split(start, 1)[1].split(end, 1)[0]


def test_email_intake_entry_is_desktop_only_and_permission_gated() -> None:
    assert 'v-if="canViewEmailIntake" class="btn desktop-only" @click="openEmailOrderIntake"' in INDEX
    assert "邮箱订单草稿" in INDEX
    assert ".desktop-only { display:none !important; }" in INDEX
    assert 'canViewEmailIntake() { return this.hasPermission("email_intake.view"); }' in INDEX
    assert 'canManageEmailIntake() { return this.hasPermission("email_intake.manage"); }' in INDEX


def test_email_intake_modal_has_safe_status_mapping_and_draft_tables() -> None:
    block = _between("modal.type === 'emailOrderIntake'", "modal.type === 'orderPdfImport'")
    for text in (
        "邮件只进入草稿，人工确认后才会保存正式订单",
        "已配置",
        "未配置",
        "最近轮询结果",
        "发件人 → 客户映射",
        "完整发件人邮箱",
        "识别草稿",
        "收件记录",
        "邮件时间",
        "附件名",
        "错误 / 提示",
    ):
        assert text in block
    assert 'v-if="canManageEmailIntake" class="btn primary"' in block
    assert 'v-if="canManageEmailIntake" class="section-card"' in block


def test_email_intake_frontend_never_exposes_mailbox_secret_fields() -> None:
    block = _between("modal.type === 'emailOrderIntake'", "modal.type === 'orderPdfImport'")
    lowered = block.lower()
    assert 'type="password"' not in lowered
    assert "password_file" not in lowered
    assert "secret_path" not in lowered
    assert "credential_path" not in lowered
    assert "邮箱服务器、账号和密码由系统管理员在服务器配置" in block
    assert "需由系统管理员在服务器配置" in block


def test_email_intake_uses_only_declared_api_contracts() -> None:
    for route in (
        "/api/email-order-intake/status",
        "/api/email-order-intake/drafts",
        "/api/email-order-intake/messages",
        "/api/email-order-intake/messages/${encodeURIComponent(id)}",
        "/api/email-order-intake/sender-mappings",
        "/api/email-order-intake/poll-once",
        "/api/email-order-intake/drafts/${encodeURIComponent(id)}/retry",
        "/api/email-order-intake/drafts/${encodeURIComponent(id)}/open-preview",
    ):
        assert route in INDEX
    assert "axios.post(\"/api/email-order-intake/sender-mappings\", payload)" in INDEX
    assert "axios.put(`/api/email-order-intake/sender-mappings/${encodeURIComponent(form.id)}`, payload)" in INDEX


def test_sender_mapping_requires_complete_email_and_supports_edit_and_disable() -> None:
    assert "请输入完整、有效的发件人邮箱" in INDEX
    assert "editEmailSenderMapping(row)" in INDEX
    assert "toggleEmailSenderMapping(row)" in INDEX
    assert "发件人映射已停用" in INDEX
    assert "不支持域名模糊映射" in INDEX
    assert "sender_email:sender" in INDEX


def test_all_required_chinese_draft_statuses_are_present() -> None:
    for label in (
        "待客户匹配",
        "待人工确认",
        "可打开识别草稿",
        "Excel待N042模块",
        "重复附件",
        "解析失败",
        "已忽略",
        "已生成正式订单",
    ):
        assert label in INDEX


def test_pdf_email_draft_reuses_existing_order_pdf_editor_without_creating_order() -> None:
    block = _between("async openEmailIntakePreview(sourceDraft)", "openOrder() {")
    assert "/api/email-order-intake/drafts/${encodeURIComponent(id)}/open-preview" in block
    assert 'this.orderImportSource = "email"' in block
    assert "this.orderImportDrafts = [draft]" in block
    assert 'this.modal = { type:"orderPdfImport", title:"邮箱订单识别草稿" }' in block
    assert "canCreateOrders" in block
    assert "/api/orders" not in block
    assert "saveConfirmedImportDrafts" not in block
    assert "this.orderImportEmailDraftId = id" in block


def test_excel_email_draft_is_disabled_until_n042_resumes() -> None:
    block = _between("modal.type === 'emailOrderIntake'", "modal.type === 'orderPdfImport'")
    assert 'v-else class="btn small" disabled' in block
    assert "等待新振Excel模块优化完成" in block
    assert "emailDraftIsExcel" in block


def test_needs_mapping_can_retry_after_admin_adds_sender_mapping() -> None:
    block = _between("emailDraftCanRetry(draft) {", "emailMappingIsActive(row)")
    assert '"needs_mapping"' in block
    assert '"pending_customer_match"' in block
    assert "补充发件人映射，再点击重试" in INDEX


def test_duplicate_attachment_warning_does_not_block_opening_pdf_draft() -> None:
    modal = _between("modal.type === 'emailOrderIntake'", "modal.type === 'orderPdfImport'")
    message = _between("emailDraftIsDuplicate(draft) {", "emailDraftCanRetry(draft) {")
    assert "is_duplicate_content" in message
    assert "duplicate_of_attachment_id" in message
    assert "重复附件：系统已保留草稿，不影响打开核对。" in message
    assert 'v-if="!emailDraftIsExcel(draft)"' in modal
    assert "emailDraftIsDuplicate(draft)" not in _between('v-if="!emailDraftIsExcel(draft)"', "</button>")


def test_status_area_shows_poll_time_result_and_summary() -> None:
    modal = _between("modal.type === 'emailOrderIntake'", "modal.type === 'orderPdfImport'")
    assert "最近轮询时间" in modal
    assert "最近轮询结果" in modal
    assert "轮询摘要" in modal
    assert "emailIntakeLastPollTimeText()" in modal
    assert "emailIntakePollStatusText()" in modal
    assert "emailIntakePollSummary()" in modal
    assert "last_poll_counts:status.last_poll_counts" in INDEX


def test_draft_rows_show_source_metadata_and_error_summary() -> None:
    modal = _between("modal.type === 'emailOrderIntake'", "modal.type === 'orderPdfImport'")
    for heading in ("发件人", "主题", "附件名", "客户", "错误 / 提示"):
        assert heading in modal
    assert "emailDraftSender(draft)" in modal
    assert "draft.subject || draft.email_subject || draft.message?.subject" in modal
    assert "emailDraftAttachmentName(draft)" in modal
    assert "emailDraftCustomerName(draft)" in modal
    assert "emailDraftMessage(draft)" in modal
    for field in ("error_message", "last_error", "warning", "hint"):
        assert f"draft?.{field}" in INDEX


def test_message_history_exposes_attachment_failures_without_hiding_unsupported_mail() -> None:
    modal = _between("modal.type === 'emailOrderIntake'", "modal.type === 'orderPdfImport'")
    for text in (
        "所有邮件都有记录",
        "系统不能替你验证发件人身份",
        "收件时间",
        "邮件状态",
        "尝试次数",
        "查看附件",
        "是否重复",
        "错误原因",
    ):
        assert text in modal
    assert "toggleEmailMessageDetail(message)" in modal
    assert 'axios.get(`/api/email-order-intake/messages/${encodeURIComponent(id)}`)' in INDEX
    assert "attachment.error_message" in modal


def test_email_draft_id_is_carried_through_both_order_save_paths() -> None:
    assert INDEX.count('email_intake_draft_id:this.orderImportSource === "email"') == 1
    assert INDEX.count('email_intake_draft_id: this.orderImportSource === "email"') == 1
    assert "emailDraftIsConverted(draft)" in INDEX
    assert "该草稿已经生成正式订单" in INDEX


def test_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend syntax validation"
    scripts = [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL) if script.strip()]
    assert scripts, "static/index.html should contain an inline Vue script"
    target = tmp_path / "n043-index-inline.js"
    target.write_text(max(scripts, key=len), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
