from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
)
from app.core.config import load_settings
from app.models.finance import FinanceIdempotencyRecord
from app.models.supplier_settlement import (
    SupplierMonthlyInvoice,
    SupplierMonthlyStatement,
)
from app.models.user import User
from app.services.audit_log import append_audit_event
from app.services.invoice_attachments import (
    InvoiceAttachmentError,
    store_original_invoice_pdf,
)
from app.services.supplier_monthly_settlement import (
    SupplierSettlementError,
    add_adjustment,
    add_invoice,
    add_payment,
    confirm_statement,
    default_closed_settlement_month,
    generate_or_refresh_settlements,
    list_statement_responses,
    reopen_statement,
    review_statement,
    scan_settlement_candidates,
    settlement_period,
    settlement_period_utc_bounds,
    statement_response,
)
from app.services.supplier_receipt_price_facts import (
    SupplierReceiptPriceFactError,
    preview_historical_price_adoptions,
)


router = APIRouter()
can_read = PermissionChecker("finance.view")
can_execute = PermissionChecker("finance.execute")
can_attachment_view = PermissionChecker("finance.invoice_attachment.view")
can_attachment_manage = PermissionChecker("finance.invoice_attachment.manage")
MONEY = Decimal("0.01")


def _company_scope(user: User, db: Session) -> User:
    if not has_permission(user, "cost.view") or not has_unrestricted_customer_access(
        user, db
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="供应商月结仅限可查看全公司成本数据的财务账号",
        )
    return user


def company_read(
    user: User = Depends(can_read), db: Session = Depends(get_db)
) -> User:
    return _company_scope(user, db)


def company_execute(
    user: User = Depends(can_execute), db: Session = Depends(get_db)
) -> User:
    return _company_scope(user, db)


