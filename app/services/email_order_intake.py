from __future__ import annotations

import hashlib
import imaplib
import json
import os
import re
import secrets
import socket
import ssl
import struct
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, load_settings
from app.core.time_contract import utc_now_naive
from app.models.customer import Customer
from app.models.email_order_intake import (
    EmailOrderIntakeAttachment,
    EmailOrderIntakeDraft,
    EmailOrderIntakeMessage,
    EmailOrderIntakePollState,
    EmailOrderSenderMapping,
)
from app.services.order_pdf_import import PdfParseError, match_import_draft
from app.services.pdf_customer_templates import load_active_pdf_template_rules
from app.services.pdf_parse_pipeline import PdfParsePipelineError, parse_pdf_bytes


ALLOWED_ATTACHMENT_TYPES = {".pdf": "pdf", ".xls": "xls", ".xlsx": "xlsx"}
# The Windows task has a 20-minute execution limit. A lock left behind by an
# interrupted process is therefore safe to reclaim after 45 minutes, while a
# legitimately running task remains protected from the next 30-minute trigger.
POLL_LOCK_STALE_SECONDS = 45 * 60
POLL_RECLAIM_LOCK_STALE_SECONDS = 60
XLSX_SIGNATURE_MEMBER_SCAN_LIMIT = 10_000


class EmailIntakeError(RuntimeError):
    pass


class EmailIntakeBusyError(EmailIntakeError):
    pass


def _zip_declared_member_count(content: bytes) -> int | None:
    """Read the classic ZIP EOCD count before ZipFile builds a full file list."""

    # EOCD is at least 22 bytes and its optional comment is at most 65535.
    start = max(0, len(content) - (65_535 + 22))
    offset = content.rfind(b"PK\x05\x06", start)
    if offset < 0 or offset + 22 > len(content):
        return None
    try:
        (
            _signature,
            disk_number,
            central_directory_disk,
            entries_on_disk,
            entries_total,
            _central_directory_size,
            _central_directory_offset,
            comment_length,
        ) = struct.unpack_from("<4s4H2LH", content, offset)
    except struct.error:
        return None
    if disk_number != 0 or central_directory_disk != 0:
        return None
    if entries_on_disk != entries_total:
        return None
    if entries_total == 0xFFFF:  # ZIP64 is unnecessary for an order workbook.
        return None
    if offset + 22 + comment_length != len(content):
        return None
    return int(entries_total)


def _attachment_content_is_valid(file_type: str, content: bytes) -> bool:
    """Reject extension-only masquerading before content is persisted."""

    if file_type == "pdf":
        return content[:1024].lstrip().startswith(b"%PDF-")
    if file_type == "xls":
        return content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if file_type == "xlsx":
        if not content.startswith(b"PK\x03\x04"):
            return False
        declared_members = _zip_declared_member_count(content)
        if (
            declared_members is None
            or declared_members > XLSX_SIGNATURE_MEMBER_SCAN_LIMIT
        ):
            return False
        try:
            with zipfile.ZipFile(BytesIO(content)) as workbook:
                has_content_types = False
                has_workbook_content = False
                for index, member in enumerate(workbook.filelist, start=1):
                    if index > XLSX_SIGNATURE_MEMBER_SCAN_LIMIT:
                        return False
                    name = member.filename
                    has_content_types = has_content_types or name == "[Content_Types].xml"
                    has_workbook_content = has_workbook_content or name.startswith("xl/")
        except (OSError, zipfile.BadZipFile):
            return False
        return has_content_types and has_workbook_content
    return False


def _xlsx_resource_error(content: bytes, settings: Settings) -> str | None:
    """Inspect ZIP metadata only; never extract an untrusted workbook."""

    declared_members = _zip_declared_member_count(content)
    if declared_members is None:
        return "XLSX 文件无法安全读取"
    if declared_members > settings.email_intake_xlsx_max_zip_members:
        return "XLSX ZIP 成员数量超过安全限制"
    try:
        with zipfile.ZipFile(BytesIO(content)) as workbook:
            total_uncompressed = 0
            total_compressed = 0
            has_content_types = False
            has_workbook_content = False
            for index, member in enumerate(workbook.filelist, start=1):
                if index > settings.email_intake_xlsx_max_zip_members:
                    return "XLSX ZIP 成员数量超过安全限制"
                name = member.filename
                has_content_types = has_content_types or name == "[Content_Types].xml"
                has_workbook_content = has_workbook_content or name.startswith("xl/")
                if member.flag_bits & 0x1:
                    return "XLSX 不允许加密 ZIP 成员"
                total_uncompressed += member.file_size
                total_compressed += member.compress_size
                if total_uncompressed > settings.email_intake_xlsx_max_uncompressed_bytes:
                    return "XLSX 解压后累计大小超过安全限制"
                if member.file_size and (
                    not member.compress_size
                    or member.file_size
                    > member.compress_size
                    * settings.email_intake_xlsx_max_compression_ratio
                ):
                    return "XLSX 压缩比超过安全限制"
    except (OSError, zipfile.BadZipFile):
        return "XLSX 文件无法安全读取"
    if not has_content_types or not has_workbook_content:
        return "附件内容与扩展名不匹配"
    if total_uncompressed and (
        not total_compressed
        or total_uncompressed
        > total_compressed * settings.email_intake_xlsx_max_compression_ratio
    ):
        return "XLSX 压缩比超过安全限制"
    return None


