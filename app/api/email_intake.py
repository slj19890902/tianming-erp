from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.api.orders import (
    _finalize_pdf_preview_for_user,
    _match_pdf_preview_for_user,
    _parse_order_pdf_preview,
    _pdf_failure_draft,
)
from app.core.config import load_settings
from app.core.time_contract import utc_naive_to_api, utc_now_naive
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.email_order_intake import (
    EmailOrderIntakeAttachment,
    EmailOrderIntakeDraft,
    EmailOrderIntakeMessage,
    EmailOrderIntakePollState,
    EmailOrderSenderMapping,
)
from app.models.user import User
from app.services.email_order_intake import (
    EmailIntakeBusyError,
    EmailIntakeError,
    normalize_sender_email,
    poll_mailbox_once,
    read_stored_attachment,
    redact_sender_email,
    rematch_draft,
    retry_draft,
)
from app.services.order_pdf_import import PdfParseError
from app.services.pdf_customer_templates import load_active_pdf_template_rules
from app.services.pdf_preview_redaction import redact_pdf_preview_for_user


router = APIRouter()
can_view = PermissionChecker("email_intake.view")
can_manage = PermissionChecker("email_intake.manage")


_EMAIL_COMMERCIAL_KEY_FRAGMENTS = (
    "price",
    "quote",
    "amount",
    "subtotal",
    "cost",
    "margin",
    "profit",
    "supplier",
    "vendor",
    "单价",
    "金额",
    "成本",
    "毛利",
    "利润",
    "报价",
    "供应商",
)
_EMAIL_COMMERCIAL_TEXT_FRAGMENTS = (
    "unit price",
    "amount",
    "cost",
    "margin",
    "profit",
    "supplier",
    "vendor",
    "quote",
    "cny",
    "rmb",
    "单价",
    "金额",
    "成本",
    "毛利",
    "利润",
    "供应商",
    "供方",
    "供应商报价",
    "采购价",
    "含税价",
    "平方价",
    "￥",
    "¥",
)
_EMAIL_PUBLIC_JSON_KEYS = frozenset(
    {
        "source_name",
        "file_hash",
        "preview_safety_token",
        "email_intake_draft_id",
        "parse_status",
        "parse_method",
        "recognition_status",
        "customer_type",
        "customer_name",
        "customer_name_raw",
        "customer_po",
        "order_no",
        "order_number",
        "order_date",
        "delivery_date",
        "matched_customer_id",
        "customer_id",
        "customer_match_status",
        "customer_route",
        "template_customer_id",
        "template_name",
        "template_code",
        "status",
        "integrity_check",
        "integrity_status",
        "source_detail_count",
        "parsed_detail_count",
        "source_line_numbers",
        "parsed_line_numbers",
        "missing_line_numbers",
        "source_total_quantity",
        "parsed_total_quantity",
        "quantity_total_diff",
        "requires_manual_quantity_review",
        "items",
        "line_no",
        "product_code",
        "raw_product_code",
        "normalized_product_code",
        "inventory_code",
        "product_name",
        "spec",
        "specification",
        "quantity",
        "raw_quantity",
        "unit",
        "production_notes",
        "matched_product_id",
        "matched_common_box_id",
        "match_status",
        "product_match_status",
        "quantity_review_required",
        "material_code",
        "flute_type",
        "layer_count",
        "product_candidates",
        "candidate_products",
        "candidates",
        "id",
        "product_id",
        "common_box_id",
        "match_score",
        "score",
        "variant_ref",
        "leading_score_gap",
    }
)
_REDACTED_EMAIL_VALUE = object()


def _api_datetime(value: datetime | None) -> str | None:
    return utc_naive_to_api(value) if value is not None else None


class SenderMappingCreate(BaseModel):
    customer_id: int = Field(gt=0)
    sender_email: str = Field(min_length=3, max_length=320)
    is_active: bool = True
    notes: str | None = Field(default=None, max_length=1000)


class SenderMappingUpdate(BaseModel):
    customer_id: int | None = Field(default=None, gt=0)
    sender_email: str | None = Field(default=None, min_length=3, max_length=320)
    is_active: bool | None = None
    notes: str | None = Field(default=None, max_length=1000)


