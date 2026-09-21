"""FIN-001 manual invoice-task APIs.

The ERP creates a reviewable tax-bureau file only.  It never logs in to the
tax bureau and never treats a download as an issued invoice.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.core.config import PROJECT_ROOT, load_settings
from app.models.customer import Customer
from app.models.customer_charge import CustomerCharge
from app.models.delivery import DeliveryItem
from app.models.finance import (
    FinanceManualMutation,
    Invoice,
    ReturnReceiptItem,
    SettlementRecord,
    Statement,
    StatementAdjustment,
    StatementItem,
)
from app.models.invoice_task import (
    CustomerInvoiceItemRule,
    CustomerInvoiceProfile,
    CustomerInvoiceSellerChange,
    FinanceInvoiceAttachment,
    FinanceInvoiceTask,
    FinanceInvoiceTaskItem,
    FinanceSettlementEntity,
    InvoiceSellerEntity,
)
from app.models.order import OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.audit_log import append_audit_event
from app.services.invoice_attachments import (
    InvoiceAttachmentError,
    create_organized_invoice_pdf,
    store_original_invoice_pdf,
)
from app.services.invoice_tax_template import (
    InvoiceTaxTemplateError,
    generate_invoice_tax_template,
)
from app.services.product_specification import resolved_product_specification
from app.services.customer_price_tax import VALID_PRICE_TAX_MODES


router = APIRouter()
customer_router = APIRouter()

can_read = PermissionChecker("finance.view")
can_confirm_statement = PermissionChecker("finance.statement.confirm")
can_generate = PermissionChecker("finance.invoice_task.generate")
can_register = PermissionChecker("finance.invoice_result.register")
can_profile_manage = PermissionChecker("finance.invoice_profile.manage")
can_attachment_view = PermissionChecker("finance.invoice_attachment.view")
can_attachment_manage = PermissionChecker("finance.invoice_attachment.manage")

MONEY = Decimal("0.01")
TAX_TEMPLATE_PATH = (
    PROJECT_ROOT
    / "app"
    / "resources"
    / "invoice_tax_templates"
    / "发票开具项目信息导入模板.xlsx"
)


class VersionPayload(BaseModel):
    expected_version: int = Field(ge=1)


class SellerPayload(BaseModel):
    seller_code: str = Field(min_length=1, max_length=40)
    seller_name: str = Field(min_length=1, max_length=200)
    tax_no: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=1000)
    phone: str | None = Field(default=None, max_length=100)
    bank_name: str | None = Field(default=None, max_length=200)
    bank_account: str | None = Field(default=None, max_length=200)
    is_enabled: bool = True
    effective_from: date | None = None
    effective_to: date | None = None
    confirmation_status: Literal["pending", "confirmed"] = "pending"
    expected_version: int | None = Field(default=None, ge=1)


class InvoiceProfilePayload(BaseModel):
    invoice_title: str | None = Field(default=None, max_length=200)
    tax_no: str | None = Field(default=None, max_length=100)
    invoice_address: str | None = Field(default=None, max_length=1000)
    invoice_phone: str | None = Field(default=None, max_length=100)
    invoice_address_phone: str | None = Field(default=None, max_length=1000)
    bank_name: str | None = Field(default=None, max_length=200)
    bank_account: str | None = Field(default=None, max_length=200)
    default_seller_id: int | None = Field(default=None, ge=1)
    settlement_entity_id: int | None = Field(default=None, ge=1)
    price_tax_mode: Literal["tax_inclusive", "tax_exclusive"] = "tax_inclusive"
    default_tax_rate: Decimal | None = Field(default=Decimal("0.13"), ge=0, le=1)
    is_enabled: bool = True
    confirmation_status: Literal["pending", "confirmed"] = "pending"
    expected_version: int = Field(ge=1)


class InvoiceRulePayload(BaseModel):
    product_id: int | None = Field(default=None, ge=1)
    project_name: str | None = Field(default=None, max_length=200)
    tax_classification_code: str | None = Field(default=None, max_length=80)
    unit: str | None = Field(default=None, max_length=40)
    tax_rate: Decimal | None = Field(default=None, ge=0, le=1)
    spec_source: Literal[
        "product_snapshot", "product_code_snapshot", "blank"
    ] = "product_code_snapshot"
    fill_unit_price: bool = False
    effective_from: date | None = None
    effective_to: date | None = None
    confirmation_status: Literal["pending", "confirmed"] = "pending"
    expected_version: int | None = Field(default=None, ge=1)


class SettlementEntityPayload(BaseModel):
    entity_code: str = Field(min_length=1, max_length=40)
    entity_name: str = Field(min_length=1, max_length=200)
    short_name: str | None = Field(default=None, max_length=50)
    tax_no: str | None = Field(default=None, max_length=100)
    invoice_address: str | None = Field(default=None, max_length=1000)
    invoice_phone: str | None = Field(default=None, max_length=100)
    bank_name: str | None = Field(default=None, max_length=200)
    bank_account: str | None = Field(default=None, max_length=200)
    default_seller_id: int | None = Field(default=None, ge=1)
    statement_cycle_start_day: int = Field(default=20, ge=1, le=28)
    is_enabled: bool = True
    confirmation_status: Literal["pending", "confirmed"] = "pending"
    expected_version: int | None = Field(default=None, ge=1)


class TaskCreatePayload(VersionPayload):
    idempotency_key: str = Field(min_length=8, max_length=160)
    seller_entity_id: int | None = Field(default=None, ge=1)
    seller_change_type: Literal["temporary", "permanent"] | None = None
    seller_change_reason: str | None = Field(default=None, max_length=500)
    confirm_permanent_change: bool = False


class TaskResultPayload(VersionPayload):
    status: Literal["issued", "failed"]
    invoice_number: str | None = Field(default=None, max_length=80)
    invoice_date: date | None = None
    failure_reason: str | None = Field(default=None, max_length=1000)
    expected_ledger_version: int | None = Field(default=None, ge=1)

    @field_validator("invoice_number", "failure_reason")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None


def _text(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _safe_export_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip(" .")
    return cleaned[:180] or "开票导入模板.xlsx"


def _audit(
    db: Session,
    *,
    user: User,
    action: str,
    resource: str,
    entity_id: int,
    customer: Customer | None,
    details: dict[str, Any],
    description: str,
) -> None:
    append_audit_event(
        db,
        event_category="business",
        result="success",
        source="web",
        module_code="finance",
        action_code=action,
        resource=resource,
        legacy_action=action,
        actor=user,
        entity_type=resource,
        entity_id=entity_id,
        customer_id=customer.id if customer else None,
        customer_name=customer.name if customer else None,
        description=description,
        details=details,
    )


def _customer_for_user(db: Session, customer_id: int, user: User) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    require_customer_access(customer_id, user, db)
    return customer


def _statement_for_user(db: Session, statement_id: int, user: User) -> Statement:
    statement = db.get(Statement, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="对账单不存在")
    customer_ids: set[int] = {statement.customer_id}
    if statement.settlement_customer_ids_snapshot_json:
        try:
            customer_ids.update(
                int(value)
                for value in json.loads(
                    statement.settlement_customer_ids_snapshot_json
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    customer_ids.update(
        int(value)
        for value in db.scalars(
            select(StatementItem.source_customer_id)
            .where(
                StatementItem.statement_id == statement.id,
                StatementItem.source_customer_id.is_not(None),
            )
            .distinct()
        ).all()
    )
    for customer_id in customer_ids:
        require_customer_access(customer_id, user, db)
    return statement


def _task_for_user(db: Session, task_id: int, user: User) -> FinanceInvoiceTask:
    task = db.get(FinanceInvoiceTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="开票任务不存在")
    _statement_for_user(db, task.statement_id, user)
    return task


def _seller_missing_items(seller: InvoiceSellerEntity | None) -> list[str]:
    if seller is None:
        return ["默认销方主体"]
    missing: list[str] = []
    if not seller.is_enabled:
        missing.append("销方已停用")
    if seller.confirmation_status != "confirmed":
        missing.append("销方资料未确认")
    for name, value in (
        ("销方名称", seller.seller_name),
        ("销方税号", seller.tax_no),
        ("销方地址", seller.address),
        ("销方电话", seller.phone),
        ("销方开户行", seller.bank_name),
        ("销方银行账号", seller.bank_account),
    ):
        if not _text(value):
            missing.append(name)
    return missing


def _profile_missing_items(
    profile: CustomerInvoiceProfile | None,
    seller: InvoiceSellerEntity | None,
    settlement_entity: FinanceSettlementEntity | None = None,
) -> list[str]:
    if profile is None:
        return ["客户开票档案"]
    missing: list[str] = []
    if not profile.is_enabled:
        missing.append("客户开票档案已停用")
    if profile.confirmation_status != "confirmed":
        missing.append("客户开票档案未确认")
    if settlement_entity is None:
        for name, value in (
            ("购方抬头", profile.invoice_title),
            ("购方税号", profile.tax_no),
        ):
            if not _text(value):
                missing.append(name)
    else:
        missing.extend(_settlement_entity_missing_items(settlement_entity))
    if profile.default_tax_rate is None:
        missing.append("默认税率")
    missing.extend(_seller_missing_items(seller))
    return list(dict.fromkeys(missing))


def _profile_response(profile: CustomerInvoiceProfile | None) -> dict[str, Any]:
    if profile is None:
        return {
            "invoice_title": "",
            "tax_no": "",
            "invoice_address": "",
            "invoice_phone": "",
            "invoice_address_phone": "",
            "bank_name": "",
            "bank_account": "",
            "default_seller_id": None,
            "settlement_entity_id": None,
            "price_tax_mode": "tax_inclusive",
            "default_tax_rate": Decimal("0.13"),
            "is_enabled": True,
            "confirmation_status": "pending",
            "version": 1,
        }
    address_phone = " ".join(
        item for item in (profile.invoice_address, profile.invoice_phone) if item
    )
    return {
        "id": profile.id,
        "customer_id": profile.customer_id,
        "invoice_title": profile.invoice_title or "",
        "tax_no": profile.tax_no or "",
        "invoice_address": profile.invoice_address or "",
        "invoice_phone": profile.invoice_phone or "",
        "invoice_address_phone": address_phone,
        "bank_name": profile.bank_name or "",
        "bank_account": profile.bank_account or "",
        "default_seller_id": profile.default_seller_id,
        "settlement_entity_id": profile.settlement_entity_id,
        "price_tax_mode": profile.price_tax_mode,
        "default_tax_rate": profile.default_tax_rate,
        "is_enabled": profile.is_enabled,
        "confirmation_status": profile.confirmation_status,
        "version": profile.version,
    }


def _seller_response(seller: InvoiceSellerEntity) -> dict[str, Any]:
    return {
        "id": seller.id,
        "seller_code": seller.seller_code,
        "seller_name": seller.seller_name,
        "tax_no": seller.tax_no,
        "address": seller.address,
        "phone": seller.phone,
        "bank_name": seller.bank_name,
        "bank_account": seller.bank_account,
        "is_enabled": seller.is_enabled,
        "effective_from": seller.effective_from,
        "effective_to": seller.effective_to,
        "confirmation_status": seller.confirmation_status,
        "version": seller.version,
    }


def _settlement_entity_response(row: FinanceSettlementEntity) -> dict[str, Any]:
    return {
        "id": row.id,
        "entity_code": row.entity_code,
        "entity_name": row.entity_name,
        "short_name": row.short_name,
        "tax_no": row.tax_no,
        "invoice_address": row.invoice_address,
        "invoice_phone": row.invoice_phone,
        "bank_name": row.bank_name,
        "bank_account": row.bank_account,
        "default_seller_id": row.default_seller_id,
        "statement_cycle_start_day": row.statement_cycle_start_day,
        "is_enabled": row.is_enabled,
        "confirmation_status": row.confirmation_status,
        "version": row.version,
    }


def _settlement_entity_missing_items(
    row: FinanceSettlementEntity | None,
) -> list[str]:
    if row is None:
        return ["结算对象"]
    missing: list[str] = []
    if not row.is_enabled:
        missing.append("结算对象已停用")
    if row.confirmation_status != "confirmed":
        missing.append("结算对象资料未确认")
    for label, value in (
        ("结算对象名称", row.entity_name),
        ("结算对象税号", row.tax_no),
    ):
        if not _text(value):
            missing.append(label)
    if row.default_seller_id is None:
        missing.append("结算对象默认销方")
    return missing


def _rule_response(rule: CustomerInvoiceItemRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "customer_id": rule.customer_id,
        "product_id": rule.product_id,
        "project_name": rule.project_name,
        "tax_classification_code": rule.tax_classification_code,
        "unit": rule.unit,
        "tax_rate": rule.tax_rate,
        "spec_source": rule.spec_source,
        "fill_unit_price": rule.fill_unit_price,
        "effective_from": rule.effective_from,
        "effective_to": rule.effective_to,
        "confirmation_status": rule.confirmation_status,
        "version": rule.version,
    }


def _default_rule_response() -> dict[str, Any]:
    return {
        "product_id": None,
        "project_name": "纸箱",
        "tax_classification_code": "1060105010000000000",
        "unit": "PCS",
        "tax_rate": Decimal("0.13"),
        "spec_source": "product_code_snapshot",
        "fill_unit_price": True,
        "effective_from": None,
        "effective_to": None,
        "confirmation_status": "pending",
        "version": 1,
    }


@router.get("/invoice-sellers")
def list_invoice_sellers(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, list[dict[str, Any]]]:
    query = select(InvoiceSellerEntity).order_by(InvoiceSellerEntity.seller_code)
    if enabled_only:
        query = query.where(InvoiceSellerEntity.is_enabled.is_(True))
    return {"items": [_seller_response(row) for row in db.scalars(query).all()]}


@router.post("/invoice-sellers", status_code=201)
def create_invoice_seller(
    payload: SellerPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, Any]:
    seller = InvoiceSellerEntity(
        seller_code=payload.seller_code.strip(),
        seller_name=payload.seller_name.strip(),
        tax_no=_text(payload.tax_no),
        address=_text(payload.address),
        phone=_text(payload.phone),
        bank_name=_text(payload.bank_name),
        bank_account=_text(payload.bank_account),
        is_enabled=payload.is_enabled,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        confirmation_status=payload.confirmation_status,
        confirmed_by=user.id if payload.confirmation_status == "confirmed" else None,
        confirmed_at=datetime.now() if payload.confirmation_status == "confirmed" else None,
    )
    missing = _seller_missing_items(seller)
    if payload.confirmation_status == "confirmed" and missing:
        raise HTTPException(status_code=409, detail={"message": "销方资料不完整", "missing_items": missing})
    try:
        db.add(seller)
        db.flush()
        _audit(db, user=user, action="CREATE_INVOICE_SELLER", resource="InvoiceSellerEntity", entity_id=seller.id, customer=None, details={"seller_code": seller.seller_code, "confirmation_status": seller.confirmation_status}, description="新增开票销方主体")
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="销方编号已存在") from error
    return _seller_response(seller)


@router.put("/invoice-sellers/{seller_id}")
def update_invoice_seller(
    seller_id: int,
    payload: SellerPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, Any]:
    seller = db.get(InvoiceSellerEntity, seller_id)
    if seller is None:
        raise HTTPException(status_code=404, detail="销方主体不存在")
    if payload.expected_version != seller.version:
        raise HTTPException(status_code=409, detail={"message": "销方资料已被修改，请刷新后重试", "current_version": seller.version})
    for field in ("seller_code", "seller_name", "tax_no", "address", "phone", "bank_name", "bank_account", "is_enabled", "effective_from", "effective_to", "confirmation_status"):
        value = getattr(payload, field)
        if isinstance(value, str):
            value = _text(value)
        setattr(seller, field, value)
    if seller.confirmation_status == "confirmed":
        missing = _seller_missing_items(seller)
        if missing:
            raise HTTPException(status_code=409, detail={"message": "销方资料不完整", "missing_items": missing})
        seller.confirmed_by = user.id
        seller.confirmed_at = datetime.now()
    else:
        seller.confirmed_by = None
        seller.confirmed_at = None
    seller.version += 1
    try:
        _audit(db, user=user, action="UPDATE_INVOICE_SELLER", resource="InvoiceSellerEntity", entity_id=seller.id, customer=None, details={"seller_code": seller.seller_code, "version": seller.version}, description="修改开票销方主体")
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="销方编号已存在") from error
    return _seller_response(seller)


@router.get("/settlement-entities")
def list_settlement_entities(
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, list[dict[str, Any]]]:
    rows = db.scalars(
        select(FinanceSettlementEntity).order_by(
            FinanceSettlementEntity.entity_name, FinanceSettlementEntity.id
        )
    ).all()
    return {"items": [_settlement_entity_response(row) for row in rows]}


@router.post("/settlement-entities", status_code=201)
def create_settlement_entity(
    payload: SettlementEntityPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, Any]:
    if not _text(payload.entity_name) or not _text(payload.tax_no):
        raise HTTPException(status_code=409, detail="结算对象开票抬头和税号必须填写")
    seller = (
        db.get(InvoiceSellerEntity, payload.default_seller_id)
        if payload.default_seller_id
        else None
    )
    if payload.default_seller_id and seller is None:
        raise HTTPException(status_code=409, detail="结算对象默认销方不存在")
    row = FinanceSettlementEntity(
        entity_code=payload.entity_code.strip(),
        entity_name=payload.entity_name.strip(),
        short_name=_text(payload.short_name),
        tax_no=_text(payload.tax_no),
        invoice_address=_text(payload.invoice_address),
        invoice_phone=_text(payload.invoice_phone),
        bank_name=_text(payload.bank_name),
        bank_account=_text(payload.bank_account),
        default_seller_id=payload.default_seller_id,
        statement_cycle_start_day=payload.statement_cycle_start_day,
        is_enabled=payload.is_enabled,
        confirmation_status=payload.confirmation_status,
    )
    if row.confirmation_status == "confirmed":
        missing = _settlement_entity_missing_items(row) + _seller_missing_items(seller)
        if missing:
            raise HTTPException(status_code=409, detail={"message": "结算对象资料不完整", "missing_items": list(dict.fromkeys(missing))})
        row.confirmed_by = user.id
        row.confirmed_at = datetime.now()
    db.add(row)
    try:
        db.flush()
        _audit(db, user=user, action="CREATE_SETTLEMENT_ENTITY", resource="FinanceSettlementEntity", entity_id=row.id, customer=None, details={"entity_code": row.entity_code}, description="新增合作结算对象")
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="结算对象编号已存在") from error
    return _settlement_entity_response(row)


@router.put("/settlement-entities/{entity_id}")
def update_settlement_entity(
    entity_id: int,
    payload: SettlementEntityPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, Any]:
    row = db.get(FinanceSettlementEntity, entity_id)
    if row is None:
        raise HTTPException(status_code=404, detail="结算对象不存在")
    if payload.expected_version != row.version:
        raise HTTPException(status_code=409, detail={"message": "结算对象已被修改", "current_version": row.version})
    if not _text(payload.entity_name) or not _text(payload.tax_no):
        raise HTTPException(status_code=409, detail="结算对象开票抬头和税号必须填写")
    seller = db.get(InvoiceSellerEntity, payload.default_seller_id) if payload.default_seller_id else None
    if payload.default_seller_id and seller is None:
        raise HTTPException(status_code=409, detail="结算对象默认销方不存在")
    for field in ("entity_code", "entity_name", "statement_cycle_start_day", "is_enabled", "confirmation_status", "default_seller_id"):
        setattr(row, field, getattr(payload, field))
    for field in ("short_name", "tax_no", "invoice_address", "invoice_phone", "bank_name", "bank_account"):
        setattr(row, field, _text(getattr(payload, field)))
    row.entity_code = row.entity_code.strip()
    row.entity_name = row.entity_name.strip()
    row.version += 1
    if row.confirmation_status == "confirmed":
        missing = _settlement_entity_missing_items(row) + _seller_missing_items(seller)
        if missing:
            raise HTTPException(status_code=409, detail={"message": "结算对象资料不完整", "missing_items": list(dict.fromkeys(missing))})
        row.confirmed_by = user.id
        row.confirmed_at = datetime.now()
    else:
        row.confirmed_by = None
        row.confirmed_at = None
    try:
        _audit(db, user=user, action="UPDATE_SETTLEMENT_ENTITY", resource="FinanceSettlementEntity", entity_id=row.id, customer=None, details={"entity_code": row.entity_code, "version": row.version}, description="修改合作结算对象")
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="结算对象编号已存在") from error
    return _settlement_entity_response(row)


@customer_router.get("/{customer_id}/invoice-profile")
def get_customer_invoice_profile(
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, Any]:
    _customer_for_user(db, customer_id, user)
    profile = db.scalar(select(CustomerInvoiceProfile).where(CustomerInvoiceProfile.customer_id == customer_id))
    return _profile_response(profile)


@customer_router.put("/{customer_id}/invoice-profile")
def save_customer_invoice_profile(
    customer_id: int,
    payload: InvoiceProfilePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, Any]:
    customer = _customer_for_user(db, customer_id, user)
    profile = db.scalar(select(CustomerInvoiceProfile).where(CustomerInvoiceProfile.customer_id == customer_id))
    if profile is not None and payload.expected_version != profile.version:
        raise HTTPException(status_code=409, detail={"message": "客户开票资料已被修改，请刷新后重试", "current_version": profile.version})
    if profile is None and payload.expected_version != 1:
        raise HTTPException(status_code=409, detail="客户开票资料尚未建立，请以版本 1 保存")
    combined = _text(payload.invoice_address_phone)
    invoice_address = _text(payload.invoice_address) or combined
    invoice_phone = _text(payload.invoice_phone)
    seller = db.get(InvoiceSellerEntity, payload.default_seller_id) if payload.default_seller_id else None
    if payload.default_seller_id and seller is None:
        raise HTTPException(status_code=409, detail="默认销方主体不存在")
    settlement_entity = (
        db.get(FinanceSettlementEntity, payload.settlement_entity_id)
        if payload.settlement_entity_id
        else None
    )
    if payload.settlement_entity_id and settlement_entity is None:
        raise HTTPException(status_code=409, detail="结算对象不存在")
    required_buyer = (("购方抬头", settlement_entity.entity_name if settlement_entity else payload.invoice_title),
                      ("购方税号", settlement_entity.tax_no if settlement_entity else payload.tax_no))
    missing_buyer = [label for label, value in required_buyer if not _text(value)]
    if missing_buyer:
        raise HTTPException(status_code=409, detail={"message": "开票抬头和税号必须填写", "missing_items": missing_buyer})
    if profile is None:
        profile = CustomerInvoiceProfile(customer_id=customer_id)
        db.add(profile)
    profile.invoice_title = _text(payload.invoice_title)
    profile.tax_no = _text(payload.tax_no)
    profile.invoice_address = invoice_address
    profile.invoice_phone = invoice_phone
    profile.bank_name = _text(payload.bank_name)
    profile.bank_account = _text(payload.bank_account)
    profile.default_seller_id = payload.default_seller_id
    profile.settlement_entity_id = payload.settlement_entity_id
    profile.price_tax_mode = payload.price_tax_mode
    profile.default_tax_rate = payload.default_tax_rate
    profile.is_enabled = payload.is_enabled
    profile.confirmation_status = payload.confirmation_status
    if profile.id is not None:
        profile.version += 1
    if profile.confirmation_status == "confirmed":
        effective_seller = (
            db.get(InvoiceSellerEntity, settlement_entity.default_seller_id)
            if settlement_entity is not None and settlement_entity.default_seller_id
            else seller
        )
        missing = _profile_missing_items(profile, effective_seller, settlement_entity)
        has_rule = db.scalar(select(CustomerInvoiceItemRule.id).where(CustomerInvoiceItemRule.customer_id == customer_id, CustomerInvoiceItemRule.product_id.is_(None), CustomerInvoiceItemRule.confirmation_status == "confirmed").limit(1))
        if not has_rule:
            missing.append("已确认的客户默认项目规则")
        if missing:
            raise HTTPException(status_code=409, detail={"message": "客户开票资料不完整", "missing_items": missing})
        profile.confirmed_by = user.id
        profile.confirmed_at = datetime.now()
    else:
        profile.confirmed_by = None
        profile.confirmed_at = None
    db.flush()
    _audit(db, user=user, action="SAVE_CUSTOMER_INVOICE_PROFILE", resource="CustomerInvoiceProfile", entity_id=profile.id, customer=customer, details={"version": profile.version, "confirmation_status": profile.confirmation_status}, description="保存客户开票资料")
    db.commit()
    return _profile_response(profile)


@customer_router.get("/{customer_id}/invoice-item-rules")
def list_customer_invoice_rules(
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, list[dict[str, Any]]]:
    _customer_for_user(db, customer_id, user)
    rows = db.scalars(select(CustomerInvoiceItemRule).where(CustomerInvoiceItemRule.customer_id == customer_id).order_by(CustomerInvoiceItemRule.product_id.is_(None).desc(), CustomerInvoiceItemRule.id)).all()
    return {"items": [_rule_response(row) for row in rows]}


@customer_router.get("/{customer_id}/invoice-item-rules/default")
def get_customer_default_invoice_rule(
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, Any]:
    _customer_for_user(db, customer_id, user)
    rule = db.scalar(
        select(CustomerInvoiceItemRule)
        .where(
            CustomerInvoiceItemRule.customer_id == customer_id,
            CustomerInvoiceItemRule.product_id.is_(None),
        )
        .order_by(CustomerInvoiceItemRule.version.desc(), CustomerInvoiceItemRule.id.desc())
        .limit(1)
    )
    return _rule_response(rule) if rule is not None else _default_rule_response()


@customer_router.put("/{customer_id}/invoice-item-rules/default")
def save_customer_default_invoice_rule(
    customer_id: int,
    payload: InvoiceRulePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_profile_manage),
) -> dict[str, Any]:
    customer = _customer_for_user(db, customer_id, user)
    if payload.product_id is not None:
        raise HTTPException(status_code=400, detail="客户默认项目规则不能绑定具体常用箱")
    rule = db.scalar(select(CustomerInvoiceItemRule).where(CustomerInvoiceItemRule.customer_id == customer_id, CustomerInvoiceItemRule.product_id.is_(None)).order_by(CustomerInvoiceItemRule.version.desc()).limit(1))
    if rule is not None and payload.expected_version is not None and payload.expected_version != rule.version:
        raise HTTPException(status_code=409, detail={"message": "项目规则已被修改，请刷新后重试", "current_version": rule.version})
    if rule is None:
        rule = CustomerInvoiceItemRule(customer_id=customer_id, product_id=None)
        db.add(rule)
    else:
        rule.version += 1
    for field in ("project_name", "tax_classification_code", "unit", "tax_rate", "spec_source", "fill_unit_price", "effective_from", "effective_to", "confirmation_status"):
        value = getattr(payload, field)
        if isinstance(value, str):
            value = _text(value)
        setattr(rule, field, value)
    if rule.confirmation_status == "confirmed":
        missing = [name for name, value in (("项目名称", rule.project_name), ("税收分类编码", rule.tax_classification_code), ("单位", rule.unit), ("税率", rule.tax_rate)) if value is None or (isinstance(value, str) and not value.strip())]
        if missing:
            raise HTTPException(status_code=409, detail={"message": "项目规则不完整", "missing_items": missing})
        rule.confirmed_by = user.id
        rule.confirmed_at = datetime.now()
    else:
        rule.confirmed_by = None
        rule.confirmed_at = None
    db.flush()
    _audit(db, user=user, action="SAVE_CUSTOMER_INVOICE_RULE", resource="CustomerInvoiceItemRule", entity_id=rule.id, customer=customer, details={"version": rule.version, "confirmation_status": rule.confirmation_status}, description="保存客户默认开票项目规则")
    db.commit()
    return _rule_response(rule)


def _task_rows(
    db: Session,
    statement_id: int,
) -> list[
    tuple[
        StatementItem,
        DeliveryItem | None,
        OrderItem | None,
        Product | None,
        CustomerCharge | None,
    ]
]:
    return list(
        db.execute(
            select(StatementItem, DeliveryItem, OrderItem, Product, CustomerCharge)
            .outerjoin(
                ReturnReceiptItem,
                StatementItem.return_receipt_item_id == ReturnReceiptItem.id,
            )
            .outerjoin(
                DeliveryItem,
                ReturnReceiptItem.delivery_item_id == DeliveryItem.id,
            )
            .outerjoin(OrderItem, DeliveryItem.order_item_id == OrderItem.id)
            .outerjoin(Product, Product.id == DeliveryItem.product_id)
            .outerjoin(
                CustomerCharge,
                StatementItem.customer_charge_id == CustomerCharge.id,
            )
            .where(StatementItem.statement_id == statement_id)
            .order_by(StatementItem.id)
        ).all()
    )


def _confirmed_rule_by_product(
    db: Session,
    customer_id: int,
    *,
    effective_on: date,
) -> dict[int | None, CustomerInvoiceItemRule]:
    rows = db.scalars(
        select(CustomerInvoiceItemRule)
        .where(
            CustomerInvoiceItemRule.customer_id == customer_id,
            CustomerInvoiceItemRule.confirmation_status == "confirmed",
            (CustomerInvoiceItemRule.effective_from.is_(None))
            | (CustomerInvoiceItemRule.effective_from <= effective_on),
            (CustomerInvoiceItemRule.effective_to.is_(None))
            | (CustomerInvoiceItemRule.effective_to >= effective_on),
        )
        .order_by(CustomerInvoiceItemRule.version.desc(), CustomerInvoiceItemRule.id.desc())
    ).all()
    result: dict[int | None, CustomerInvoiceItemRule] = {}
    for row in rows:
        result.setdefault(row.product_id, row)
    return result


def _snapshot_for_statement(
    db: Session,
    statement: Statement,
    seller: InvoiceSellerEntity,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    profile = db.scalar(select(CustomerInvoiceProfile).where(CustomerInvoiceProfile.customer_id == statement.customer_id))
    settlement_entity = (
        db.get(FinanceSettlementEntity, statement.settlement_entity_id)
        if statement.settlement_entity_id
        else None
    )
    missing = _profile_missing_items(profile, seller, settlement_entity)
    if profile is None:
        return {}, [], missing
    task_rows = _task_rows(db, statement.id)
    source_customer_ids = {
        int(statement_item.source_customer_id or statement.customer_id)
        for statement_item, *_rest in task_rows
    }
    profiles_by_customer = {
        row.customer_id: row
        for row in db.scalars(
            select(CustomerInvoiceProfile).where(
                CustomerInvoiceProfile.customer_id.in_(source_customer_ids)
            )
        ).all()
    }
    rules_by_customer = {
        source_customer_id: _confirmed_rule_by_product(
            db,
            source_customer_id,
            effective_on=date.today(),
        )
        for source_customer_id in source_customer_ids
    }
    delivery_customer_ids = {
        int(statement_item.source_customer_id or statement.customer_id)
        for statement_item, delivery_item, *_rest in task_rows
        if delivery_item is not None
    }
    for source_customer_id in sorted(source_customer_ids):
        source_profile = profiles_by_customer.get(source_customer_id)
        if source_profile is None:
            missing.append(f"客户 {source_customer_id} 缺少开票档案")
            continue
        if not source_profile.is_enabled or source_profile.confirmation_status != "confirmed":
            missing.append(f"客户 {source_customer_id} 的开票档案未确认或已停用")
        if source_profile.default_tax_rate is None:
            missing.append(f"客户 {source_customer_id} 缺少默认税率")
        if (
            source_customer_id in delivery_customer_ids
            and rules_by_customer[source_customer_id].get(None) is None
        ):
            missing.append(f"客户 {source_customer_id} 缺少已确认的默认项目规则")
    lines: list[dict[str, Any]] = []
    for statement_item, delivery_item, order_item, product, charge in task_rows:
        source_customer_id = int(
            statement_item.source_customer_id or statement.customer_id
        )
        source_profile = profiles_by_customer.get(source_customer_id) or profile
        rules = rules_by_customer.get(source_customer_id, {})
        default_rule = rules.get(None)
        if charge is not None:
            mode = statement_item.price_tax_mode_snapshot
            rate_value = statement_item.tax_rate_snapshot
            project_name = _text(charge.tax_project_name)
            classification_code = _text(charge.tax_classification_code)
            unit = _text(statement_item.unit_snapshot or charge.unit)
            if (
                mode not in VALID_PRICE_TAX_MODES
                or rate_value is None
                or not project_name
                or not classification_code
                or not unit
                or statement_item.receivable_amount is None
                or statement_item.charge_quantity_snapshot is None
            ):
                missing.append(
                    f"附加收费 {charge.display_name} 缺少税价口径/税率/项目名称/税收编码/单位"
                )
                continue
            rate = Decimal(str(rate_value))
            if rate < 0 or rate > 1:
                missing.append(f"附加收费 {charge.display_name} 的冻结税率无效")
                continue
            total = Decimal(str(statement_item.receivable_amount)).quantize(
                MONEY, rounding=ROUND_HALF_UP
            )
            quantity = Decimal(str(statement_item.charge_quantity_snapshot))
            if mode == "tax_exclusive":
                raw_unit_price = Decimal(str(statement_item.unit_price_snapshot))
                net = (quantity * raw_unit_price).quantize(
                    MONEY, rounding=ROUND_HALF_UP
                )
                tax = (net * rate).quantize(MONEY, rounding=ROUND_HALF_UP)
                calculated_total = (net + tax).quantize(MONEY, rounding=ROUND_HALF_UP)
                if calculated_total != total:
                    missing.append(
                        f"附加收费 {charge.display_name} 的价税合计与未税单价不一致"
                    )
                    continue
                amount = net
                unit_price = raw_unit_price.quantize(MONEY, rounding=ROUND_HALF_UP)
            else:
                net = (total / (Decimal("1") + rate)).quantize(
                    MONEY, rounding=ROUND_HALF_UP
                )
                tax = (total - net).quantize(MONEY, rounding=ROUND_HALF_UP)
                amount = total
                unit_price = None
            lines.append(
                {
                    "statement_item_id": statement_item.id,
                    "source_type": "customer_charge",
                    "customer_charge_id": charge.id,
                    "product_id": None,
                    "product_code_snapshot": None,
                    "product_name_snapshot": charge.display_name,
                    "project_name": project_name,
                    "tax_classification_code": classification_code,
                    "specification": None,
                    "unit": unit,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "amount": amount,
                    "tax_rate": rate,
                    "tax_amount": tax,
                    "net_amount": net,
                    "total_amount": total,
                    "price_tax_mode": mode,
                    "rule_version": charge.version,
                }
            )
            continue
        if delivery_item is None:
            missing.append(f"对账明细 {statement_item.id} 的来源无效")
            continue
        product_id = delivery_item.product_id or (order_item.product_id if order_item else None)
        rule = rules.get(product_id) or default_rule
        if rule is None:
            missing.append(f"对账明细 {statement_item.id} 的项目规则")
            continue
        code = delivery_item.product_code_snapshot or (order_item.snapshot_product_code if order_item else None) or (product.product_code if product else None)
        name = delivery_item.product_name_snapshot or (order_item.snapshot_product_name if order_item else None) or (product.product_name if product else None)
        spec = resolved_product_specification(
            delivery_item.specification_snapshot,
            product,
            fallback_snapshots=(order_item.snapshot_spec if order_item else None,),
        )
        if not all((_text(rule.project_name), _text(rule.tax_classification_code), _text(rule.unit), rule.tax_rate is not None, statement_item.receivable_amount is not None)):
            missing.append(f"对账明细 {statement_item.id} 的项目名称/税收编码/单位/税率")
            continue
        mode = (
            statement_item.price_tax_mode_snapshot
            if statement_item.price_tax_mode_snapshot in VALID_PRICE_TAX_MODES
            else (
                order_item.price_tax_mode_snapshot
                if order_item is not None
                and order_item.price_tax_mode_snapshot in VALID_PRICE_TAX_MODES
                else source_profile.price_tax_mode
            )
        )
        if mode not in VALID_PRICE_TAX_MODES:
            missing.append(f"对账明细 {statement_item.id} 缺少有效税价口径")
            continue
        rate = Decimal(
            str(
                statement_item.tax_rate_snapshot
                if statement_item.tax_rate_snapshot is not None
                else (
                    order_item.tax_rate_snapshot
                    if order_item is not None and order_item.tax_rate_snapshot is not None
                    else (
                        source_profile.default_tax_rate
                        if source_profile.default_tax_rate is not None
                        else rule.tax_rate
                    )
                )
            )
        )
        if rate < 0 or rate > 1:
            missing.append(f"对账明细 {statement_item.id} 的冻结税率无效")
            continue
        total = Decimal(str(statement_item.receivable_amount)).quantize(
            MONEY, rounding=ROUND_HALF_UP
        )
        quantity = Decimal(str(statement_item.actual_received_quantity))
        if mode == "tax_exclusive":
            raw_unit_price = Decimal(str(statement_item.unit_price_snapshot))
            net = (quantity * raw_unit_price).quantize(
                MONEY, rounding=ROUND_HALF_UP
            )
            tax = (net * rate).quantize(MONEY, rounding=ROUND_HALF_UP)
            calculated_total = (net + tax).quantize(MONEY, rounding=ROUND_HALF_UP)
            if calculated_total != total:
                missing.append(
                    f"对账明细 {statement_item.id} 的价税合计与冻结未税单价不一致"
                )
                continue
            amount = net
            unit_price = (
                raw_unit_price.quantize(MONEY, rounding=ROUND_HALF_UP)
                if rule.fill_unit_price
                else None
            )
        else:
            net = (total / (Decimal("1") + rate)).quantize(
                MONEY, rounding=ROUND_HALF_UP
            )
            tax = (total - net).quantize(MONEY, rounding=ROUND_HALF_UP)
            amount = total
            unit_price = None
        if rule.spec_source == "product_code_snapshot":
            invoice_specification = code
        elif rule.spec_source == "product_snapshot":
            invoice_specification = spec
        else:
            invoice_specification = None
        lines.append({
            "statement_item_id": statement_item.id,
            "source_type": "delivery",
            "customer_charge_id": None,
            "product_id": product_id,
            "product_code_snapshot": code,
            "product_name_snapshot": name,
            "project_name": rule.project_name,
            "tax_classification_code": rule.tax_classification_code,
            "specification": invoice_specification,
            "unit": rule.unit,
            "quantity": quantity,
            "unit_price": unit_price,
            "amount": amount,
            "tax_rate": rate,
            "tax_amount": tax,
            "net_amount": net,
            "total_amount": total,
            "price_tax_mode": mode,
            "rule_version": rule.version,
        })
    if not lines:
        missing.append("可开票对账明细")
    snapshot = {
        "statement_id": statement.id,
        "statement_version": statement.version,
        "customer_id": statement.customer_id,
        "profile_versions": {
            str(customer_id): source_profile.version
            for customer_id, source_profile in sorted(profiles_by_customer.items())
        },
        "settlement_entity_id": (
            settlement_entity.id if settlement_entity is not None else None
        ),
        "settlement_entity_version": (
            settlement_entity.version if settlement_entity is not None else None
        ),
        "seller_id": seller.id,
        "seller_version": seller.version,
        "price_tax_mode": (
            next(iter({line["price_tax_mode"] for line in lines}))
            if len({line["price_tax_mode"] for line in lines}) == 1
            else "mixed"
        ),
        "lines": [
            {
                **{key: (format(value, "f") if isinstance(value, Decimal) else value) for key, value in line.items()},
            }
            for line in lines
        ],
    }
    return snapshot, lines, list(dict.fromkeys(missing))


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest().upper()


def _task_response(db: Session, task: FinanceInvoiceTask) -> dict[str, Any]:
    customer = db.get(Customer, task.customer_id)
    seller = db.get(InvoiceSellerEntity, task.seller_entity_id)
    statement = db.get(Statement, task.statement_id)
    return {
        "id": task.id,
        "task_number": task.task_number,
        "statement_id": task.statement_id,
        "statement_month": statement.statement_month if statement else None,
        "statement_version": task.statement_version,
        "ledger_version": statement.ledger_version if statement else None,
        "customer_id": task.customer_id,
        "customer_name": (
            statement.settlement_name_snapshot
            if statement and statement.settlement_name_snapshot
            else (customer.name if customer else "")
        ),
        "seller_entity_id": task.seller_entity_id,
        "seller_name": seller.seller_name if seller else "",
        "price_tax_mode": task.price_tax_mode,
        "net_amount": task.net_amount,
        "tax_amount": task.tax_amount,
        "total_amount": task.total_amount,
        "status": task.status,
        "version": task.version,
        "last_export_hash": task.last_export_hash,
        "missing_items": [],
    }


def _invoice_task_result_request_hash(
    task: FinanceInvoiceTask,
    payload: TaskResultPayload,
) -> str:
    encoded = json.dumps(
        {
            "mutation_type": "register_invoice_task",
            "task_id": task.id,
            "statement_id": task.statement_id,
            "status": payload.status,
            "invoice_number": payload.invoice_number,
            "invoice_date": (
                payload.invoice_date.isoformat() if payload.invoice_date else None
            ),
            "failure_reason": payload.failure_reason,
            "expected_version": payload.expected_version,
            "expected_ledger_version": payload.expected_ledger_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest().upper()


def _invoice_task_result_replay(
    mutation: FinanceManualMutation,
    *,
    statement_id: int,
    request_hash: str,
    actor_id: int,
) -> dict[str, Any]:
    if (
        mutation.mutation_type != "register_invoice_task"
        or mutation.statement_id != statement_id
        or mutation.request_hash != request_hash
        or mutation.actor_id != actor_id
    ):
        raise HTTPException(
            status_code=409,
            detail="该开票任务成功结果已由不同操作者或不同请求载荷登记",
        )
    try:
        response = json.loads(mutation.response_json)
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=500,
            detail="开票任务幂等响应事实损坏，请停止重试并联系管理员",
        ) from error
    if not isinstance(response, dict):
        raise HTTPException(
            status_code=500,
            detail="开票任务幂等响应事实格式错误，请停止重试并联系管理员",
        )
    return response


def _reserve_invoice_task_result(
    db: Session,
    *,
    task: FinanceInvoiceTask,
    request_hash: str,
    actor_id: int,
) -> tuple[FinanceManualMutation | None, dict[str, Any] | None]:
    idempotency_key = f"system:invoice-task-result:{task.id}"
    existing = db.scalar(
        select(FinanceManualMutation).where(
            FinanceManualMutation.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        return None, _invoice_task_result_replay(
            existing,
            statement_id=task.statement_id,
            request_hash=request_hash,
            actor_id=actor_id,
        )
    mutation = FinanceManualMutation(
        idempotency_key=idempotency_key,
        mutation_type="register_invoice_task",
        statement_id=task.statement_id,
        request_hash=request_hash,
        actor_id=actor_id,
        response_json="{}",
    )
    db.add(mutation)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        existing = db.scalar(
            select(FinanceManualMutation).where(
                FinanceManualMutation.idempotency_key == idempotency_key
            )
        )
        if existing is None:
            raise HTTPException(
                status_code=409,
                detail="开票任务成功结果正在由另一请求登记，请刷新后重试",
            ) from error
        return None, _invoice_task_result_replay(
            existing,
            statement_id=task.statement_id,
            request_hash=request_hash,
            actor_id=actor_id,
        )
    return mutation, None


def _record_invoice_task_result_response(
    mutation: FinanceManualMutation,
    response: dict[str, Any],
) -> dict[str, Any]:
    encoded = jsonable_encoder(response)
    mutation.response_json = json.dumps(
        encoded,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encoded


@router.post("/statements/{statement_id}/confirm")
def confirm_statement_for_invoice(
    statement_id: int,
    payload: VersionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_confirm_statement),
) -> dict[str, Any]:
    statement = _statement_for_user(db, statement_id, user)
    customer = db.get(Customer, statement.customer_id)
    if statement.version != payload.expected_version:
        raise HTTPException(status_code=409, detail={"message": "对账单版本已变化，请刷新后重试", "current_version": statement.version})
    if statement.confirmation_status == "confirmed":
        return {
            "id": statement.id,
            "confirmation_status": statement.confirmation_status,
            "version": statement.version,
            "ledger_version": statement.ledger_version,
        }
    if statement.confirmation_status == "cancelled":
        raise HTTPException(status_code=409, detail="已取消的对账单不能确认")
    if not _task_rows(db, statement.id):
        raise HTTPException(status_code=409, detail="对账单没有有效明细，不能确认开票")
    statement.confirmation_status = "confirmed"
    statement.confirmed_by = user.id
    statement.confirmed_at = datetime.now()
    statement.version += 1
    _audit(db, user=user, action="CONFIRM_FINANCE_STATEMENT", resource="Statement", entity_id=statement.id, customer=customer, details={"statement_number": statement.statement_number, "version": statement.version}, description="核对并确认月结对账单")
    db.commit()
    return {
        "id": statement.id,
        "confirmation_status": statement.confirmation_status,
        "version": statement.version,
        "ledger_version": statement.ledger_version,
    }


@router.post("/statements/{statement_id}/invoice-tasks", status_code=201)
def create_invoice_task(
    statement_id: int,
    payload: TaskCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_generate),
) -> dict[str, Any]:
    statement = _statement_for_user(db, statement_id, user)
    customer = db.get(Customer, statement.customer_id)
    if statement.confirmation_status != "confirmed":
        raise HTTPException(status_code=409, detail="请先核对并确认对账单，再生成开票任务")
    if statement.version != payload.expected_version:
        raise HTTPException(status_code=409, detail={"message": "对账单版本已变化，请刷新后重试", "current_version": statement.version})
    request_idempotency_base = hashlib.sha256(
        f"{user.id}\0{payload.idempotency_key.strip()}".encode("utf-8")
    ).hexdigest().upper()
    request_payload_hash = hashlib.sha256(
        json.dumps(
            {
                "statement_id": statement.id,
                "expected_version": payload.expected_version,
                "seller_entity_id": payload.seller_entity_id,
                "seller_change_type": payload.seller_change_type,
                "seller_change_reason": _text(payload.seller_change_reason),
                "confirm_permanent_change": payload.confirm_permanent_change,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest().upper()
    request_idempotency_prefix = f"invoice-request:{request_idempotency_base}:"
    request_idempotency_key = request_idempotency_prefix + request_payload_hash
    request_replay = db.scalar(
        select(FinanceInvoiceTask).where(
            FinanceInvoiceTask.idempotency_key == request_idempotency_key
        )
    )
    if request_replay is not None:
        return _task_response(db, request_replay)
    conflicting_replay = db.scalar(
        select(FinanceInvoiceTask.id).where(
            FinanceInvoiceTask.idempotency_key.like(
                request_idempotency_prefix + "%"
            )
        )
    )
    if conflicting_replay is not None:
        raise HTTPException(status_code=409, detail="幂等键已用于不同的开票任务请求")
    profile = db.scalar(select(CustomerInvoiceProfile).where(CustomerInvoiceProfile.customer_id == statement.customer_id))
    settlement_entity = (
        db.get(FinanceSettlementEntity, statement.settlement_entity_id)
        if statement.settlement_entity_id
        else None
    )
    seller_id = payload.seller_entity_id or (
        settlement_entity.default_seller_id
        if settlement_entity is not None
        else (profile.default_seller_id if profile else None)
    )
    effective_default_seller_id = (
        settlement_entity.default_seller_id
        if settlement_entity is not None
        else (profile.default_seller_id if profile else None)
    )
    seller = db.get(InvoiceSellerEntity, seller_id) if seller_id else None
    if seller is None:
        raise HTTPException(status_code=409, detail={"message": "客户缺少默认销方主体", "missing_items": ["默认销方主体"]})
    active = db.scalar(select(FinanceInvoiceTask).where(FinanceInvoiceTask.statement_id == statement.id, FinanceInvoiceTask.statement_version == statement.version, FinanceInvoiceTask.seller_entity_id == seller.id, FinanceInvoiceTask.status != "voided").order_by(FinanceInvoiceTask.id.desc()).limit(1))
    snapshot, lines, missing = _snapshot_for_statement(db, statement, seller)
    source_hash = _snapshot_hash(snapshot) if snapshot else ""
    if active is not None:
        if active.source_snapshot_hash == source_hash:
            return _task_response(db, active)
        raise HTTPException(status_code=409, detail="该对账版本已有冻结开票任务；请先作废旧任务后重新生成")
    if missing:
        raise HTTPException(status_code=409, detail={"message": "开票资料不完整，不能生成可导出任务", "missing_items": missing})
    if payload.seller_entity_id and seller.id != effective_default_seller_id:
        if payload.seller_change_type is None or not _text(payload.seller_change_reason):
            raise HTTPException(status_code=409, detail="临时或永久换销方必须选择类型并填写原因")
        if payload.seller_change_type == "permanent" and not payload.confirm_permanent_change:
            raise HTTPException(status_code=409, detail="永久修改默认销方需要再次确认")
    buyer_snapshot = {
        "invoice_title": settlement_entity.entity_name if settlement_entity else profile.invoice_title,
        "tax_no": settlement_entity.tax_no if settlement_entity else profile.tax_no,
        "invoice_address": settlement_entity.invoice_address if settlement_entity else profile.invoice_address,
        "invoice_phone": settlement_entity.invoice_phone if settlement_entity else profile.invoice_phone,
        "bank_name": settlement_entity.bank_name if settlement_entity else profile.bank_name,
        "bank_account": settlement_entity.bank_account if settlement_entity else profile.bank_account,
        "profile_version": profile.version,
        "settlement_entity_id": settlement_entity.id if settlement_entity else None,
        "settlement_entity_version": settlement_entity.version if settlement_entity else None,
    }
    seller_snapshot = {
        "seller_code": seller.seller_code,
        "seller_name": seller.seller_name,
        "tax_no": seller.tax_no,
        "address": seller.address,
        "phone": seller.phone,
        "bank_name": seller.bank_name,
        "bank_account": seller.bank_account,
        "seller_version": seller.version,
    }
    total = sum((line["total_amount"] for line in lines), Decimal("0")).quantize(MONEY)
    net = sum((line["net_amount"] for line in lines), Decimal("0")).quantize(MONEY)
    tax = sum((line["tax_amount"] for line in lines), Decimal("0")).quantize(MONEY)
    task = FinanceInvoiceTask(
        task_number=f"IT-{statement.statement_number}-V{statement.version}",
        statement_id=statement.id,
        statement_version=statement.version,
        customer_id=statement.customer_id,
        seller_entity_id=seller.id,
        buyer_snapshot_json=json.dumps(buyer_snapshot, ensure_ascii=False, sort_keys=True),
        seller_snapshot_json=json.dumps(seller_snapshot, ensure_ascii=False, sort_keys=True),
        invoice_type=profile.invoice_type,
        price_tax_mode=snapshot["price_tax_mode"],
        net_amount=net,
        tax_amount=tax,
        total_amount=total,
        status="draft",
        rule_version=max(line["rule_version"] for line in lines),
        source_snapshot_hash=source_hash,
        idempotency_key=request_idempotency_key,
        created_by=user.id,
    )
    try:
        db.add(task)
        db.flush()
        for sequence, line in enumerate(lines, start=1):
            db.add(
                FinanceInvoiceTaskItem(
                    task_id=task.id,
                    sequence_no=sequence,
                    statement_item_id=line["statement_item_id"],
                    source_type=line["source_type"],
                    customer_charge_id=line["customer_charge_id"],
                    product_code_snapshot=line["product_code_snapshot"],
                    product_name_snapshot=line["product_name_snapshot"],
                    project_name=line["project_name"],
                    tax_classification_code=line["tax_classification_code"],
                    specification=line["specification"],
                    unit=line["unit"],
                    quantity=line["quantity"],
                    unit_price=line["unit_price"],
                    amount=line["amount"],
                    tax_rate=line["tax_rate"],
                    tax_amount=line["tax_amount"],
                    rule_version=line["rule_version"],
                )
            )
        if payload.seller_entity_id and seller.id != effective_default_seller_id:
            change_type = payload.seller_change_type or "temporary"
            db.add(CustomerInvoiceSellerChange(customer_id=customer.id, old_seller_id=effective_default_seller_id, new_seller_id=seller.id, change_type=change_type, statement_id=statement.id, invoice_task_id=task.id, reason=payload.seller_change_reason or "", created_by=user.id))
            if change_type == "permanent":
                if settlement_entity is not None:
                    settlement_entity.default_seller_id = seller.id
                    settlement_entity.version += 1
                else:
                    profile.default_seller_id = seller.id
                    profile.version += 1
        _audit(db, user=user, action="CREATE_INVOICE_TASK", resource="FinanceInvoiceTask", entity_id=task.id, customer=customer, details={"statement_id": statement.id, "statement_version": statement.version, "seller_id": seller.id, "task_number": task.task_number, "total_amount": total, "line_count": len(lines), "request_idempotency_key": payload.idempotency_key}, description="生成冻结开票任务")
        db.commit()
    except IntegrityError as error:
        db.rollback()
        existing = db.scalar(
            select(FinanceInvoiceTask).where(
                FinanceInvoiceTask.idempotency_key == request_idempotency_key
            )
        )
        if existing is not None:
            return _task_response(db, existing)
        conflicting = db.scalar(
            select(FinanceInvoiceTask.id).where(
                FinanceInvoiceTask.idempotency_key.like(
                    request_idempotency_prefix + "%"
                )
            )
        )
        if conflicting is not None:
            raise HTTPException(
                status_code=409,
                detail="幂等键已用于不同的开票任务请求",
            ) from error
        raise HTTPException(status_code=409, detail="开票任务重复或来源已变化") from error
    return _task_response(db, task)


@router.get("/invoice-tasks")
def list_invoice_tasks(
    customer_id: int | None = Query(default=None, ge=1),
    statement_month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict[str, list[dict[str, Any]]]:
    query = select(FinanceInvoiceTask).order_by(FinanceInvoiceTask.id.desc())
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
        query = query.where(FinanceInvoiceTask.customer_id == customer_id)
    elif not has_unrestricted_customer_access(user, db):
        scopes = customer_scope_ids(user, db)
        if not scopes:
            return {"items": []}
        query = query.where(FinanceInvoiceTask.customer_id.in_(scopes))
    if status:
        query = query.where(FinanceInvoiceTask.status == status)
    tasks = db.scalars(query).all()
    if statement_month:
        tasks = [task for task in tasks if (statement := db.get(Statement, task.statement_id)) and statement.statement_month == statement_month]
    return {"items": [_task_response(db, task) for task in tasks]}


@router.get("/invoice-tasks/{task_id}")
def get_invoice_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict[str, Any]:
    task = _task_for_user(db, task_id, user)
    payload = _task_response(db, task)
    payload["buyer_snapshot"] = json.loads(task.buyer_snapshot_json)
    payload["seller_snapshot"] = json.loads(task.seller_snapshot_json)
    payload["items"] = [
        {
            "id": item.id,
            "sequence": item.sequence_no,
            "source_type": item.source_type,
            "customer_charge_id": item.customer_charge_id,
            "product_code": item.product_code_snapshot,
            "product_name": item.product_name_snapshot,
            "product_code_snapshot": item.product_code_snapshot,
            "product_name_snapshot": item.product_name_snapshot,
            "project_name": item.project_name,
            "tax_category_code": item.tax_classification_code,
            "specification": item.specification,
            "unit": item.unit,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "amount": item.amount,
            "tax_rate": item.tax_rate,
            "tax_amount": item.tax_amount,
        }
        for item in db.scalars(select(FinanceInvoiceTaskItem).where(FinanceInvoiceTaskItem.task_id == task.id).order_by(FinanceInvoiceTaskItem.sequence_no)).all()
    ]
    payload["attachments"] = [
        {"id": item.id, "attachment_type": item.attachment_type, "original_name": item.original_name, "content_hash": item.content_hash, "file_size": item.file_size}
        for item in db.scalars(select(FinanceInvoiceAttachment).where(FinanceInvoiceAttachment.task_id == task.id).order_by(FinanceInvoiceAttachment.id)).all()
    ]
    return payload


def _ensure_task_source_current(db: Session, task: FinanceInvoiceTask) -> None:
    statement = db.get(Statement, task.statement_id)
    seller = db.get(InvoiceSellerEntity, task.seller_entity_id)
    if statement is None or seller is None or statement.confirmation_status != "confirmed" or statement.version != task.statement_version:
        raise HTTPException(status_code=409, detail="对账来源已变化或未确认；请作废旧任务后重新生成")


def _raise_invoice_task_ledger_conflict(
    db: Session,
    *,
    task: FinanceInvoiceTask,
    expected_ledger_version: int,
) -> None:
    db.expire_all()
    statement = db.get(Statement, task.statement_id)
    if (
        statement is None
        or statement.confirmation_status != "confirmed"
        or statement.version != task.statement_version
    ):
        raise HTTPException(
            status_code=409,
            detail="对账来源已变化或未确认；请作废旧任务后重新生成",
        )
    if statement.ledger_version != expected_ledger_version:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "对账单财务流水版本已变化，请刷新后重试",
                "current_ledger_version": statement.ledger_version,
            },
        )
    raise HTTPException(status_code=409, detail="累计开票金额不能超过对账应收")


def _legacy_issued_invoice_task_replay(
    db: Session,
    *,
    task: FinanceInvoiceTask,
    payload: TaskResultPayload,
    user: User,
    mutation: FinanceManualMutation,
) -> dict[str, Any]:
    invoice = db.scalar(
        select(Invoice).where(Invoice.invoice_task_id == task.id)
    )
    if (
        invoice is None
        or invoice.invoice_number != payload.invoice_number
        or invoice.invoice_date != payload.invoice_date
        or invoice.confirmed_by != user.id
        or payload.expected_ledger_version is not None
        or payload.expected_version != task.version - 1
    ):
        raise HTTPException(status_code=409, detail="该任务已登记开票成功")
    response = _record_invoice_task_result_response(
        mutation,
        {
            **_task_response(db, task),
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
        },
    )
    db.commit()
    return response


def _register_issued_invoice_task_result(
    db: Session,
    *,
    task: FinanceInvoiceTask,
    payload: TaskResultPayload,
    user: User,
) -> dict[str, Any]:
    if not payload.invoice_number or payload.invoice_date is None:
        raise HTTPException(status_code=422, detail="开票成功必须填写发票号码和开票日期")

    request_hash = _invoice_task_result_request_hash(task, payload)
    try:
        mutation, replay = _reserve_invoice_task_result(
            db,
            task=task,
            request_hash=request_hash,
            actor_id=int(user.id),
        )
        if replay is not None:
            return replay
        assert mutation is not None

        if task.status == "issued":
            return _legacy_issued_invoice_task_replay(
                db,
                task=task,
                payload=payload,
                user=user,
                mutation=mutation,
            )
        if payload.expected_ledger_version is None:
            raise HTTPException(
                status_code=422,
                detail="开票成功必须提供 expected_ledger_version",
            )
        if task.version != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "任务版本已变化，请刷新后重试",
                    "current_version": task.version,
                },
            )
        if task.status != "exported":
            raise HTTPException(
                status_code=409,
                detail="请先下载税局 Excel 并由财务人工核对后再登记成功",
            )
        _ensure_task_source_current(db, task)

        updated_statement = db.execute(
            text(
                """
                UPDATE finance_statements
                SET invoiced_amount = invoiced_amount + :amount,
                    ledger_version = ledger_version + 1
                WHERE id = :statement_id
                  AND confirmation_status = 'confirmed'
                  AND version = :statement_version
                  AND ledger_version = :expected_ledger_version
                  AND invoiced_amount + :amount <= total_receivable
                RETURNING id, total_receivable, invoiced_amount, settled_amount,
                          status, confirmation_status, version, ledger_version
                """
            ),
            {
                "statement_id": task.statement_id,
                "statement_version": task.statement_version,
                "expected_ledger_version": payload.expected_ledger_version,
                "amount": str(task.total_amount),
            },
        ).mappings().one_or_none()
        if updated_statement is None:
            _raise_invoice_task_ledger_conflict(
                db,
                task=task,
                expected_ledger_version=payload.expected_ledger_version,
            )

        updated_task = db.execute(
            text(
                """
                UPDATE finance_invoice_tasks
                SET status = 'issued',
                    failure_reason = NULL,
                    version = version + 1
                WHERE id = :task_id
                  AND status = 'exported'
                  AND version = :expected_version
                RETURNING id, version
                """
            ),
            {
                "task_id": task.id,
                "expected_version": payload.expected_version,
            },
        ).mappings().one_or_none()
        if updated_task is None:
            db.expire_all()
            current_task = db.get(FinanceInvoiceTask, task.id)
            if current_task is None:
                raise HTTPException(status_code=404, detail="开票任务不存在")
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "任务版本已变化，请刷新后重试",
                    "current_version": current_task.version,
                },
            )

        invoice = Invoice(
            statement_id=task.statement_id,
            invoice_number=payload.invoice_number,
            invoice_date=payload.invoice_date,
            invoice_amount=task.total_amount,
            invoice_task_id=task.id,
            seller_entity_id=task.seller_entity_id,
            net_amount=task.net_amount,
            tax_amount=task.tax_amount,
            total_amount=task.total_amount,
            invoice_status="issued",
            source="invoice_task",
            confirmed_by=user.id,
            confirmed_at=datetime.now(),
            created_by=user.id,
        )
        db.add(invoice)
        db.flush()
        _audit(
            db,
            user=user,
            action="REGISTER_INVOICE_TASK_SUCCESS",
            resource="FinanceInvoiceTask",
            entity_id=task.id,
            customer=db.get(Customer, task.customer_id),
            details={
                "task_number": task.task_number,
                "invoice_number": invoice.invoice_number,
                "invoice_amount": task.total_amount,
                "expected_ledger_version": payload.expected_ledger_version,
                "statement_version": updated_statement["version"],
                "ledger_version": updated_statement["ledger_version"],
            },
            description="登记税局开票成功",
        )
        db.expire_all()
        current_task = db.get(FinanceInvoiceTask, task.id)
        assert current_task is not None
        response = _record_invoice_task_result_response(
            mutation,
            {
                **_task_response(db, current_task),
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
            },
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="发票号码已存在或开票任务已登记",
        ) from error
    except Exception:
        db.rollback()
        raise


@router.post("/invoice-tasks/{task_id}/confirm")
def confirm_invoice_task(
    task_id: int,
    payload: VersionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_generate),
) -> dict[str, Any]:
    task = _task_for_user(db, task_id, user)
    customer = db.get(Customer, task.customer_id)
    if task.version != payload.expected_version:
        raise HTTPException(status_code=409, detail={"message": "任务版本已变化，请刷新后重试", "current_version": task.version})
    if task.status in {"ready", "exported", "issued"}:
        return _task_response(db, task)
    if task.status == "voided":
        raise HTTPException(status_code=409, detail="已作废任务不能确认")
    _ensure_task_source_current(db, task)
    task.status = "ready"
    task.failure_reason = None
    task.confirmed_by = user.id
    task.confirmed_at = datetime.now()
    task.version += 1
    _audit(db, user=user, action="CONFIRM_INVOICE_TASK", resource="FinanceInvoiceTask", entity_id=task.id, customer=customer, details={"task_number": task.task_number, "version": task.version}, description="确认冻结开票任务")
    db.commit()
    return _task_response(db, task)


def _invoice_export_dir() -> Path:
    configured = os.getenv("ERP_INVOICE_EXPORT_DIR")
    if configured:
        return Path(configured)
    return load_settings().database_path.parent / "invoice_exports"


@router.get("/invoice-tasks/{task_id}/tax-template.xlsx")
def download_tax_template(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_generate),
) -> FileResponse:
    task = _task_for_user(db, task_id, user)
    customer = db.get(Customer, task.customer_id)
    if task.status not in {"ready", "exported"}:
        raise HTTPException(status_code=409, detail="请先确认开票任务，再下载税局 Excel")
    _ensure_task_source_current(db, task)
    items = db.scalars(select(FinanceInvoiceTaskItem).where(FinanceInvoiceTaskItem.task_id == task.id).order_by(FinanceInvoiceTaskItem.sequence_no)).all()
    export_dir = _invoice_export_dir()
    statement = db.get(Statement, task.statement_id)
    settlement_entity = (
        db.get(FinanceSettlementEntity, statement.settlement_entity_id)
        if statement is not None and statement.settlement_entity_id
        else None
    )
    short_name = (
        (settlement_entity.short_name or settlement_entity.entity_name).strip()
        if settlement_entity is not None
        else (
            (customer.chinese_short_name or "").strip()
            if customer is not None
            else ""
        )
    ) or (customer.name[:12] if customer is not None else "客户")
    statement_month = statement.statement_month if statement is not None else ""
    download_name = _safe_export_filename(
        f"{short_name}{statement_month}导入模板.xlsx"
    )
    output = export_dir / (
        f"{task.task_number}-{task.source_snapshot_hash[:12]}-{download_name}"
    )
    try:
        export = generate_invoice_tax_template(template_path=TAX_TEMPLATE_PATH, output_path=output, lines=[{"project_name": item.project_name, "tax_classification_code": item.tax_classification_code, "specification": item.specification, "unit": item.unit, "quantity": item.quantity, "unit_price": item.unit_price, "amount": item.amount, "tax_rate": item.tax_rate} for item in items])
    except InvoiceTaxTemplateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    task.status = "exported"
    task.export_count += 1
    task.last_export_hash = export.sha256
    task.last_exported_at = datetime.now()
    _audit(db, user=user, action="EXPORT_INVOICE_TAX_TEMPLATE", resource="FinanceInvoiceTask", entity_id=task.id, customer=customer, details={"task_number": task.task_number, "file_hash": export.sha256, "line_count": export.line_count, "warnings": list(export.warnings)}, description="生成税局开票导入文件")
    db.commit()
    return FileResponse(export.path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=download_name)


@router.post("/invoice-tasks/{task_id}/result")
def register_invoice_task_result(
    task_id: int,
    payload: TaskResultPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_register),
) -> dict[str, Any]:
    task = _task_for_user(db, task_id, user)
    if payload.status == "issued":
        return _register_issued_invoice_task_result(
            db,
            task=task,
            payload=payload,
            user=user,
        )

    customer = db.get(Customer, task.customer_id)
    try:
        if task.version != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "任务版本已变化，请刷新后重试",
                    "current_version": task.version,
                },
            )
        if not payload.failure_reason:
            raise HTTPException(status_code=422, detail="请填写税局导入或开具失败原因")
        if task.status == "issued":
            raise HTTPException(status_code=409, detail="已开票任务不能登记为失败")
        updated = db.execute(
            text(
                """
                UPDATE finance_invoice_tasks
                SET status = 'failed',
                    failure_reason = :failure_reason,
                    version = version + 1
                WHERE id = :task_id
                  AND status != 'issued'
                  AND version = :expected_version
                RETURNING id, version
                """
            ),
            {
                "task_id": task.id,
                "failure_reason": payload.failure_reason,
                "expected_version": payload.expected_version,
            },
        ).mappings().one_or_none()
        if updated is None:
            db.expire_all()
            current_task = db.get(FinanceInvoiceTask, task.id)
            if current_task is None:
                raise HTTPException(status_code=404, detail="开票任务不存在")
            if current_task.status == "issued":
                raise HTTPException(status_code=409, detail="已开票任务不能登记为失败")
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "任务版本已变化，请刷新后重试",
                    "current_version": current_task.version,
                },
            )
        _audit(
            db,
            user=user,
            action="REGISTER_INVOICE_TASK_FAILURE",
            resource="FinanceInvoiceTask",
            entity_id=task.id,
            customer=customer,
            details={
                "task_number": task.task_number,
                "failure_reason": payload.failure_reason,
            },
            description="登记税局开票失败",
        )
        db.expire_all()
        current_task = db.get(FinanceInvoiceTask, task.id)
        assert current_task is not None
        response = _task_response(db, current_task)
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/invoice-tasks/{task_id}/void")
def void_invoice_task(
    task_id: int,
    payload: VersionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_generate),
) -> dict[str, Any]:
    task = _task_for_user(db, task_id, user)
    customer = db.get(Customer, task.customer_id)
    try:
        if task.version != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "任务版本已变化，请刷新后重试",
                    "current_version": task.version,
                },
            )
        if task.status == "issued":
            raise HTTPException(status_code=409, detail="已登记发票不能在本阶段自动作废")
        if task.status == "voided":
            return _task_response(db, task)

        statement = db.get(Statement, task.statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="关联对账单不存在")
        if statement.version != task.statement_version:
            raise HTTPException(
                status_code=409,
                detail="对账单已经调整，请刷新后按当前账单处理开票任务",
            )
        if statement.settled_amount > 0 or db.scalar(
            select(SettlementRecord.id)
            .where(SettlementRecord.statement_id == statement.id)
            .limit(1)
        ) is not None:
            raise HTTPException(
                status_code=409,
                detail="该对账单已有收款事实，不能直接撤销开票任务后修改账单",
            )
        if db.scalar(
            select(Invoice.id)
            .where(
                Invoice.statement_id == statement.id,
                Invoice.invoice_status == "issued",
            )
            .limit(1)
        ) is not None:
            raise HTTPException(
                status_code=409,
                detail="该对账单已登记实际开票，不能普通撤销；请按红冲/重开流程处理。",
            )

        updated = db.execute(
            text(
                """
                UPDATE finance_invoice_tasks
                SET status = 'voided',
                    voided_by = :voided_by,
                    voided_at = :voided_at,
                    version = version + 1
                WHERE id = :task_id
                  AND status NOT IN ('issued', 'voided')
                  AND version = :expected_version
                RETURNING id, version
                """
            ),
            {
                "task_id": task.id,
                "voided_by": user.id,
                "voided_at": datetime.now(),
                "expected_version": payload.expected_version,
            },
        ).mappings().one_or_none()
        if updated is None:
            db.expire_all()
            current_task = db.get(FinanceInvoiceTask, task.id)
            if current_task is None:
                raise HTTPException(status_code=404, detail="开票任务不存在")
            if current_task.status == "issued":
                raise HTTPException(
                    status_code=409,
                    detail="已登记发票不能在本阶段自动作废",
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "任务版本已变化，请刷新后重试",
                    "current_version": current_task.version,
                },
            )
        _audit(
            db,
            user=user,
            action="VOID_INVOICE_TASK",
            resource="FinanceInvoiceTask",
            entity_id=task.id,
            customer=customer,
            details={
                "task_number": task.task_number,
                "statement_id": statement.id,
                "old_tax_template_must_not_be_used": True,
            },
            description="作废冻结开票任务",
        )
        db.expire_all()
        active_task_count = db.scalar(
            select(FinanceInvoiceTask.id)
            .where(
                FinanceInvoiceTask.statement_id == statement.id,
                FinanceInvoiceTask.status.not_in(("voided", "issued")),
            )
            .limit(1)
        )
        reopened_statement = False
        if active_task_count is None:
            statement = db.get(Statement, statement.id)
            assert statement is not None
            if statement.version != task.statement_version:
                raise HTTPException(
                    status_code=409,
                    detail="对账单版本已变化，请刷新后重试",
                )
            before_version = statement.version
            statement.confirmation_status = "draft"
            statement.confirmed_by = None
            statement.confirmed_at = None
            statement.version += 1
            db.add(
                StatementAdjustment(
                    statement_id=statement.id,
                    action="void_invoice_task_for_modify",
                    reason="撤销未实际开票任务，旧税局导入文件不可继续使用",
                    before_version=before_version,
                    after_version=statement.version,
                    details_json=json.dumps(
                        {
                            "voided_invoice_task_id": task.id,
                            "voided_invoice_task_number": task.task_number,
                            "old_tax_template_must_not_be_used": True,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    created_by=user.id,
                )
            )
            _audit(
                db,
                user=user,
                action="RETURN_TASK_TO_MODIFY",
                resource="Statement",
                entity_id=statement.id,
                customer=customer,
                details={
                    "task_number": task.task_number,
                    "before_version": before_version,
                    "after_version": statement.version,
                },
                description="撤销未开票任务并退回对账修改",
            )
            reopened_statement = True
        db.expire_all()
        current_task = db.get(FinanceInvoiceTask, task.id)
        assert current_task is not None
        response = _task_response(db, current_task)
        response["reopened_statement"] = reopened_statement
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def _attachment_root() -> Path:
    configured = os.getenv("ERP_INVOICE_ATTACHMENT_DIR")
    if configured:
        return Path(configured)
    return load_settings().database_path.parent / "invoice_attachments"


@router.post("/invoice-tasks/{task_id}/attachments/original", status_code=201)
async def upload_original_invoice_pdf(
    task_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(can_attachment_manage),
) -> dict[str, Any]:
    task = _task_for_user(db, task_id, user)
    customer = db.get(Customer, task.customer_id)
    if task.status != "issued":
        raise HTTPException(status_code=409, detail="请先人工登记开票成功，再归档税局原始 PDF")
    content = await file.read()
    try:
        stored = store_original_invoice_pdf(storage_root=_attachment_root(), original_filename=file.filename or "", content=content)
    except InvoiceAttachmentError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    existing = db.scalar(select(FinanceInvoiceAttachment).where(FinanceInvoiceAttachment.task_id == task.id, FinanceInvoiceAttachment.attachment_type == "original", FinanceInvoiceAttachment.content_hash == stored.sha256))
    if existing is None:
        relative = stored.path.relative_to(_attachment_root()).as_posix()
        existing = FinanceInvoiceAttachment(task_id=task.id, attachment_type="original", original_name=stored.original_filename, stored_name=relative, content_hash=stored.sha256, file_size=stored.size, uploaded_by=user.id)
        db.add(existing)
        db.flush()
        invoice = db.scalar(select(Invoice).where(Invoice.invoice_task_id == task.id))
        if invoice is not None:
            organized_name = f"天明-{customer.name}-{invoice.invoice_number}-{invoice.invoice_date.isoformat()}.pdf"
            organized = create_organized_invoice_pdf(storage_root=_attachment_root(), original=stored, organized_filename=organized_name)
            organized_relative = organized.path.relative_to(_attachment_root()).as_posix()
            duplicate = db.scalar(select(FinanceInvoiceAttachment).where(FinanceInvoiceAttachment.task_id == task.id, FinanceInvoiceAttachment.attachment_type == "organized", FinanceInvoiceAttachment.content_hash == stored.sha256))
            if duplicate is None:
                db.add(FinanceInvoiceAttachment(task_id=task.id, attachment_type="organized", original_name=organized_name, stored_name=organized_relative, content_hash=stored.sha256, file_size=organized.size, source_attachment_id=existing.id, uploaded_by=user.id))
        _audit(db, user=user, action="UPLOAD_INVOICE_ORIGINAL_PDF", resource="FinanceInvoiceAttachment", entity_id=existing.id, customer=customer, details={"task_number": task.task_number, "content_hash": stored.sha256, "size": stored.size}, description="归档税局原始发票 PDF")
        db.commit()
    return {"id": existing.id, "content_hash": existing.content_hash, "file_size": existing.file_size, "reused": stored.reused}


@router.get("/invoice-tasks/{task_id}/attachments/{attachment_id}")
def download_invoice_pdf(
    task_id: int,
    attachment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_attachment_view),
) -> FileResponse:
    task = _task_for_user(db, task_id, user)
    attachment = db.get(FinanceInvoiceAttachment, attachment_id)
    if attachment is None or attachment.task_id != task.id:
        raise HTTPException(status_code=404, detail="发票附件不存在")
    root = _attachment_root().resolve()
    path = (root / attachment.stored_name).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise HTTPException(status_code=409, detail="发票附件路径异常") from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="发票附件文件不存在")
    return FileResponse(path, media_type="application/pdf", filename=attachment.original_name)