def _pdf_resource_error(content: bytes, settings: Settings) -> str | None:
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if len(reader.pages) > settings.email_intake_max_pdf_pages:
            return "PDF 页数超过安全限制"
    except Exception:
        return "PDF 无法安全解析，请检查文件后重试"
    return None


def _attachment_resource_error(
    file_type: str, content: bytes, settings: Settings
) -> str | None:
    if file_type == "xlsx":
        return _xlsx_resource_error(content, settings)
    if not _attachment_content_is_valid(file_type, content):
        return "附件内容与扩展名不匹配"
    if file_type == "pdf":
        return _pdf_resource_error(content, settings)
    return None


def normalize_sender_email(value: str | None) -> str:
    address = parseaddr(value or "")[1].strip().casefold()
    if (
        not address
        or len(address) > 320
        or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address) is None
    ):
        return ""
    return address


def redact_sender_email(value: str | None) -> str:
    address = normalize_sender_email(value)
    if not address:
        return "unknown"
    local, domain = address.rsplit("@", 1)
    return f"{local[:1]}***@{domain}"


def _safe_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _without_preview_token(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _without_preview_token(item)
            for key, item in value.items()
            if str(key) != "preview_safety_token"
        }
    if isinstance(value, list):
        return [_without_preview_token(item) for item in value]
    return value


def _received_datetime(message: Message) -> datetime | None:
    try:
        value = parsedate_to_datetime(str(message.get("Date") or ""))
    except (TypeError, ValueError, OverflowError):
        return None
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _attachment_filename(part: Message, index: int) -> str:
    filename = str(part.get_filename() or "").strip()
    if not filename:
        filename = f"attachment-{index}"
    return Path(filename.replace("\\", "/")).name[:500] or f"attachment-{index}"


def _content_addressed_relative_path(digest: str, file_type: str) -> str:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise EmailIntakeError("附件摘要格式无效")
    if file_type not in {"pdf", "xls", "xlsx"}:
        raise EmailIntakeError("附件类型不允许保存")
    return PurePosixPath(digest[:2], digest[2:4], f"{digest}.{file_type}").as_posix()