class RematchRequest(BaseModel):
    customer_id: int = Field(gt=0)


def _audit(
    db: Session,
    request: Request,
    user: User,
    *,
    action: str,
    resource: str,
    entity_id: int | None = None,
    details: dict | None = None,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            username=user.username,
            role=user.role,
            action=action[:30],
            resource=resource[:100],
            entity_type=resource[:100],
            entity_id=entity_id,
            details=json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
            ip_address=request.client.host if request.client else None,
        )
    )


def _email_metadata_text_for_user(value: str | None, user: User | None) -> str | None:
    if value is None or user is None or has_permission(user, "cost.view"):
        return value
    return None if _email_commercial_text_is_sensitive(value) else value


def _mapping_payload(row: EmailOrderSenderMapping, user: User | None = None) -> dict:
    return {
        "id": row.id,
        "customer_id": row.customer_id,
        "customer_name": row.customer.name if row.customer else None,
        "sender_email": row.sender_email,
        "is_active": row.is_active,
        "notes": _email_metadata_text_for_user(row.notes, user),
        "created_at": _api_datetime(row.created_at),
        "updated_at": _api_datetime(row.updated_at),
    }


def _safe_duplicate_attachment_id(
    db: Session,
    row: EmailOrderIntakeAttachment,
    user: User,
    customer_id: int | None,
) -> int | None:
    duplicate_id = row.duplicate_of_attachment_id
    if duplicate_id is None:
        return None
    target = db.execute(
        select(
            EmailOrderIntakeAttachment.id,
            EmailOrderIntakeDraft.customer_id,
            EmailOrderIntakeMessage.mapped_customer_id,
        )
        .join(
            EmailOrderIntakeMessage,
            EmailOrderIntakeMessage.id == EmailOrderIntakeAttachment.message_id,
        )
        .outerjoin(
            EmailOrderIntakeDraft,
            EmailOrderIntakeDraft.attachment_id == EmailOrderIntakeAttachment.id,
        )
        .where(EmailOrderIntakeAttachment.id == duplicate_id)
    ).first()
    if target is None:
        return None
    target_customer_id = target.customer_id
    if target_customer_id is None:
        target_customer_id = target.mapped_customer_id
    if target_customer_id != customer_id:
        return None
    if not _customer_visible(user, db, target_customer_id):
        return None
    return int(target.id)


def _attachment_payload(
    row: EmailOrderIntakeAttachment,
    *,
    db: Session,
    user: User,
    customer_id: int | None,
) -> dict:
    duplicate_id = _safe_duplicate_attachment_id(db, row, user, customer_id)
    return {
        "id": row.id,
        "part_index": row.part_index,
        "filename": _email_metadata_text_for_user(row.filename, user),
        "content_type": row.content_type,
        "byte_size": row.byte_size,
        "file_sha256": row.file_sha256,
        "file_type": row.file_type,
        "is_duplicate_content": duplicate_id is not None,
        "duplicate_of_attachment_id": duplicate_id,
        "status": row.status,
        "error_message": _email_metadata_text_for_user(row.error_message, user),
        "created_at": _api_datetime(row.created_at),
    }


def _normalize_json_key(value: object) -> str:
    return str(value or "").casefold().replace("-", "_").replace(" ", "_")


def _email_commercial_key_is_sensitive(value: object) -> bool:
    normalized = _normalize_json_key(value)
    return any(fragment in normalized for fragment in _EMAIL_COMMERCIAL_KEY_FRAGMENTS)


def _email_commercial_text_is_sensitive(value: str) -> bool:
    normalized = value.casefold()
    return any(fragment in normalized for fragment in _EMAIL_COMMERCIAL_TEXT_FRAGMENTS)