class IdempotentPayload(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def clean_idempotency_key(cls, value: str) -> str:
        return value.strip()


class GeneratePayload(IdempotentPayload):
    settlement_month: str

    @field_validator("settlement_month")
    @classmethod
    def validate_month(cls, value: str) -> str:
        settlement_period(value)
        return value


class ReviewPayload(IdempotentPayload):
    expected_version: int = Field(gt=0)
    supplier_statement_number: str = Field(min_length=1, max_length=120)
    supplier_statement_date: date
    supplier_statement_amount: Decimal = Field(ge=0)

    @field_validator("supplier_statement_number")
    @classmethod
    def clean_statement_number(cls, value: str) -> str:
        return value.strip()


class AdjustmentPayload(IdempotentPayload):
    expected_version: int = Field(gt=0)
    statement_line_id: int | None = Field(default=None, gt=0)
    difference_type: str
    amount: Decimal
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("difference_type")
    @classmethod
    def validate_difference_type(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in {"price", "quantity", "tax_rounding", "other"}:
            raise ValueError("差异类型无效")
        return normalized

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str | None) -> str | None:
        return str(value or "").strip() or None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        normalized = value.quantize(MONEY, rounding=ROUND_HALF_UP)
        if normalized == 0:
            raise ValueError("差异调整金额不能为 0")
        return normalized


class TransitionPayload(IdempotentPayload):
    expected_version: int = Field(gt=0)


class InvoicePayload(IdempotentPayload):
    expected_version: int = Field(gt=0)
    invoice_number: str = Field(min_length=1, max_length=120)
    invoice_date: date
    received_date: date
    invoice_total_amount: Decimal = Field(gt=0)
    allocated_amount: Decimal = Field(gt=0)
    tax_amount: Decimal = Field(ge=0)
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("invoice_number")
    @classmethod
    def clean_invoice_number(cls, value: str) -> str:
        return value.strip()

    @field_validator("note")
    @classmethod
    def clean_invoice_note(cls, value: str | None) -> str | None:
        return str(value or "").strip() or None

    @model_validator(mode="after")
    def validate_invoice_amounts(self):
        self.invoice_total_amount = self.invoice_total_amount.quantize(
            MONEY, rounding=ROUND_HALF_UP
        )
        self.allocated_amount = self.allocated_amount.quantize(
            MONEY, rounding=ROUND_HALF_UP
        )
        self.tax_amount = self.tax_amount.quantize(MONEY, rounding=ROUND_HALF_UP)
        if self.tax_amount > self.allocated_amount:
            raise ValueError("税额不能超过本期发票分配金额")
        return self


class PaymentPayload(IdempotentPayload):
    expected_version: int = Field(gt=0)
    payment_date: date
    amount: Decimal = Field(gt=0)
    reference: str | None = Field(default=None, max_length=200)

    @field_validator("amount")
    @classmethod
    def normalize_payment_amount(cls, value: Decimal) -> Decimal:
        return value.quantize(MONEY, rounding=ROUND_HALF_UP)

    @field_validator("reference")
    @classmethod
    def clean_reference(cls, value: str | None) -> str | None:
        return str(value or "").strip() or None


def _request_hash(action: str, payload: dict[str, Any]) -> str:
    body = json.dumps(
        {"action": action, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _replay(
    db: Session,
    *,
    key: str,
    request_hash: str,
    action: str,
    user: User,
) -> dict[str, Any] | None:
    record = db.scalar(
        select(FinanceIdempotencyRecord).where(
            FinanceIdempotencyRecord.idempotency_key == key
        )
    )
    if record is None:
        return None
    if (
        record.actor_user_id != user.id
        or record.action != action
        or record.request_hash != request_hash
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SUPPLIER_SETTLEMENT_IDEMPOTENCY_CONFLICT",
                "message": "该提交编号已用于不同内容，请刷新后重试",
            },
        )
    return json.loads(record.response_json)


def _record_replay(
    db: Session,
    *,
    key: str,
    request_hash: str,
    action: str,
    user: User,
    resource_id: int,
    response: dict[str, Any],
) -> None:
    db.add(
        FinanceIdempotencyRecord(
            idempotency_key=key,
            request_hash=request_hash,
            action=action,
            actor_user_id=user.id,
            resource_type="supplier_statement",
            resource_id=resource_id,
            response_json=json.dumps(
                response, ensure_ascii=False, sort_keys=True
            ),
        )
    )


def _translate(
    error: SupplierSettlementError | SupplierReceiptPriceFactError,
) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


def _audit(
    db: Session,
    *,
    user: User,
    action: str,
    row: SupplierMonthlyStatement | None,
    description: str,
    details: dict[str, Any],
) -> None:
    append_audit_event(
        db,
        event_category="business",
        result="success",
        source="web",
        module_code="supplier_monthly_settlement",
        action_code=action,
        resource="SupplierMonthlyStatement",
        legacy_action=action[:30],
        actor=user,
        entity_type="supplier_monthly_statement",
        entity_id=(row.id if row is not None else None),
        object_ref=(row.statement_number if row is not None else None),
        description=description,
        details=details,
    )


def _run_mutation(
    db: Session,
    *,
    user: User,
    key: str,
    action: str,
    payload: dict[str, Any],
    operation: Callable[[], tuple[dict[str, Any], SupplierMonthlyStatement | None]],
    description: str,
) -> dict[str, Any]:
    request_hash = _request_hash(action, payload)
    replay = _replay(
        db, key=key, request_hash=request_hash, action=action, user=user
    )
    if replay is not None:
        return replay
    try:
        response, row = operation()
        # Return and persist one canonical wire representation so an
        # idempotent retry is byte-for-byte equivalent to the first response.
        response = jsonable_encoder(response, custom_encoder={Decimal: str})
        _audit(
            db,
            user=user,
            action=action,
            row=row,
            description=description,
            details={"request": payload, "response": response},
        )
        _record_replay(
            db,
            key=key,
            request_hash=request_hash,
            action=action,
            user=user,
            resource_id=(row.id if row is not None else 0),
            response=response,
        )
        db.commit()
        return response
    except (SupplierSettlementError, SupplierReceiptPriceFactError) as error:
        db.rollback()
        raise _translate(error) from error
    except IntegrityError as error:
        db.rollback()
        replay = _replay(
            db, key=key, request_hash=request_hash, action=action, user=user
        )
        if replay is not None:
            return replay
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SUPPLIER_SETTLEMENT_CONCURRENT_WRITE",
                "message": "供应商月结正在被其他操作修改，请刷新后重试",
            },
        ) from error
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/supplier-settlements")
def list_supplier_settlements(
    settlement_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    db: Session = Depends(get_db),
    _user: User = Depends(company_read),
) -> dict[str, Any]:
    selected = settlement_month or default_closed_settlement_month()
    _candidates, issues, period_start, period_end = scan_settlement_candidates(
        db,
        settlement_month=selected,
    )
    return {
        "settlement_month": selected,
        "period_start": period_start,
        "period_end": period_end,
        "default_month": default_closed_settlement_month(),
        "items": list_statement_responses(db, settlement_month=selected),
        "issues": issues,
    }