def resolve_stored_attachment_path(storage_path: str, settings: Settings | None = None) -> Path:
    current = settings or load_settings()
    root = current.email_intake_storage_dir.resolve(strict=False)
    relative = PurePosixPath(storage_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise EmailIntakeError("附件存储路径无效")
    target = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise EmailIntakeError("附件存储路径越界") from error
    return target


def read_stored_attachment(
    attachment: EmailOrderIntakeAttachment,
    settings: Settings | None = None,
) -> bytes:
    if not attachment.storage_path:
        raise EmailIntakeError("附件未保存或已被安全拒绝")
    target = resolve_stored_attachment_path(attachment.storage_path, settings)
    try:
        content = target.read_bytes()
    except OSError as error:
        raise EmailIntakeError("附件文件不可读取") from error
    if hashlib.sha256(content).hexdigest() != attachment.file_sha256:
        raise EmailIntakeError("附件校验失败，文件内容已变化")
    return content


def _store_allowed_attachment(
    content: bytes,
    digest: str,
    file_type: str,
    settings: Settings,
) -> str:
    relative = _content_addressed_relative_path(digest, file_type)
    target = resolve_stored_attachment_path(relative, settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise EmailIntakeError("内容寻址附件发生摘要冲突")
    else:
        temporary = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
    return relative


def _active_sender_mapping(
    db: Session, normalized_sender: str
) -> tuple[EmailOrderSenderMapping | None, Customer | None]:
    mapping = db.scalar(
        select(EmailOrderSenderMapping).where(
            EmailOrderSenderMapping.normalized_sender_email == normalized_sender,
            EmailOrderSenderMapping.is_active.is_(True),
        )
    )
    if mapping is None:
        return None, None
    customer = db.scalar(
        select(Customer).where(
            Customer.id == mapping.customer_id,
            Customer.is_active.is_(True),
            Customer.status == "active",
        )
    )
    return mapping, customer


def _parsed_customer_id(db: Session, draft: dict) -> int | None:
    route = draft.get("customer_route")
    if isinstance(route, dict) and route.get("status") == "locked":
        try:
            value = int(route.get("template_customer_id"))
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    matched = match_import_draft(db, draft)
    try:
        value = int(matched.get("matched_customer_id"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _process_pdf_attachment(
    db: Session,
    message: EmailOrderIntakeMessage,
    attachment: EmailOrderIntakeAttachment,
    content: bytes,
    customer: Customer | None,
) -> EmailOrderIntakeDraft:
    try:
        result = parse_pdf_bytes(
            content,
            attachment.filename,
            load_active_pdf_template_rules(db),
        )
        raw_draft = result.draft
        parsed_customer_id = _parsed_customer_id(db, raw_draft)
        if customer is None:
            persisted = raw_draft
            status = "needs_mapping"
        elif parsed_customer_id is not None and parsed_customer_id != customer.id:
            persisted = raw_draft
            status = "needs_confirmation"
            persisted = {**persisted, "email_customer_conflict": True}
        else:
            persisted = match_import_draft(db, raw_draft, customer_id=customer.id)
            status = (
                "review_ready"
                if persisted.get("recognition_status") == "recognized"
                else "needs_confirmation"
            )
        persisted = dict(_without_preview_token(persisted))
        persisted["file_hash"] = attachment.file_sha256
        attachment.status = "parsed"
        return EmailOrderIntakeDraft(
            message_id=message.id,
            attachment_id=attachment.id,
            customer_id=customer.id if customer is not None else None,
            parser_type="pdf",
            parse_method=result.parse_method,
            parse_status=str(persisted.get("parse_status") or "recognized"),
            status=status,
            draft_json=_safe_json(persisted),
            source_name=attachment.filename,
            file_sha256=attachment.file_sha256,
        )
    except (PdfParseError, PdfParsePipelineError) as error:
        attachment.status = "failed"
        attachment.error_message = "PDF 解析失败，请检查文件后重试"
        return EmailOrderIntakeDraft(
            message_id=message.id,
            attachment_id=attachment.id,
            customer_id=customer.id if customer is not None else None,
            parser_type="pdf",
            parse_method="failed",
            parse_status=str(getattr(error, "parse_status", "failed")),
            status="failed",
            source_name=attachment.filename,
            file_sha256=attachment.file_sha256,
            last_error=attachment.error_message,
        )
    except Exception:
        attachment.status = "failed"
        attachment.error_message = "PDF 解析发生内部错误"
        return EmailOrderIntakeDraft(
            message_id=message.id,
            attachment_id=attachment.id,
            customer_id=customer.id if customer is not None else None,
            parser_type="pdf",
            parse_method="failed",
            parse_status="failed",
            status="failed",
            source_name=attachment.filename,
            file_sha256=attachment.file_sha256,
            last_error="PDF 解析发生内部错误",
        )


def _message_status(drafts: list[EmailOrderIntakeDraft], attachments: list[EmailOrderIntakeAttachment]) -> str:
    statuses = {draft.status for draft in drafts}
    if "needs_mapping" in statuses:
        return "needs_mapping"
    if "needs_confirmation" in statuses:
        return "needs_confirmation"
    if "review_ready" in statuses:
        return "review_ready"
    if "waiting_excel_parser" in statuses:
        return "waiting_excel_parser"
    if "failed" in statuses:
        return "failed"
    if any(attachment.status == "failed" for attachment in attachments):
        return "failed"
    if attachments:
        return "ignored"
    return "ignored"


def _persist_rejected_message_uid(
    db: Session,
    *,
    mailbox_key: str,
    uidvalidity: str,
    uid: str,
    raw_message_size: int,
    reason: str,
) -> tuple[EmailOrderIntakeMessage, bool]:
    """Persist a permanent pre-download rejection for reliable UID dedupe."""

    existing = db.scalar(
        select(EmailOrderIntakeMessage).where(
            EmailOrderIntakeMessage.mailbox_key == mailbox_key,
            EmailOrderIntakeMessage.imap_uidvalidity == str(uidvalidity),
            EmailOrderIntakeMessage.imap_uid == str(uid),
        )
    )
    if existing is not None:
        return existing, False
    digest_source = (
        f"rejected-before-download\0{mailbox_key}\0{uidvalidity}\0{uid}\0"
        f"{raw_message_size}\0{reason}"
    ).encode("utf-8")
    now = utc_now_naive()
    row = EmailOrderIntakeMessage(
        mailbox_key=mailbox_key,
        imap_uidvalidity=str(uidvalidity),
        imap_uid=str(uid),
        sender_email="unknown",
        normalized_sender_email="",
        subject="邮件在下载前被安全拒收",
        raw_message_sha256=hashlib.sha256(digest_source).hexdigest(),
        mapped_customer_id=None,
        status="ignored",
        attempt_count=1,
        last_error=reason[:1000],
        processed_at=now,
        updated_at=now,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raced = db.scalar(
            select(EmailOrderIntakeMessage).where(
                EmailOrderIntakeMessage.mailbox_key == mailbox_key,
                EmailOrderIntakeMessage.imap_uidvalidity == str(uidvalidity),
                EmailOrderIntakeMessage.imap_uid == str(uid),
            )
        )
        if raced is None:
            raise
        return raced, False
    db.refresh(row)
    return row, True


def ingest_raw_message(
    db: Session,
    *,
    mailbox_key: str,
    uidvalidity: str,
    uid: str,
    raw_message: bytes,
    settings: Settings | None = None,
) -> tuple[EmailOrderIntakeMessage, bool]:
    current = settings or load_settings()
    existing = db.scalar(
        select(EmailOrderIntakeMessage).where(
            EmailOrderIntakeMessage.mailbox_key == mailbox_key,
            EmailOrderIntakeMessage.imap_uidvalidity == str(uidvalidity),
            EmailOrderIntakeMessage.imap_uid == str(uid),
        )
    )
    if existing is not None:
        return existing, False

    parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
    sender = normalize_sender_email(str(parsed.get("From") or ""))
    _mapping, customer = _active_sender_mapping(db, sender)
    record = EmailOrderIntakeMessage(
        mailbox_key=mailbox_key,
        imap_uidvalidity=str(uidvalidity),
        imap_uid=str(uid),
        message_id_header=str(parsed.get("Message-ID") or "")[:500] or None,
        sender_email=sender or "unknown",
        normalized_sender_email=sender,
        subject=str(parsed.get("Subject") or "")[:500] or None,
        received_at=_received_datetime(parsed),
        raw_message_sha256=hashlib.sha256(raw_message).hexdigest(),
        mapped_customer_id=customer.id if customer is not None else None,
        status="received",
        attempt_count=1,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        # Another worker/process may have inserted the same immutable IMAP UID
        # after our initial lookup.  Recover the winner instead of leaving the
        # caller with a broken SQLAlchemy transaction.
        db.rollback()
        raced = db.scalar(
            select(EmailOrderIntakeMessage).where(
                EmailOrderIntakeMessage.mailbox_key == mailbox_key,
                EmailOrderIntakeMessage.imap_uidvalidity == str(uidvalidity),
                EmailOrderIntakeMessage.imap_uid == str(uid),
            )
        )
        if raced is None:
            raise
        return raced, False

    attachments: list[EmailOrderIntakeAttachment] = []
    drafts: list[EmailOrderIntakeDraft] = []
    part_index = 0
    total_attachment_bytes = 0
    for part in parsed.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() != "attachment" and not part.get_filename():
            continue
        part_index += 1
        filename = _attachment_filename(part, part_index)
        suffix = Path(filename).suffix.casefold()
        file_type = ALLOWED_ATTACHMENT_TYPES.get(suffix, "unsupported")
        attachment_count_exceeded = part_index > current.email_intake_max_attachments
        payload = b"" if attachment_count_exceeded else (part.get_payload(decode=True) or b"")
        total_attachment_bytes += len(payload)
        digest = hashlib.sha256(payload).hexdigest()
        previous_statement = (
            select(EmailOrderIntakeAttachment)
            .join(
                EmailOrderIntakeMessage,
                EmailOrderIntakeMessage.id == EmailOrderIntakeAttachment.message_id,
            )
            .where(EmailOrderIntakeAttachment.file_sha256 == digest)
            .order_by(EmailOrderIntakeAttachment.id)
        )
        if record.mapped_customer_id is None:
            previous_statement = previous_statement.where(
                EmailOrderIntakeMessage.mapped_customer_id.is_(None)
            )
        else:
            previous_statement = previous_statement.where(
                EmailOrderIntakeMessage.mapped_customer_id == record.mapped_customer_id
            )
        previous = db.scalar(previous_statement)
        too_large = len(payload) > current.email_intake_max_attachment_bytes
        total_too_large = (
            total_attachment_bytes > current.email_intake_max_total_attachment_bytes
        )
        resource_error = (
            _attachment_resource_error(file_type, payload, current)
            if file_type != "unsupported"
            and not too_large
            and not total_too_large
            and not attachment_count_exceeded
            else None
        )
        allowed = (
            file_type != "unsupported"
            and not attachment_count_exceeded
            and not too_large
            and not total_too_large
            and resource_error is None
        )
        storage_path = (
            _store_allowed_attachment(payload, digest, file_type, current)
            if allowed
            else None
        )
        attachment = EmailOrderIntakeAttachment(
            message_id=record.id,
            part_index=part_index,
            filename=filename,
            content_type=str(part.get_content_type() or "")[:200] or None,
            byte_size=len(payload),
            file_sha256=digest,
            storage_path=storage_path,
            file_type=file_type,
            is_duplicate_content=previous is not None,
            duplicate_of_attachment_id=previous.id if previous is not None else None,
            status=(
                "stored"
                if allowed
                else ("unsupported" if file_type == "unsupported" else "failed")
            ),
            error_message=(
                "附件数量超过配置的安全限制"
                if attachment_count_exceeded
                else (
                    "附件超过配置的大小限制"
                    if too_large
                    else (
                        "附件累计大小超过配置的安全限制"
                        if total_too_large
                        else (
                            resource_error
                            if resource_error is not None
                            else (
                                "不支持的附件类型"
                                if file_type == "unsupported"
                                else None
                            )
                        )
                    )
                )
            ),
        )
        db.add(attachment)
        db.flush()
        attachments.append(attachment)
        if not allowed:
            if file_type != "unsupported":
                draft = EmailOrderIntakeDraft(
                    message_id=record.id,
                    attachment_id=attachment.id,
                    customer_id=customer.id if customer is not None else None,
                    parser_type=file_type,
                    parse_method="rejected",
                    parse_status="failed",
                    status="failed",
                    source_name=filename,
                    file_sha256=digest,
                    last_error=attachment.error_message,
                )
                db.add(draft)
                db.flush()
                drafts.append(draft)
            if attachment_count_exceeded or total_too_large:
                break
            continue
        if file_type == "pdf":
            draft = _process_pdf_attachment(db, record, attachment, payload, customer)
        else:
            attachment.status = "waiting_excel_parser"
            draft = EmailOrderIntakeDraft(
                message_id=record.id,
                attachment_id=attachment.id,
                customer_id=customer.id if customer is not None else None,
                parser_type=file_type,
                parse_method=None,
                parse_status="waiting_excel_parser",
                status="waiting_excel_parser",
                draft_json=_safe_json(
                    {
                        "source_name": filename,
                        "file_hash": digest,
                        "parse_status": "waiting_excel_parser",
                        "recognition_status": "waiting_excel_parser",
                        "items": [],
                    }
                ),
                source_name=filename,
                file_sha256=digest,
            )
        db.add(draft)
        db.flush()
        drafts.append(draft)

    record.status = _message_status(drafts, attachments)
    record.processed_at = utc_now_naive()
    record.updated_at = utc_now_naive()
    db.commit()
    db.refresh(record)
    return record, True


def retry_draft(
    db: Session,
    draft: EmailOrderIntakeDraft,
    settings: Settings | None = None,
    *,
    commit: bool = True,
) -> EmailOrderIntakeDraft:
    attachment = draft.attachment
    message = draft.message
    message.attempt_count += 1
    message.updated_at = utc_now_naive()
    if attachment.file_type in {"xls", "xlsx"}:
        attachment.status = "waiting_excel_parser"
        draft.status = "waiting_excel_parser"
        draft.parse_status = "waiting_excel_parser"
        draft.last_error = None
    elif attachment.file_type == "pdf":
        content = read_stored_attachment(attachment, settings)
        _mapping, mapped_customer = _active_sender_mapping(
            db, message.normalized_sender_email
        )
        customer = mapped_customer or (
            db.get(Customer, draft.customer_id) if draft.customer_id else None
        )
        if customer is not None:
            draft.customer_id = customer.id
            message.mapped_customer_id = customer.id
        replacement = _process_pdf_attachment(db, message, attachment, content, customer)
        draft.customer_id = replacement.customer_id
        draft.parse_method = replacement.parse_method
        draft.parse_status = replacement.parse_status
        draft.status = replacement.status
        draft.draft_json = replacement.draft_json
        draft.last_error = replacement.last_error
        draft.updated_at = utc_now_naive()
    else:
        raise EmailIntakeError("该附件不能重试")
    message.status = draft.status if draft.status != "review_ready" else "review_ready"
    if commit:
        db.commit()
        db.refresh(draft)
    else:
        db.flush()
    return draft


def rematch_draft(
    db: Session,
    draft: EmailOrderIntakeDraft,
    customer: Customer,
    *,
    commit: bool = True,
) -> EmailOrderIntakeDraft:
    if not customer.is_active or customer.status != "active":
        raise EmailIntakeError("客户已停用，不能用于邮件草稿")
    draft.customer_id = customer.id
    draft.reviewed_at = utc_now_naive()
    if draft.parser_type in {"xls", "xlsx"}:
        draft.status = "waiting_excel_parser"
    else:
        payload = json.loads(draft.draft_json or "{}")
        payload.pop("email_customer_conflict", None)
        matched = match_import_draft(db, payload, customer_id=customer.id)
        draft.draft_json = _safe_json(_without_preview_token(matched))
        draft.status = (
            "review_ready"
            if matched.get("recognition_status") == "recognized"
            else "needs_confirmation"
        )
    draft.message.mapped_customer_id = customer.id
    draft.message.status = draft.status
    draft.updated_at = utc_now_naive()
    if commit:
        db.commit()
        db.refresh(draft)
    else:
        db.flush()
    return draft


def _read_password_file(settings: Settings) -> str:
    path = settings.email_imap_password_file
    if path is None:
        raise EmailIntakeError("未配置邮箱密码文件")
    try:
        password = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise EmailIntakeError("邮箱密码文件不可读取") from error
    if not password:
        raise EmailIntakeError("邮箱密码文件为空")
    return password


def _poll_state(db: Session, mailbox_key: str) -> EmailOrderIntakePollState:
    state = db.get(EmailOrderIntakePollState, mailbox_key)
    if state is None:
        state = EmailOrderIntakePollState(mailbox_key=mailbox_key)
        db.add(state)
        db.flush()
    return state


def _mark_poll_started(db: Session, mailbox_key: str) -> None:
    state = _poll_state(db, mailbox_key)
    state.last_started_at = utc_now_naive()
    state.last_status = "running"
    state.last_error = None
    state.updated_at = utc_now_naive()
    db.commit()


def _mark_poll_finished(
    db: Session,
    mailbox_key: str,
    *,
    result: dict | None = None,
    error_message: str | None = None,
) -> None:
    state = _poll_state(db, mailbox_key)
    state.last_finished_at = utc_now_naive()
    state.updated_at = utc_now_naive()
    if error_message:
        state.last_status = "failed"
        state.last_error = error_message[:1000]
    else:
        payload = result or {}
        state.last_status = "success"
        state.last_error = None
        state.examined_count = int(payload.get("examined") or 0)
        state.created_count = int(payload.get("created") or 0)
        state.duplicate_count = int(payload.get("duplicates") or 0)
        state.failed_count = int(payload.get("failed") or 0)
    db.commit()


def _lock_age_seconds(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except (FileNotFoundError, OSError):
        return 0.0


def _lock_owner_token(path: Path) -> str:
    try:
        first_line = path.read_text(encoding="ascii", errors="ignore").splitlines()[0]
    except (FileNotFoundError, OSError, IndexError):
        return ""
    return first_line.removeprefix("token=").strip() if first_line.startswith("token=") else ""


def _unlink_lock_if_owned(path: Path, owner_token: str) -> None:
    if _lock_owner_token(path) != owner_token:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _write_exclusive_lock(path: Path, owner_token: str) -> int:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    payload = (
        f"token={owner_token}\npid={os.getpid()}\nstarted={time.time():.6f}\n"
    ).encode("ascii")
    try:
        os.write(descriptor, payload)
    except Exception:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return descriptor


def _reclaim_stale_poll_lock(path: Path) -> None:
    if not path.exists() or _lock_age_seconds(path) <= POLL_LOCK_STALE_SECONDS:
        return
    reclaim_path = path.with_name(f"{path.name}.reclaim")
    if (
        reclaim_path.exists()
        and _lock_age_seconds(reclaim_path) > POLL_RECLAIM_LOCK_STALE_SECONDS
    ):
        try:
            reclaim_path.unlink()
        except FileNotFoundError:
            pass
    reclaim_token = secrets.token_hex(16)
    try:
        descriptor = _write_exclusive_lock(reclaim_path, reclaim_token)
    except FileExistsError as error:
        raise EmailIntakeBusyError("邮箱轮询锁正在安全恢复，请稍后重试") from error
    try:
        os.close(descriptor)
        descriptor = -1
        # Recheck after obtaining the exclusive reclaimer.  A healthy poll is
        # bounded far below the stale threshold, so an actually stale file can
        # now be removed without two reclaimers deleting each other's lock.
        if path.exists() and _lock_age_seconds(path) > POLL_LOCK_STALE_SECONDS:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _unlink_lock_if_owned(reclaim_path, reclaim_token)


@contextmanager
def _poll_lock(settings: Settings) -> Iterator[None]:
    settings.email_intake_storage_dir.mkdir(parents=True, exist_ok=True)
    path = settings.email_intake_storage_dir / ".poll.lock"
    _reclaim_stale_poll_lock(path)
    owner_token = secrets.token_hex(16)
    try:
        descriptor = _write_exclusive_lock(path, owner_token)
    except FileExistsError as error:
        raise EmailIntakeBusyError("邮箱轮询正在执行，请稍后重试") from error
    try:
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        # Token comparison prevents an old owner from deleting a replacement
        # lock (the classic stale-lock ABA race).
        _unlink_lock_if_owned(path, owner_token)


def _uidvalidity(client: imaplib.IMAP4, folder: str) -> str:
    _kind, values = client.response("UIDVALIDITY")
    if values and values[0]:
        value = values[0].decode("ascii", errors="ignore") if isinstance(values[0], bytes) else str(values[0])
        if value.strip().isdigit():
            return value.strip()
    kind, values = client.status(folder, "(UIDVALIDITY)")
    if kind == "OK" and values:
        text = values[0].decode("ascii", errors="ignore") if isinstance(values[0], bytes) else str(values[0])
        digits = "".join(char if char.isdigit() else " " for char in text).split()
        if digits:
            return digits[-1]
    raise EmailIntakeError("无法读取邮箱 UIDVALIDITY")


def _imap_rfc822_size(fetched: object) -> int | None:
    if not isinstance(fetched, list):
        return None
    for item in fetched:
        if not isinstance(item, tuple) or not item:
            continue
        metadata = item[0]
        text = metadata.decode("ascii", errors="ignore") if isinstance(metadata, bytes) else str(metadata)
        marker = "RFC822.SIZE"
        position = text.upper().find(marker)
        if position < 0:
            continue
        digits = ""
        for char in text[position + len(marker) :]:
            if char.isdigit():
                digits += char
            elif digits:
                break
        if digits:
            return int(digits)
    return None


def _ensure_poll_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise EmailIntakeError("邮箱检查超过安全时限，已停止本轮收取，请稍后重试")


def _set_imap_socket_timeout(client: imaplib.IMAP4, timeout_seconds: int) -> None:
    client_socket = getattr(client, "sock", None)
    if client_socket is not None and hasattr(client_socket, "settimeout"):
        client_socket.settimeout(timeout_seconds)


def poll_mailbox_once(
    db: Session,
    *,
    settings: Settings | None = None,
    client_factory: Callable[..., imaplib.IMAP4] | None = None,
) -> dict:
    current = settings or load_settings()
    if not current.email_intake_enabled:
        raise EmailIntakeError("邮箱自动收单尚未启用")
    factory = client_factory or (imaplib.IMAP4_SSL if current.email_imap_use_ssl else imaplib.IMAP4)
    with _poll_lock(current):
        _mark_poll_started(db, current.email_intake_mailbox_key)
        deadline = time.monotonic() + current.email_intake_poll_timeout_seconds
        client: imaplib.IMAP4 | None = None
        try:
            password = _read_password_file(current)
            _ensure_poll_deadline(deadline)
            client = (
                factory(
                    current.email_imap_host,
                    current.email_imap_port,
                    ssl_context=ssl.create_default_context(),
                    timeout=current.email_imap_timeout_seconds,
                )
                if current.email_imap_use_ssl
                else factory(
                    current.email_imap_host,
                    current.email_imap_port,
                    timeout=current.email_imap_timeout_seconds,
                )
            )
            _set_imap_socket_timeout(client, current.email_imap_timeout_seconds)
            _ensure_poll_deadline(deadline)
            if not current.email_imap_use_ssl and current.email_imap_starttls:
                client.starttls(ssl_context=ssl.create_default_context())
                _set_imap_socket_timeout(client, current.email_imap_timeout_seconds)
            _ensure_poll_deadline(deadline)
            client.login(current.email_imap_username, password)
            _ensure_poll_deadline(deadline)
            kind, _ = client.select(current.email_imap_folder, readonly=True)
            if kind != "OK":
                raise EmailIntakeError("无法打开配置的邮箱文件夹")
            _ensure_poll_deadline(deadline)
            uidvalidity = _uidvalidity(client, current.email_imap_folder)
            _ensure_poll_deadline(deadline)
            kind, values = client.uid("search", None, "ALL")
            if kind != "OK":
                raise EmailIntakeError("邮箱消息检索失败")
            all_uids = sorted(
                (values[0] or b"").split(),
                key=lambda raw_uid: int(raw_uid) if raw_uid.isdigit() else 0,
            )
            existing_uids = set(
                db.scalars(
                    select(EmailOrderIntakeMessage.imap_uid).where(
                        EmailOrderIntakeMessage.mailbox_key
                        == current.email_intake_mailbox_key,
                        EmailOrderIntakeMessage.imap_uidvalidity == uidvalidity,
                    )
                ).all()
            )
            selected_uids: list[bytes] = []
            # Failed UIDs do not consume the valid-message quota.  Walk the
            # complete server result under the explicit poll deadline so the
            # first N malformed UIDs cannot starve a later valid order.
            for raw_uid in all_uids:
                uid = raw_uid.decode("ascii", errors="ignore")
                if not uid or uid in existing_uids:
                    continue
                selected_uids.append(raw_uid)
            # Select and process old-to-new so a busy mailbox drains its backlog
            # instead of permanently starving older unprocessed messages.
            uids = selected_uids
            created = duplicate = failed = examined = 0
            for raw_uid in uids:
                if created + duplicate >= current.email_intake_max_messages:
                    break
                _ensure_poll_deadline(deadline)
                uid = raw_uid.decode("ascii", errors="ignore")
                if not uid:
                    continue
                examined += 1
                kind, size_fetched = client.uid("fetch", raw_uid, "(RFC822.SIZE)")
                _ensure_poll_deadline(deadline)
                raw_message_size = _imap_rfc822_size(size_fetched)
                if kind != "OK" or raw_message_size is None:
                    failed += 1
                    continue
                if raw_message_size > current.email_intake_max_raw_message_bytes:
                    reason = (
                        f"邮件大小 {raw_message_size} 字节超过安全上限 "
                        f"{current.email_intake_max_raw_message_bytes} 字节，已拒收且记录 UID。"
                    )
                    _rejected, was_created = _persist_rejected_message_uid(
                        db,
                        mailbox_key=current.email_intake_mailbox_key,
                        uidvalidity=uidvalidity,
                        uid=uid,
                        raw_message_size=raw_message_size,
                        reason=reason,
                    )
                    failed += int(was_created)
                    duplicate += int(not was_created)
                    continue
                kind, fetched = client.uid("fetch", raw_uid, "(BODY.PEEK[])")
                _ensure_poll_deadline(deadline)
                if kind != "OK":
                    failed += 1
                    continue
                raw_message = next(
                    (item[1] for item in fetched if isinstance(item, tuple) and isinstance(item[1], bytes)),
                    None,
                )
                if raw_message is None:
                    failed += 1
                    continue
                actual_message_size = len(raw_message)
                if actual_message_size > current.email_intake_max_raw_message_bytes:
                    reason = (
                        f"邮件实际大小 {actual_message_size} 字节超过安全上限 "
                        f"{current.email_intake_max_raw_message_bytes} 字节，已拒收且记录 UID。"
                    )
                    _rejected, was_created = _persist_rejected_message_uid(
                        db,
                        mailbox_key=current.email_intake_mailbox_key,
                        uidvalidity=uidvalidity,
                        uid=uid,
                        raw_message_size=actual_message_size,
                        reason=reason,
                    )
                    failed += int(was_created)
                    duplicate += int(not was_created)
                    continue
                _message, was_created = ingest_raw_message(
                    db,
                    mailbox_key=current.email_intake_mailbox_key,
                    uidvalidity=uidvalidity,
                    uid=uid,
                    raw_message=raw_message,
                    settings=current,
                )
                _ensure_poll_deadline(deadline)
                created += int(was_created)
                duplicate += int(not was_created)
            result = {
                "examined": examined,
                "created": created,
                "duplicates": duplicate,
                "failed": failed,
            }
            _mark_poll_finished(
                db, current.email_intake_mailbox_key, result=result
            )
            return result
        except EmailIntakeError as error:
            db.rollback()
            _mark_poll_finished(
                db,
                current.email_intake_mailbox_key,
                error_message=str(error),
            )
            raise
        except (TimeoutError, socket.timeout) as error:
            db.rollback()
            message = "邮箱连接或读取超时，已安全停止本轮收取"
            _mark_poll_finished(
                db,
                current.email_intake_mailbox_key,
                error_message=message,
            )
            raise EmailIntakeError(message) from error
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as error:
            db.rollback()
            message = "邮箱连接或认证失败"
            _mark_poll_finished(
                db,
                current.email_intake_mailbox_key,
                error_message=message,
            )
            raise EmailIntakeError(message) from error
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