def _redact_email_commercial_fields(value: object, *, parent_key: str = "") -> object:
    """Return a fail-closed allow-listed email draft for a no-cost user."""

    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, child in value.items():
            normalized_key = _normalize_json_key(key)
            if (
                normalized_key not in _EMAIL_PUBLIC_JSON_KEYS
                or _email_commercial_key_is_sensitive(key)
            ):
                continue
            redacted = _redact_email_commercial_fields(child, parent_key=str(key))
            if redacted is not _REDACTED_EMAIL_VALUE:
                result[str(key)] = redacted
        return result
    if isinstance(value, list):
        result_list: list[object] = []
        for child in value:
            redacted = _redact_email_commercial_fields(child, parent_key=parent_key)
            if redacted is not _REDACTED_EMAIL_VALUE:
                result_list.append(redacted)
        return result_list
    if isinstance(value, tuple):
        redacted_items = [
            _redact_email_commercial_fields(child, parent_key=parent_key)
            for child in value
        ]
        return tuple(
            child for child in redacted_items if child is not _REDACTED_EMAIL_VALUE
        )
    if isinstance(value, str) and _email_commercial_text_is_sensitive(value):
        return _REDACTED_EMAIL_VALUE
    return value


def _email_draft_json_for_user(value: object, user: User) -> object:
    may_view_cost = has_permission(user, "cost.view")
    # Reuse the established PDF-preview redaction first.  The extra email
    # boundary only tightens the no-cost response; authorized users retain the
    # complete independent copy.
    redacted = redact_pdf_preview_for_user(value, can_view_cost=may_view_cost)
    if may_view_cost:
        return redacted
    public_value = _redact_email_commercial_fields(redacted)
    return {} if public_value is _REDACTED_EMAIL_VALUE else public_value


def _draft_payload(
    row: EmailOrderIntakeDraft,
    *,
    db: Session,
    include_result: bool = False,
    user: User | None = None,
) -> dict:
    message = row.message
    attachment = row.attachment
    duplicate_attachment_id = (
        _safe_duplicate_attachment_id(db, attachment, user, row.customer_id)
        if attachment is not None and user is not None
        else None
    )
    result = {
        "id": row.id,
        "message_id": row.message_id,
        "attachment_id": row.attachment_id,
        "customer_id": row.customer_id,
        "customer_name": row.customer.name if row.customer else None,
        "parser_type": row.parser_type,
        "parse_method": row.parse_method,
        "parse_status": row.parse_status,
        "status": row.status,
        "source_name": _email_metadata_text_for_user(row.source_name, user),
        "attachment_name": _email_metadata_text_for_user(
            attachment.filename if attachment else row.source_name, user
        ),
        "file_type": attachment.file_type if attachment else row.parser_type,
        "sender_email": message.sender_email if message else None,
        "received_at": _api_datetime(message.received_at) if message else None,
        "subject": _email_metadata_text_for_user(
            message.subject if message else None, user
        ),
        "is_duplicate_content": duplicate_attachment_id is not None,
        "duplicate_of_attachment_id": duplicate_attachment_id,
        "file_sha256": row.file_sha256,
        "converted_order_id": row.converted_order_id,
        "last_error": _email_metadata_text_for_user(row.last_error, user),
        "error_message": _email_metadata_text_for_user(
            row.last_error or (attachment.error_message if attachment else None), user
        ),
        "created_at": _api_datetime(row.created_at),
        "updated_at": _api_datetime(row.updated_at),
    }
    if include_result:
        draft_value = json.loads(row.draft_json or "{}")
        result["draft"] = (
            _email_draft_json_for_user(draft_value, user)
            if user is not None
            else draft_value
        )
    return result


def _converted_draft_conflict(draft: EmailOrderIntakeDraft) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "EMAIL_INTAKE_DRAFT_CONVERTED",
            "message": "This email order draft was already converted to a formal order.",
            "email_intake_draft_id": draft.id,
            "converted_order_id": draft.converted_order_id,
        },
    )


def _ensure_draft_not_converted(draft: EmailOrderIntakeDraft) -> None:
    if draft.status == "converted" or draft.converted_order_id is not None:
        raise _converted_draft_conflict(draft)