@router.get("/supplier-settlements/price-adoptions/preview")
def preview_supplier_receipt_price_adoptions(
    settlement_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    db: Session = Depends(get_db),
    _user: User = Depends(company_read),
) -> dict[str, Any]:
    selected = settlement_month or default_closed_settlement_month()
    _start, _end, start_utc, end_utc = settlement_period_utc_bounds(selected)
    return preview_historical_price_adoptions(
        db,
        settlement_month=selected,
        start_utc=start_utc,
        end_utc=end_utc,
    )


@router.post("/supplier-settlements/generate")
def generate_supplier_settlements(
    payload: GeneratePayload,
    db: Session = Depends(get_db),
    user: User = Depends(company_execute),
) -> dict[str, Any]:
    values = payload.model_dump(exclude={"idempotency_key"})

    def operation():
        response = generate_or_refresh_settlements(
            db, settlement_month=payload.settlement_month, user=user
        )
        first_id = response["items"][0]["id"] if response["items"] else None
        row = db.get(SupplierMonthlyStatement, first_id) if first_id else None
        return response, row

    return _run_mutation(
        db,
        user=user,
        key=payload.idempotency_key,
        action="SUPPLIER_SETTLEMENT_GENERATE",
        payload=values,
        operation=operation,
        description="生成或刷新供应商20日月结草稿",
    )


@router.post("/supplier-settlements/{statement_id}/review")
def save_supplier_statement_review(
    statement_id: int,
    payload: ReviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(company_execute),
) -> dict[str, Any]:
    values = payload.model_dump(exclude={"idempotency_key"})

    def operation():
        row = review_statement(db, statement_id=statement_id, user=user, **values)
        return statement_response(db, row), row

    return _run_mutation(
        db,
        user=user,
        key=payload.idempotency_key,
        action="SUPPLIER_SETTLEMENT_REVIEW",
        payload={"statement_id": statement_id, **values},
        operation=operation,
        description="登记供应商实际对账单",
    )


@router.post("/supplier-settlements/{statement_id}/adjustments", status_code=201)
def create_supplier_statement_adjustment(
    statement_id: int,
    payload: AdjustmentPayload,
    db: Session = Depends(get_db),
    user: User = Depends(company_execute),
) -> dict[str, Any]:
    values = payload.model_dump(exclude={"idempotency_key"})

    def operation():
        row, adjustment = add_adjustment(
            db, statement_id=statement_id, user=user, **values
        )
        response = statement_response(db, row)
        response["created_adjustment_id"] = adjustment.id
        return response, row

    return _run_mutation(
        db,
        user=user,
        key=payload.idempotency_key,
        action="SUPPLIER_SETTLEMENT_ADJUST",
        payload={"statement_id": statement_id, **values},
        operation=operation,
        description="登记供应商月结差异调整",
    )


@router.post("/supplier-settlements/{statement_id}/confirm")
def confirm_supplier_statement(
    statement_id: int,
    payload: TransitionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(company_execute),
) -> dict[str, Any]:
    values = payload.model_dump(exclude={"idempotency_key"})

    def operation():
        row = confirm_statement(db, statement_id=statement_id, user=user, **values)
        return statement_response(db, row), row

    return _run_mutation(
        db,
        user=user,
        key=payload.idempotency_key,
        action="SUPPLIER_SETTLEMENT_CONFIRM",
        payload={"statement_id": statement_id, **values},
        operation=operation,
        description="人工确认供应商应付",
    )


@router.post("/supplier-settlements/{statement_id}/reopen")
def reopen_supplier_statement(
    statement_id: int,
    payload: TransitionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(company_execute),
) -> dict[str, Any]:
    values = payload.model_dump(exclude={"idempotency_key"})

    def operation():
        row = reopen_statement(db, statement_id=statement_id, user=user, **values)
        return statement_response(db, row), row

    return _run_mutation(
        db,
        user=user,
        key=payload.idempotency_key,
        action="SUPPLIER_SETTLEMENT_REOPEN",
        payload={"statement_id": statement_id, **values},
        operation=operation,
        description="受控重开未付款供应商月结",
    )


@router.post("/supplier-settlements/{statement_id}/invoices", status_code=201)
def create_supplier_invoice(
    statement_id: int,
    payload: InvoicePayload,
    db: Session = Depends(get_db),
    user: User = Depends(company_execute),
) -> dict[str, Any]:
    values = payload.model_dump(exclude={"idempotency_key"})

    def operation():
        row, invoice = add_invoice(
            db, statement_id=statement_id, user=user, **values
        )
        response = statement_response(db, row)
        response["created_invoice_id"] = invoice.id
        return response, row

    return _run_mutation(
        db,
        user=user,
        key=payload.idempotency_key,
        action="SUPPLIER_SETTLEMENT_INVOICE",
        payload={"statement_id": statement_id, **values},
        operation=operation,
        description="登记供应商发票分配",
    )