def _message_payload(
    row: EmailOrderIntakeMessage,
    *,
    db: Session,
    detail: bool = False,
    user: User | None = None,
) -> dict:
    unrestricted = user is None or has_unrestricted_customer_access(user, db)
    visible_drafts = [
        draft
        for draft in row.drafts
        if unrestricted or _customer_visible(user, db, draft.customer_id)
    ]
    mapped_customer_id = row.mapped_customer_id
    if (
        not unrestricted
        and user is not None
        and not _customer_visible(user, db, mapped_customer_id)
    ):
        mapped_customer_id = None
    visible_status = _visible_message_status(
        row.status, {draft.status for draft in visible_drafts}
    ) if not unrestricted else row.status
    visible_last_error = row.last_error if unrestricted else next(
        (draft.last_error for draft in visible_drafts if draft.last_error), None
    )
    result = {
        "id": row.id,
        "mailbox_key": row.mailbox_key,
        "imap_uidvalidity": row.imap_uidvalidity,
        "imap_uid": row.imap_uid,
        "message_id": row.message_id_header,
        "sender_email": row.sender_email,
        "subject": _email_metadata_text_for_user(row.subject, user),
        "received_at": _api_datetime(row.received_at),
        "mapped_customer_id": mapped_customer_id,
        "status": visible_status,
        "attempt_count": row.attempt_count,
        "last_error": _email_metadata_text_for_user(visible_last_error, user),
        "processed_at": _api_datetime(row.processed_at),
        "created_at": _api_datetime(row.created_at),
    }
    if detail:
        drafts_by_attachment_id = {draft.attachment_id: draft for draft in row.drafts}
        visible_draft_ids = {draft.id for draft in visible_drafts}
        visible_attachments: list[tuple[EmailOrderIntakeAttachment, int | None]] = []
        for attachment in row.attachments:
            attachment_draft = drafts_by_attachment_id.get(attachment.id)
            if attachment_draft is not None:
                if attachment_draft.id not in visible_draft_ids:
                    continue
                attachment_customer_id = attachment_draft.customer_id
            else:
                # An attachment without its own draft has no reliable customer
                # owner.  Once sibling drafts exist, never expose that orphan
                # attachment to a scoped account based only on the message's
                # mutable sender mapping.
                if not unrestricted and row.drafts:
                    continue
                attachment_customer_id = row.mapped_customer_id
                if (
                    not unrestricted
                    and user is not None
                    and not _customer_visible(user, db, attachment_customer_id)
                ):
                    continue
            visible_attachments.append((attachment, attachment_customer_id))
        result["attachments"] = [
            _attachment_payload(
                attachment,
                db=db,
                user=user,
                customer_id=customer_id,
            )
            for attachment, customer_id in visible_attachments
            if user is not None
        ]
        result["drafts"] = [
            _draft_payload(item, db=db, include_result=True, user=user)
            for item in visible_drafts
        ]
    return result


def _customer_visible(user: User, db: Session, customer_id: int | None) -> bool:
    if has_unrestricted_customer_access(user, db):
        return True
    return customer_id is not None and customer_id in customer_scope_ids(user, db)


def _visible_message_status(base_status: str, draft_statuses: set[str]) -> str:
    if not draft_statuses:
        return base_status
    priorities = (
        "needs_mapping",
        "needs_confirmation",
        "review_ready",
        "waiting_excel_parser",
        "failed",
        "converted",
        "archived",
        "ignored",
    )
    return next(
        (candidate for candidate in priorities if candidate in draft_statuses),
        base_status,
    )


def _message_scope_filter(visible_customer_ids: set[int]):
    visible_draft_message_ids = select(EmailOrderIntakeDraft.message_id).where(
        EmailOrderIntakeDraft.customer_id.in_(visible_customer_ids)
    )
    any_draft_exists = (
        select(EmailOrderIntakeDraft.id)
        .where(EmailOrderIntakeDraft.message_id == EmailOrderIntakeMessage.id)
        .exists()
    )
    return or_(
        EmailOrderIntakeMessage.id.in_(visible_draft_message_ids),
        and_(
            EmailOrderIntakeMessage.mapped_customer_id.in_(visible_customer_ids),
            ~any_draft_exists,
        ),
    )


def _get_visible_message(db: Session, message_id: int, user: User) -> EmailOrderIntakeMessage:
    row = db.scalar(
        select(EmailOrderIntakeMessage)
        .options(
            selectinload(EmailOrderIntakeMessage.attachments),
            selectinload(EmailOrderIntakeMessage.drafts),
        )
        .where(EmailOrderIntakeMessage.id == message_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="邮件记录不存在")
    visible_draft = any(
        _customer_visible(user, db, draft.customer_id) for draft in row.drafts
    )
    visible_unassigned_message = not row.drafts and _customer_visible(
        user, db, row.mapped_customer_id
    )
    if not visible_draft and not visible_unassigned_message:
        raise HTTPException(status_code=403, detail="无客户访问权限")
    return row


def _get_visible_draft(db: Session, draft_id: int, user: User) -> EmailOrderIntakeDraft:
    row = db.scalar(
        select(EmailOrderIntakeDraft)
        .options(
            selectinload(EmailOrderIntakeDraft.attachment),
            selectinload(EmailOrderIntakeDraft.message),
            selectinload(EmailOrderIntakeDraft.customer),
        )
        .where(EmailOrderIntakeDraft.id == draft_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="邮件订单草稿不存在")
    if not _customer_visible(user, db, row.customer_id):
        raise HTTPException(status_code=403, detail="无客户访问权限")
    return row


@router.get("/status")
def email_intake_status(
    db: Session = Depends(get_db),
    user: User = Depends(can_view),
) -> dict:
    current = load_settings()
    password_file_ready = False
    if current.email_imap_password_file is not None:
        try:
            password_file_ready = current.email_imap_password_file.is_file()
        except OSError:
            password_file_ready = False
    poll_state = db.get(
        EmailOrderIntakePollState, current.email_intake_mailbox_key
    )
    if poll_state is None:
        last_poll_status = "never"
        last_poll_at = None
        last_poll_message = "尚未执行邮箱检查"
    else:
        last_poll_status = poll_state.last_status
        last_poll_at = poll_state.last_finished_at or poll_state.last_started_at
        if poll_state.last_status == "failed":
            last_poll_message = poll_state.last_error or "邮箱检查失败"
        elif poll_state.last_status == "running":
            last_poll_message = "正在检查邮箱"
        elif poll_state.last_status == "success":
            last_poll_message = (
                f"检查 {poll_state.examined_count} 封，"
                f"新建 {poll_state.created_count}，"
                f"重复 {poll_state.duplicate_count}，"
                f"失败 {poll_state.failed_count}"
            )
        else:
            last_poll_message = "尚未执行邮箱检查"
    unrestricted = has_unrestricted_customer_access(user, db)
    if unrestricted:
        counts = dict(
            db.execute(
                select(
                    EmailOrderIntakeMessage.status,
                    func.count(EmailOrderIntakeMessage.id),
                )
                .where(
                    EmailOrderIntakeMessage.mailbox_key
                    == current.email_intake_mailbox_key
                )
                .group_by(EmailOrderIntakeMessage.status)
            ).all()
        )
        poll_counts = {
            "examined": poll_state.examined_count if poll_state else 0,
            "created": poll_state.created_count if poll_state else 0,
            "duplicates": poll_state.duplicate_count if poll_state else 0,
            "failed": poll_state.failed_count if poll_state else 0,
        }
        statistics_scope = "all_customers"
    else:
        visible = customer_scope_ids(user, db)
        counts: dict[str, int] = {}
        if visible:
            message_rows = db.execute(
                select(
                    EmailOrderIntakeMessage.id,
                    EmailOrderIntakeMessage.status,
                ).where(
                    EmailOrderIntakeMessage.mailbox_key
                    == current.email_intake_mailbox_key,
                    _message_scope_filter(visible),
                )
            ).all()
            visible_message_ids = {row.id for row in message_rows}
            draft_statuses: dict[int, set[str]] = defaultdict(set)
            if visible_message_ids:
                for message_id, draft_status in db.execute(
                    select(
                        EmailOrderIntakeDraft.message_id,
                        EmailOrderIntakeDraft.status,
                    ).where(
                        EmailOrderIntakeDraft.message_id.in_(visible_message_ids),
                        EmailOrderIntakeDraft.customer_id.in_(visible),
                    )
                ).all():
                    draft_statuses[message_id].add(draft_status)
            counts = dict(
                Counter(
                    _visible_message_status(
                        row.status, draft_statuses.get(row.id, set())
                    )
                    for row in message_rows
                )
            )
        # Poll-state counters are mailbox-wide and cannot be attributed to one
        # customer without a persisted run ledger.  Never expose those totals
        # or the mailbox-wide timestamp/status to a scoped account; the
        # per-status counts above are scope-filtered.
        poll_counts = {}
        statistics_scope = "customer_scope"
        last_poll_status = "scope_filtered"
        last_poll_at = None
        last_poll_message = "统计仅显示当前账号可见客户。"
    return {
        "enabled": current.email_intake_enabled,
        "mailbox_key": current.email_intake_mailbox_key,
        "configured": bool(
            current.email_intake_enabled
            and current.email_imap_host
            and current.email_imap_username
            and password_file_ready
            and (current.email_imap_use_ssl or current.email_imap_starttls)
        ),
        "masked_email": redact_sender_email(current.email_imap_username)
        if current.email_imap_username
        else "",
        "tls_enabled": current.email_imap_use_ssl or current.email_imap_starttls,
        "poll_mode": "one_shot",
        "max_messages_per_poll": current.email_intake_max_messages,
        "max_attachment_bytes": current.email_intake_max_attachment_bytes,
        "statistics_scope": statistics_scope,
        "message_counts": counts,
        "last_poll_at": _api_datetime(last_poll_at),
        "last_poll_status": last_poll_status,
        "last_poll_message": last_poll_message,
        "last_poll_counts": poll_counts,
    }


@router.get("/messages")
def list_messages(
    intake_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(can_view),
) -> dict:
    statement = select(EmailOrderIntakeMessage).options(
        selectinload(EmailOrderIntakeMessage.drafts)
    )
    if intake_status:
        statement = statement.where(EmailOrderIntakeMessage.status == intake_status)
    if not has_unrestricted_customer_access(user, db):
        visible = customer_scope_ids(user, db)
        if not visible:
            return {"items": [], "limit": limit, "offset": offset}
        statement = statement.where(_message_scope_filter(visible))
    rows = db.scalars(
        statement.order_by(EmailOrderIntakeMessage.id.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "items": [_message_payload(row, db=db, user=user) for row in rows],
        "limit": limit,
        "offset": offset,
    }


@router.get("/messages/{message_id}")
def get_message(
    message_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view),
) -> dict:
    return _message_payload(
        _get_visible_message(db, message_id, user), db=db, detail=True, user=user
    )


@router.get("/drafts")
def list_drafts(
    draft_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(can_view),
) -> dict:
    statement = select(EmailOrderIntakeDraft).options(
        selectinload(EmailOrderIntakeDraft.attachment),
        selectinload(EmailOrderIntakeDraft.message),
        selectinload(EmailOrderIntakeDraft.customer),
    )
    if draft_status:
        statement = statement.where(EmailOrderIntakeDraft.status == draft_status)
    if not has_unrestricted_customer_access(user, db):
        visible = customer_scope_ids(user, db)
        if not visible:
            return {"items": [], "limit": limit, "offset": offset}
        statement = statement.where(EmailOrderIntakeDraft.customer_id.in_(visible))
    rows = db.scalars(
        statement.order_by(EmailOrderIntakeDraft.id.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "items": [_draft_payload(row, db=db, user=user) for row in rows],
        "limit": limit,
        "offset": offset,
    }


@router.get("/drafts/{draft_id}")
def get_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view),
) -> dict:
    return _draft_payload(
        _get_visible_draft(db, draft_id, user),
        db=db,
        include_result=True,
        user=user,
    )


@router.get("/sender-mappings")
def list_mappings(
    db: Session = Depends(get_db),
    user: User = Depends(can_view),
) -> dict:
    statement = select(EmailOrderSenderMapping).options(selectinload(EmailOrderSenderMapping.customer))
    if not has_unrestricted_customer_access(user, db):
        visible = customer_scope_ids(user, db)
        if not visible:
            return {"items": []}
        statement = statement.where(EmailOrderSenderMapping.customer_id.in_(visible))
    rows = db.scalars(statement.order_by(EmailOrderSenderMapping.id.desc())).all()
    return {"items": [_mapping_payload(row, user) for row in rows]}


@router.post("/sender-mappings", status_code=status.HTTP_201_CREATED)
def create_mapping(
    payload: SenderMappingCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_manage),
) -> dict:
    require_customer_access(payload.customer_id, current_user=user, db=db)
    customer = db.get(Customer, payload.customer_id)
    if customer is None or not customer.is_active or customer.status != "active":
        raise HTTPException(status_code=400, detail="客户不存在或已停用")
    normalized = normalize_sender_email(payload.sender_email)
    if not normalized:
        raise HTTPException(status_code=400, detail="发件人邮箱格式无效")
    row = EmailOrderSenderMapping(
        customer_id=payload.customer_id,
        sender_email=normalized,
        normalized_sender_email=normalized,
        is_active=payload.is_active,
        notes=payload.notes,
        created_by=user.id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="该发件人邮箱已存在映射") from error
    _audit(db, request, user, action="EMAIL_MAP_CREATE", resource="email_sender_mapping", entity_id=row.id, details={"customer_id": row.customer_id})
    db.commit()
    db.refresh(row)
    row.customer = customer
    return _mapping_payload(row, user)