@router.post("/supplier-settlements/{statement_id}/payments", status_code=201)
def create_supplier_payment(
    statement_id: int,
    payload: PaymentPayload,
    db: Session = Depends(get_db),
    user: User = Depends(company_execute),
) -> dict[str, Any]:
    values = payload.model_dump(exclude={"idempotency_key"})

    def operation():
        row, payment = add_payment(
            db, statement_id=statement_id, user=user, **values
        )
        response = statement_response(db, row)
        response["created_payment_id"] = payment.id
        return response, row

    return _run_mutation(
        db,
        user=user,
        key=payload.idempotency_key,
        action="SUPPLIER_SETTLEMENT_PAYMENT",
        payload={"statement_id": statement_id, **values},
        operation=operation,
        description="登记供应商分次付款",
    )


def _attachment_root() -> Path:
    configured = os.getenv("ERP_SUPPLIER_INVOICE_ATTACHMENT_DIR")
    if configured:
        return Path(configured)
    return load_settings().database_path.parent / "supplier_invoice_attachments"


def _invoice_for_statement(
    db: Session, statement_id: int, invoice_id: int
) -> tuple[SupplierMonthlyStatement, SupplierMonthlyInvoice]:
    statement = db.get(SupplierMonthlyStatement, statement_id)
    invoice = db.get(SupplierMonthlyInvoice, invoice_id)
    if (
        statement is None
        or statement.active_guard != 1
        or invoice is None
        or invoice.statement_id != statement.id
    ):
        raise HTTPException(status_code=404, detail="供应商发票不存在")
    return statement, invoice


@router.post(
    "/supplier-settlements/{statement_id}/invoices/{invoice_id}/attachment",
    status_code=201,
)
async def upload_supplier_invoice_attachment(
    statement_id: int,
    invoice_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(can_attachment_manage),
) -> dict[str, Any]:
    _company_scope(user, db)
    statement, invoice = _invoice_for_statement(db, statement_id, invoice_id)
    content = await file.read()
    try:
        stored = store_original_invoice_pdf(
            storage_root=_attachment_root(),
            original_filename=file.filename or "",
            content=content,
        )
    except InvoiceAttachmentError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if invoice.attachment_content_hash:
        if invoice.attachment_content_hash != stored.sha256:
            raise HTTPException(
                status_code=409,
                detail="该供应商发票已经归档原件，不能覆盖；请核对发票号码",
            )
        return {
            "invoice_id": invoice.id,
            "content_hash": invoice.attachment_content_hash,
            "file_size": invoice.attachment_size,
            "reused": True,
        }
    invoice.attachment_original_name = stored.original_filename
    invoice.attachment_stored_name = stored.path.relative_to(
        _attachment_root()
    ).as_posix()
    invoice.attachment_content_hash = stored.sha256
    invoice.attachment_size = stored.size
    invoice.attached_by = user.id
    from app.core.time_contract import utc_now_naive

    invoice.attached_at = utc_now_naive()
    _audit(
        db,
        user=user,
        action="SUPPLIER_INVOICE_ATTACHMENT",
        row=statement,
        description="归档供应商发票原始PDF",
        details={
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "content_hash": stored.sha256,
            "file_size": stored.size,
        },
    )
    db.commit()
    return {
        "invoice_id": invoice.id,
        "content_hash": stored.sha256,
        "file_size": stored.size,
        "reused": stored.reused,
    }


@router.get(
    "/supplier-settlements/{statement_id}/invoices/{invoice_id}/attachment"
)
def download_supplier_invoice_attachment(
    statement_id: int,
    invoice_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_attachment_view),
) -> FileResponse:
    _company_scope(user, db)
    _statement, invoice = _invoice_for_statement(db, statement_id, invoice_id)
    if not invoice.attachment_stored_name or not invoice.attachment_original_name:
        raise HTTPException(status_code=404, detail="供应商发票尚未上传附件")
    root = _attachment_root().resolve()
    path = (root / invoice.attachment_stored_name).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise HTTPException(status_code=409, detail="供应商发票附件路径异常") from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="供应商发票附件文件不存在")
    return FileResponse(
        path, media_type="application/pdf", filename=invoice.attachment_original_name
    )