@router.put("/sender-mappings/{mapping_id}")
def update_mapping(
    mapping_id: int,
    payload: SenderMappingUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_manage),
) -> dict:
    row = db.get(EmailOrderSenderMapping, mapping_id)
    if row is None:
        raise HTTPException(status_code=404, detail="发件人映射不存在")
    require_customer_access(row.customer_id, current_user=user, db=db)
    customer_id = payload.customer_id if payload.customer_id is not None else row.customer_id
    require_customer_access(customer_id, current_user=user, db=db)
    customer = db.get(Customer, customer_id)
    if customer is None or not customer.is_active or customer.status != "active":
        raise HTTPException(status_code=400, detail="客户不存在或已停用")
    if payload.sender_email is not None:
        normalized = normalize_sender_email(payload.sender_email)
        if not normalized:
            raise HTTPException(status_code=400, detail="发件人邮箱格式无效")
        row.sender_email = normalized
        row.normalized_sender_email = normalized
    row.customer_id = customer_id
    if payload.is_active is not None:
        row.is_active = payload.is_active
    if "notes" in payload.model_fields_set:
        row.notes = payload.notes
    row.updated_by = user.id
    row.updated_at = utc_now_naive()
    _audit(db, request, user, action="EMAIL_MAP_UPDATE", resource="email_sender_mapping", entity_id=row.id, details={"customer_id": row.customer_id, "active": row.is_active})
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="该发件人邮箱已存在映射") from error
    db.refresh(row)
    row.customer = customer
    return _mapping_payload(row, user)


@router.delete("/sender-mappings/{mapping_id}")
def delete_mapping(
    mapping_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_manage),
) -> dict:
    row = db.get(EmailOrderSenderMapping, mapping_id)
    if row is None:
        raise HTTPException(status_code=404, detail="发件人映射不存在")
    require_customer_access(row.customer_id, current_user=user, db=db)
    customer_id = row.customer_id
    db.delete(row)
    _audit(db, request, user, action="EMAIL_MAP_DELETE", resource="email_sender_mapping", entity_id=mapping_id, details={"customer_id": customer_id})
    db.commit()
    return {"ok": True}


@router.post("/poll-once")
def manual_poll(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_manage),
) -> dict:
    try:
        result = poll_mailbox_once(db)
    except EmailIntakeBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except EmailIntakeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    _audit(db, request, user, action="EMAIL_POLL", resource="email_intake", details=result)
    db.commit()
    return {
        **result,
        "received_count": result.get("created", 0),
        "message": f"邮箱收取完成，新增 {result.get('created', 0)} 份草稿",
    }


@router.post("/drafts/{draft_id}/retry")
def retry_email_draft(
    draft_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_manage),
) -> dict:
    draft = _get_visible_draft(db, draft_id, user)
    _ensure_draft_not_converted(draft)
    mapped_customer_id = db.scalar(
        select(EmailOrderSenderMapping.customer_id).where(
            EmailOrderSenderMapping.normalized_sender_email
            == draft.message.normalized_sender_email,
            EmailOrderSenderMapping.is_active.is_(True),
        )
    )
    if mapped_customer_id is not None:
        require_customer_access(mapped_customer_id, current_user=user, db=db)
    try:
        result = retry_draft(db, draft, commit=False)
    except EmailIntakeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    require_customer_access(result.customer_id, current_user=user, db=db)
    _audit(db, request, user, action="EMAIL_DRAFT_RETRY", resource="email_intake_draft", entity_id=draft.id)
    db.commit()
    db.refresh(result)
    return _draft_payload(result, db=db, include_result=True, user=user)


@router.post("/drafts/{draft_id}/rematch")
def rematch_email_draft(
    draft_id: int,
    payload: RematchRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_manage),
) -> dict:
    draft = _get_visible_draft(db, draft_id, user)
    _ensure_draft_not_converted(draft)
    require_customer_access(payload.customer_id, current_user=user, db=db)
    customer = db.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    try:
        result = rematch_draft(db, draft, customer, commit=False)
    except EmailIntakeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    require_customer_access(result.customer_id, current_user=user, db=db)
    result.reviewed_by = user.id
    _audit(db, request, user, action="EMAIL_DRAFT_REMATCH", resource="email_intake_draft", entity_id=draft.id, details={"customer_id": customer.id})
    db.commit()
    db.refresh(result)
    return _draft_payload(result, db=db, include_result=True, user=user)


@router.post("/drafts/{draft_id}/open-preview")
def open_email_pdf_preview(
    draft_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_view),
) -> dict:
    if not has_permission(user, "orders.create"):
        raise HTTPException(status_code=403, detail="需要订单新建权限才能打开订单草稿")
    draft = _get_visible_draft(db, draft_id, user)
    _ensure_draft_not_converted(draft)
    if draft.parser_type != "pdf":
        raise HTTPException(status_code=409, detail="Excel 附件等待后续解析器，当前不能打开订单预览")
    if draft.customer_id is None:
        raise HTTPException(status_code=409, detail="请先人工确认发件人对应客户")
    if draft.status not in {"review_ready", "needs_confirmation"}:
        raise HTTPException(
            status_code=409,
            detail="该邮箱订单草稿尚未准备好，请先重试解析或确认客户。",
        )
    require_customer_access(draft.customer_id, current_user=user, db=db)
    try:
        content = read_stored_attachment(draft.attachment)
        parsed = _parse_order_pdf_preview(content, draft.source_name, load_active_pdf_template_rules(db))
        route = parsed.get("customer_route") if isinstance(parsed.get("customer_route"), dict) else {}
        routed_id = route.get("template_customer_id") if route.get("status") == "locked" else None
        if routed_id is not None and int(routed_id) != draft.customer_id:
            raise HTTPException(status_code=409, detail="PDF 解析客户与发件人映射冲突，请人工复核")
        parsed["file_hash"] = draft.file_sha256
        matched = _match_pdf_preview_for_user(
            db, parsed, user, customer_id=draft.customer_id
        )
        matched["email_intake_draft_id"] = draft.id
        preview = _finalize_pdf_preview_for_user(matched, user)
    except PdfParseError as error:
        failed = _pdf_failure_draft(
            draft.source_name, error, digest=draft.file_sha256
        )
        failed["email_intake_draft_id"] = draft.id
        preview = _finalize_pdf_preview_for_user(
            failed,
            user,
        )
    except EmailIntakeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    _audit(db, request, user, action="EMAIL_OPEN_PREVIEW", resource="email_intake_draft", entity_id=draft.id, details={"customer_id": draft.customer_id})
    db.commit()
    return _email_draft_json_for_user(preview, user)
