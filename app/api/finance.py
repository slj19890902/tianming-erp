from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.models.audit import OperationLog
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import (
    Invoice,
    ReturnReceipt,
    ReturnReceiptItem,
    SettlementRecord,
    Statement,
    StatementItem,
)
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.history_orders import build_display_registry, display_order_number


router = APIRouter()
can_read = PermissionChecker("finance.view")
can_operate = PermissionChecker("finance.execute")
MONEY = Decimal("0.00")
STATEMENT_COST_FIELDS = frozenset(
    {"total_gross_profit", "unit_cost_snapshot", "gross_profit_amount"}
)

_CITY_PREFIXES = ("苏州", "昆山", "常熟", "太仓", "上海", "无锡", "南京", "杭州", "深圳", "广州")
_COMPANY_SUFFIXES = ("股份有限公司", "有限责任公司", "科技有限公司", "有限公司")
_ILLEGAL_CHARS = r'/\:*?"<>|'


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _redact_statement_costs(payload: dict, user: User) -> dict:
    if has_permission(user, "cost.view"):
        return payload
    redacted = {
        key: value
        for key, value in payload.items()
        if key not in STATEMENT_COST_FIELDS
    }
    if "items" in redacted:
        redacted["items"] = [
            {
                key: value
                for key, value in item.items()
                if key not in STATEMENT_COST_FIELDS
            }
            for item in redacted["items"]
        ]
    return redacted


def _statement_for_user(
    db: Session,
    statement_id: int,
    user: User,
) -> Statement:
    statement = db.get(Statement, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="对账单不存在")
    require_customer_access(statement.customer_id, user, db)
    return statement


def _delivery_for_user(
    db: Session,
    delivery_id: int,
    user: User,
) -> Delivery:
    delivery = db.get(Delivery, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="送货单不存在")
    require_customer_access(delivery.customer_id, user, db)
    return delivery


def _return_receipt_for_user(
    db: Session,
    receipt_id: int,
    user: User,
) -> ReturnReceipt:
    receipt = db.get(ReturnReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="回单不存在")
    _delivery_for_user(db, receipt.delivery_id, user)
    return receipt


def _customer_abbr(name: str) -> str:
    for prefix in _CITY_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for suffix in _COMPANY_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    chinese = [c for c in name if "一" <= c <= "鿿"]
    abbr = "".join(chinese[:2])
    return abbr if abbr else "客户"


def _safe_filename(name: str) -> str:
    for ch in _ILLEGAL_CHARS:
        name = name.replace(ch, "")
    return name.strip()


class ReturnReceiptLineCreate(BaseModel):
    delivery_item_id: int
    actual_received_quantity: int
    resolution_action: str | None = None
    difference_reason: str | None = None


class ReturnReceiptCreate(BaseModel):
    delivery_id: int
    actual_received_date: date
    signed_by: str | None = None
    items: list[ReturnReceiptLineCreate]

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[ReturnReceiptLineCreate]):
        if not value:
            raise ValueError("回单至少需要一条明细")
        ids = [item.delivery_item_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("回单明细不能重复")
        return value


class ReturnReceiptUpdate(BaseModel):
    actual_received_date: date
    signed_by: str | None = None
    items: list[ReturnReceiptLineCreate]

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[ReturnReceiptLineCreate]):
        if not value:
            raise ValueError("回单至少需要一条明细")
        ids = [item.delivery_item_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("回单明细不能重复")
        return value


class StatementCreate(BaseModel):
    customer_id: int
    statement_month: str
    delivery_ids: list[int] = Field(default_factory=list)
    # 兼容旧客户端；后端仍会校验这些明细是否覆盖完整送货单。
    return_receipt_item_ids: list[int] = Field(default_factory=list)

    @field_validator("statement_month")
    @classmethod
    def validate_month(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
            raise ValueError("对账月份格式必须为 YYYY-MM")
        return value

    @field_validator("delivery_ids", "return_receipt_item_ids")
    @classmethod
    def validate_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("待对账送货单或明细不能重复")
        if any(item_id <= 0 for item_id in value):
            raise ValueError("待对账送货单或明细ID必须为正整数")
        return value

    @model_validator(mode="after")
    def validate_selection(self):
        if not self.delivery_ids and not self.return_receipt_item_ids:
            raise ValueError("至少选择一张待对账送货单")
        if self.delivery_ids and self.return_receipt_item_ids:
            raise ValueError("送货单与旧版明细选择不能同时提交")
        return self


class StatementUpdate(BaseModel):
    statement_month: str

    @field_validator("statement_month")
    @classmethod
    def validate_month(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
            raise ValueError("对账月份格式必须为 YYYY-MM")
        return value


def _statement_period(
    statement_month: str,
    cycle_start_day: int,
) -> tuple[date, date]:
    """Return the inclusive delivery-date range assigned to a statement month."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", statement_month):
        raise ValueError("对账月份格式必须为 YYYY-MM")
    if not 1 <= cycle_start_day <= 28:
        raise ValueError("客户对账结转日必须在 1 至 28 日之间")
    year, month = (int(part) for part in statement_month.split("-"))
    month_start = date(year, month, 1)
    if cycle_start_day == 1:
        next_month = (
            date(year + 1, 1, 1)
            if month == 12
            else date(year, month + 1, 1)
        )
        return month_start, next_month - timedelta(days=1)
    previous_month_end = month_start - timedelta(days=1)
    return (
        date(previous_month_end.year, previous_month_end.month, cycle_start_day),
        date(year, month, cycle_start_day) - timedelta(days=1),
    )


class InvoiceCreate(BaseModel):
    statement_id: int
    invoice_number: str
    invoice_date: date
    invoice_amount: Decimal

    @field_validator("invoice_number")
    @classmethod
    def validate_invoice_number(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("发票号码不能为空")
        return normalized

    @field_validator("invoice_amount")
    @classmethod
    def validate_invoice_amount(cls, value: Decimal) -> Decimal:
        amount = value.quantize(MONEY, rounding=ROUND_HALF_UP)
        if amount <= 0:
            raise ValueError("开票金额必须大于 0")
        return amount


class SettlementCreate(BaseModel):
    amount: Decimal
    settlement_date: date
    account: str | None = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        amount = value.quantize(MONEY, rounding=ROUND_HALF_UP)
        if amount <= 0:
            raise ValueError("收款金额必须大于 0")
        return amount

    @field_validator("account")
    @classmethod
    def validate_account(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


@router.get("/statements")
def list_statements(
    customer_id: int | None = None,
    statement_month: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = (
        select(Statement, Customer.name.label("customer_name"))
        .join(Customer, Customer.id == Statement.customer_id)
        .order_by(Statement.statement_month.desc(), Statement.id.desc())
    )
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
        query = query.where(Statement.customer_id == customer_id)
    else:
        visible_customer_ids = _visible_customer_ids(user, db)
        if visible_customer_ids is not None:
            query = query.where(Statement.customer_id.in_(visible_customer_ids))
    if statement_month:
        query = query.where(Statement.statement_month == statement_month)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            _redact_statement_costs(
                {
                    "id": statement.id,
                    "statement_number": statement.statement_number,
                    "customer_id": statement.customer_id,
                    "customer_name": customer_name,
                    "statement_month": statement.statement_month,
                    "total_receivable": statement.total_receivable,
                    "total_gross_profit": statement.total_gross_profit,
                    "invoiced_amount": statement.invoiced_amount,
                    "settled_amount": statement.settled_amount,
                    "status": statement.status,
                    "created_at": statement.created_at,
                },
                user,
            )
            for statement, customer_name in rows
        ],
    }


@router.get("/statement-customers")
def list_statement_customers(
    statement_month: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    visible_customer_ids = _visible_customer_ids(user, db)
    candidate_query = (
        select(Delivery.customer_id)
        .join(ReturnReceipt, ReturnReceipt.delivery_id == Delivery.id)
        .where(ReturnReceipt.status == "confirmed")
        .distinct()
    )
    if visible_customer_ids is not None:
        candidate_query = candidate_query.where(
            Delivery.customer_id.in_(visible_customer_ids)
        )
    candidate_ids = set(db.scalars(candidate_query).all())
    if not candidate_ids:
        return {"items": []}
    query = (
        select(Customer)
        .where(Customer.id.in_(candidate_ids))
        .order_by(Customer.name)
    )
    items = []
    for customer in db.scalars(query).all():
        period_start = period_end = None
        if statement_month:
            try:
                period_start, period_end = _statement_period(
                    statement_month,
                    customer.statement_cycle_start_day,
                )
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        grouped = _pending_statement_groups(
            db,
            customer.id,
            period_start=period_start,
            period_end=period_end,
        )
        selectable_count = sum(
            1 for delivery in grouped if not delivery["selection_blocked"]
        )
        blocked_count = sum(
            1 for delivery in grouped if delivery["selection_blocked"]
        )
        if not selectable_count and not blocked_count:
            continue
        items.append(
            {
                "id": customer.id,
                "name": customer.name,
                "pending_count": selectable_count,
                "blocked_count": blocked_count,
                "statement_cycle_start_day": customer.statement_cycle_start_day,
                "period_start": period_start,
                "period_end": period_end,
            }
        )
    return {"items": items}


def _statement_detail_response(
    db: Session,
    statement_id: int,
    user: User,
) -> dict:
    row = db.execute(
        select(Statement, Customer.name.label("customer_name"))
        .join(Customer, Customer.id == Statement.customer_id)
        .where(Statement.id == statement_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="对账单不存在")
    statement, customer_name = row
    require_customer_access(statement.customer_id, user, db)
    items = db.execute(
        select(
            StatementItem.id.label("statement_item_id"),
            StatementItem.return_receipt_item_id,
            ReturnReceipt.actual_received_date,
            Delivery.delivery_number,
            Order.customer_po,
            Product.product_code,
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
            OrderItem.snapshot_material.label("material"),
            StatementItem.actual_received_quantity,
            StatementItem.unit_price_snapshot,
            StatementItem.unit_cost_snapshot,
            StatementItem.receivable_amount,
            StatementItem.gross_profit_amount,
            ReturnReceiptItem.difference_reason,
        )
        .select_from(StatementItem)
        .join(
            ReturnReceiptItem,
            ReturnReceiptItem.id == StatementItem.return_receipt_item_id,
        )
        .join(
            ReturnReceipt,
            ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
        )
        .join(DeliveryItem, DeliveryItem.id == ReturnReceiptItem.delivery_item_id)
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(StatementItem.statement_id == statement.id)
        .order_by(StatementItem.id)
    ).all()
    invoices = db.execute(
        select(
            Invoice.id,
            Invoice.invoice_number,
            Invoice.invoice_date,
            Invoice.invoice_amount,
        ).where(Invoice.statement_id == statement.id)
    ).all()
    settlements = db.execute(
        select(
            SettlementRecord.id,
            SettlementRecord.settled_amount,
            SettlementRecord.settlement_date,
            SettlementRecord.account,
        ).where(SettlementRecord.statement_id == statement.id)
    ).all()
    return _redact_statement_costs(
        {
            "id": statement.id,
            "statement_number": statement.statement_number,
            "customer_id": statement.customer_id,
            "customer_name": customer_name,
            "statement_month": statement.statement_month,
            "total_receivable": statement.total_receivable,
            "total_gross_profit": statement.total_gross_profit,
            "invoiced_amount": statement.invoiced_amount,
            "settled_amount": statement.settled_amount,
            "status": statement.status,
            "status_label": "已结清" if statement.status == "settled" else "未结清",
            "item_count": len(items),
            "invoice_count": len(invoices),
            "settlement_count": len(settlements),
            "items": [
                {
                    **dict(item._mapping),
                }
                for item in items
            ],
            "invoices": [dict(row._mapping) for row in invoices],
            "settlements": [dict(row._mapping) for row in settlements],
        },
        user,
    )


@router.get("/statements/{statement_id}/export")
def export_statement_excel(
    statement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> StreamingResponse:
    row = db.execute(
        select(Statement, Customer)
        .join(Customer, Customer.id == Statement.customer_id)
        .where(Statement.id == statement_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="对账单不存在")
    statement, customer = row
    require_customer_access(statement.customer_id, user, db)
    lines = db.execute(
        select(
            Delivery.delivery_date,
            Delivery.delivery_number,
            Order.customer_po,
            OrderItem.snapshot_product_code,
            OrderItem.snapshot_product_name,
            OrderItem.snapshot_spec,
            OrderItem.snapshot_material,
            StatementItem.actual_received_quantity,
            StatementItem.unit_price_snapshot,
            StatementItem.receivable_amount,
            ReturnReceiptItem.difference_reason,
        )
        .join(
            ReturnReceiptItem,
            ReturnReceiptItem.id == StatementItem.return_receipt_item_id,
        )
        .join(
            DeliveryItem,
            DeliveryItem.id == ReturnReceiptItem.delivery_item_id,
        )
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(StatementItem.statement_id == statement.id)
        .order_by(Delivery.delivery_date, Delivery.delivery_number)
    ).all()

    inv = statement.invoiced_amount
    rec = statement.total_receivable
    if inv <= 0:
        invoice_status = "未开票"
    elif inv < rec:
        invoice_status = "部分开票"
    else:
        invoice_status = "已开票"

    sett = statement.settled_amount
    if sett <= 0:
        settlement_status = "未收款"
    elif sett < rec:
        settlement_status = "部分收款"
    else:
        settlement_status = "已结清"

    company = db.scalar(select(CompanyConfig).where(CompanyConfig.id == 1))
    company_name = company.company_name if company and company.company_name else ""

    col_count = 15
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "月结对账单"
    last_col = chr(64 + col_count)
    sheet.merge_cells(f"A1:{last_col}1")
    sheet["A1"] = "月结对账单"
    sheet["A1"].font = Font(size=18, bold=True)
    sheet["A1"].alignment = Alignment(horizontal="center")
    sheet.merge_cells(f"A2:{last_col}2")
    sheet["A2"] = (
        f"客户：{customer.name}    月份：{statement.statement_month}    "
        f"对账单号：{statement.statement_number}"
    )
    sheet.merge_cells(f"A3:{last_col}3")
    sender_parts = [f"供方：{company_name}"] if company_name else []
    if company and company.address:
        sender_parts.append(f"地址：{company.address}")
    if company and company.phone:
        sender_parts.append(f"电话：{company.phone}")
    if company and company.tax_number:
        sender_parts.append(f"税号：{company.tax_number}")
    if company and company.bank_name and company.bank_account:
        sender_parts.append(f"开户行：{company.bank_name}  账号：{company.bank_account}")
    sheet["A3"] = "    ".join(sender_parts)
    sheet["A3"].alignment = Alignment(horizontal="left")
    headers = [
        "客户名称",    # 1
        "客户单号",    # 2
        "存货编码",    # 3
        "送货日期",    # 4
        "送货单号",    # 5
        "产品名称",    # 6
        "规格型号",    # 7
        "材质",        # 8
        "实际签收数量", # 9
        "单价",        # 10
        "金额",        # 11
        "备注",        # 12
        "开票状态",    # 13
        "对账状态",    # 14
        "结清状态",    # 15
    ]
    sheet.append([])
    sheet.append(headers)
    for cell in sheet[5]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DCE6F1")
        cell.alignment = Alignment(horizontal="center")
    for line in lines:
        sheet.append(
            [
                customer.name,
                line.customer_po,
                line.snapshot_product_code,
                line.delivery_date,
                line.delivery_number,
                line.snapshot_product_name,
                line.snapshot_spec,
                line.snapshot_material,
                line.actual_received_quantity,
                float(line.unit_price_snapshot),
                float(line.receivable_amount),
                line.difference_reason,
                invoice_status,
                "已对账",
                settlement_status,
            ]
        )
    total_row = sheet.max_row + 2
    sheet.cell(total_row, 10, "合计")
    sheet.cell(total_row, 11, float(statement.total_receivable))
    sheet.cell(total_row, 10).font = Font(bold=True)
    sheet.cell(total_row, 11).font = Font(bold=True)
    widths = [22, 18, 16, 13, 20, 28, 20, 14, 12, 12, 14, 24, 10, 10, 10]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(64 + index)].width = width
    sheet.freeze_panes = "A6"

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    abbr = _customer_abbr(customer.name)
    raw_name = f"{abbr}{statement.statement_month}对账单.xlsx"
    filename = _safe_filename(raw_name)
    encoded = quote(filename, safe="")
    return StreamingResponse(
        output,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="statement.xlsx"; filename*=UTF-8\'\'{encoded}'
            ),
        },
    )


@router.get("/invoices")
def list_invoices(
    statement_id: int | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = (
        select(
            Invoice,
            Statement.statement_number,
            Customer.name.label("customer_name"),
        )
        .join(Statement, Statement.id == Invoice.statement_id)
        .join(Customer, Customer.id == Statement.customer_id)
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    )
    if statement_id is not None:
        statement = db.get(Statement, statement_id)
        if statement is not None:
            require_customer_access(statement.customer_id, user, db)
        query = query.where(Invoice.statement_id == statement_id)
    else:
        visible_customer_ids = _visible_customer_ids(user, db)
        if visible_customer_ids is not None:
            query = query.where(Statement.customer_id.in_(visible_customer_ids))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": invoice.id,
                "statement_id": invoice.statement_id,
                "statement_number": statement_number,
                "customer_name": customer_name,
                "invoice_number": invoice.invoice_number,
                "invoice_date": invoice.invoice_date,
                "invoice_amount": invoice.invoice_amount,
                "created_at": invoice.created_at,
            }
            for invoice, statement_number, customer_name in rows
        ],
    }

def _audit(
    db: Session,
    *,
    user: User,
    action: str,
    resource: str,
    entity_id: int,
    details: dict,
    description: str,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource=resource,
            details=json.dumps(details, ensure_ascii=False, default=str),
            username=user.username,
            role=user.role,
            entity_type=resource.lower(),
            entity_id=entity_id,
            description=description,
        )
    )


def _next_statement_number(db: Session, month: str) -> str:
    sequence = db.execute(
        text(
            """
            INSERT INTO statement_monthly_sequences (statement_month, last_value)
            VALUES (:month, 1)
            ON CONFLICT(statement_month)
            DO UPDATE SET last_value = last_value + 1
            RETURNING last_value
            """
        ),
        {"month": month},
    ).scalar_one()
    if sequence > 999:
        raise HTTPException(status_code=409, detail="当月对账单流水号已超过999")
    return f"ST-{month.replace('-', '')}-{sequence:03d}"


def _validated_receipt_resolution(
    delivery_item: DeliveryItem,
    line: ReturnReceiptLineCreate,
) -> tuple[str | None, str | None]:
    actual = line.actual_received_quantity
    delivered = int(delivery_item.delivered_quantity or 0)
    action = (line.resolution_action or "").strip() or None
    reason = (line.difference_reason or "").strip() or None
    if actual < 0:
        raise HTTPException(
            status_code=400,
            detail=f"送货明细{delivery_item.id}实收数量不能小于0",
        )
    if actual == delivered:
        return None, reason
    if actual < delivered and action not in {"continue_delivery", "accept_short"}:
        raise HTTPException(
            status_code=400,
            detail=f"送货明细{delivery_item.id}短收后必须选择继续待送或按实收结单",
        )
    if actual > delivered and action != "accept_over":
        raise HTTPException(
            status_code=400,
            detail=f"送货明细{delivery_item.id}超收后必须确认按实际数量入账",
        )
    return action, reason


def _apply_receipt_order_effect(
    db: Session,
    *,
    delivery_item: DeliveryItem,
    actual_received_quantity: int,
    resolution_action: str | None,
    direction: int,
) -> int | None:
    if resolution_action not in {"continue_delivery", "accept_short", "accept_over"}:
        return None
    order_item = db.get(OrderItem, delivery_item.order_item_id)
    if order_item is None:
        raise HTTPException(status_code=409, detail="回单关联订单明细不存在")
    adjustment = (
        int(actual_received_quantity) - int(delivery_item.delivered_quantity or 0)
    ) * direction
    adjusted_quantity = int(order_item.delivered_quantity or 0) + adjustment
    if adjusted_quantity < 0:
        raise HTTPException(status_code=409, detail="回单数量与订单累计已送数量冲突")
    order_item.delivered_quantity = adjusted_quantity
    if resolution_action == "accept_short":
        order_item.is_force_closed = direction > 0
    return order_item.order_id


def _assert_no_later_dispatched_deliveries(
    db: Session,
    *,
    receipt: ReturnReceipt,
    receipt_items: list[ReturnReceiptItem],
    delivery_items: dict[int, DeliveryItem],
) -> None:
    """Do not reverse a receipt after its released balance has been dispatched."""
    relevant_order_item_ids = {
        delivery_items[item.delivery_item_id].order_item_id
        for item in receipt_items
        if item.resolution_action == "continue_delivery"
        and item.delivery_item_id in delivery_items
    }
    if not relevant_order_item_ids:
        return

    # Serialize edits for databases that support row-level locks. SQLite treats
    # this as a normal read, which is sufficient for the local deployment.
    db.scalars(
        select(OrderItem)
        .where(OrderItem.id.in_(relevant_order_item_ids))
        .with_for_update()
    ).all()

    for receipt_item in receipt_items:
        if receipt_item.resolution_action != "continue_delivery":
            continue
        source_item = delivery_items.get(receipt_item.delivery_item_id)
        if source_item is None:
            continue
        later_delivery_number = db.scalar(
            select(Delivery.delivery_number)
            .join(DeliveryItem, DeliveryItem.delivery_id == Delivery.id)
            .where(
                DeliveryItem.order_item_id == source_item.order_item_id,
                DeliveryItem.delivery_id != source_item.delivery_id,
                Delivery.status == "dispatched",
                Delivery.dispatched_at.is_not(None),
                Delivery.dispatched_at >= receipt.created_at,
            )
            .order_by(Delivery.dispatched_at, Delivery.id)
            .limit(1)
        )
        if later_delivery_number:
            raise HTTPException(
                status_code=409,
                detail=(
                    "该回单产生的待送数量已用于后续送货单 "
                    f"{later_delivery_number}，请先取消后续发货后再修改或取消旧回单。"
                ),
            )


def _refresh_receipt_order_statuses(db: Session, order_ids: set[int]) -> None:
    if not order_ids:
        return
    from app.api.deliveries import _refresh_order_status

    for order_id in order_ids:
        _refresh_order_status(db, order_id)


def _claim_return_receipt_status(
    db: Session,
    *,
    receipt_id: int,
    expected_status: str,
    next_status: str,
) -> None:
    try:
        claimed = db.execute(
            update(ReturnReceipt)
            .where(
                ReturnReceipt.id == receipt_id,
                ReturnReceipt.status == expected_status,
            )
            .values(status=next_status)
            .execution_options(synchronize_session=False)
        )
    except OperationalError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="回单正在被其他操作修改，请刷新后重试",
        ) from error
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="回单状态已变化，请刷新后重试",
        )


def _receipt_response(db: Session, receipt_id: int) -> dict:
    receipt = db.get(ReturnReceipt, receipt_id)
    rows = db.execute(
        select(
            ReturnReceiptItem.id,
            ReturnReceiptItem.delivery_item_id,
            DeliveryItem.delivered_quantity,
            ReturnReceiptItem.actual_received_quantity,
            ReturnReceiptItem.resolution_action,
            ReturnReceiptItem.difference_reason,
        )
        .join(
            DeliveryItem,
            DeliveryItem.id == ReturnReceiptItem.delivery_item_id,
        )
        .where(ReturnReceiptItem.return_receipt_id == receipt_id)
        .order_by(ReturnReceiptItem.id)
    ).all()
    return {
        "id": receipt.id,
        "delivery_id": receipt.delivery_id,
        "actual_received_date": receipt.actual_received_date,
        "signed_by": receipt.signed_by,
        "status": receipt.status,
        "items": [dict(row._mapping) for row in rows],
    }


@router.get("/return_receipts/{receipt_id}")
def get_return_receipt(
    receipt_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    _return_receipt_for_user(db, receipt_id, user)
    return _receipt_response(db, receipt_id)


@router.post("/return_receipts", status_code=status.HTTP_201_CREATED)
def create_return_receipt(
    payload: ReturnReceiptCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _delivery_for_user(db, payload.delivery_id, user)
    try:
        # 与取消发货竞争时，先对同一送货单执行条件写并取得写锁。
        # 第二个事务等待后会重新判断状态，不能同时确认回单和取消发货。
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == payload.delivery_id,
                Delivery.status == "dispatched",
            )
            .values(status="dispatched")
        )
        if claimed.rowcount != 1:
            exists = db.scalar(
                select(Delivery.id).where(Delivery.id == payload.delivery_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            raise HTTPException(status_code=400, detail="送货单尚未确认发货")
        delivery = db.get(Delivery, payload.delivery_id)
        delivery_items = db.scalars(
            select(DeliveryItem)
            .where(DeliveryItem.delivery_id == delivery.id)
            .order_by(DeliveryItem.id)
        ).all()
        requested = {line.delivery_item_id: line for line in payload.items}
        if set(requested) != {item.id for item in delivery_items}:
            raise HTTPException(
                status_code=400,
                detail="回单必须包含送货单全部明细",
            )
        receipt = db.scalar(
            select(ReturnReceipt).where(ReturnReceipt.delivery_id == delivery.id)
        )
        if receipt is not None and receipt.status == "confirmed":
            raise HTTPException(status_code=409, detail="该送货单已经提交回单")
        if receipt is None:
            receipt = ReturnReceipt(
                delivery_id=delivery.id,
                actual_received_date=payload.actual_received_date,
                signed_by=(payload.signed_by or "").strip() or None,
                status="confirmed",
                created_by=user.id,
            )
            db.add(receipt)
            db.flush()
        else:
            db.execute(
                delete(ReturnReceiptItem).where(
                    ReturnReceiptItem.return_receipt_id == receipt.id
                )
            )
            receipt.actual_received_date = payload.actual_received_date
            receipt.signed_by = (payload.signed_by or "").strip() or None
            receipt.status = "confirmed"
        affected_order_ids: set[int] = set()
        audit_items: list[dict] = []
        for item in delivery_items:
            line = requested[item.id]
            action, reason = _validated_receipt_resolution(item, line)
            db.add(
                ReturnReceiptItem(
                    return_receipt_id=receipt.id,
                    delivery_item_id=item.id,
                    actual_received_quantity=line.actual_received_quantity,
                    resolution_action=action,
                    difference_reason=reason,
                )
            )
            order_id = _apply_receipt_order_effect(
                db,
                delivery_item=item,
                actual_received_quantity=line.actual_received_quantity,
                resolution_action=action,
                direction=1,
            )
            if order_id is not None:
                affected_order_ids.add(order_id)
            audit_items.append(
                {
                    "delivery_item_id": item.id,
                    "delivered_quantity": item.delivered_quantity,
                    "actual_received_quantity": line.actual_received_quantity,
                    "resolution_action": action,
                    "difference_reason": reason,
                }
            )
        _refresh_receipt_order_statuses(db, affected_order_ids)
        _audit(
            db,
            user=user,
            action="CONFIRM_RETURN_RECEIPT",
            resource="ReturnReceipt",
            entity_id=receipt.id,
            details={
                "delivery_id": delivery.id,
                "actual_received_date": payload.actual_received_date,
                "item_count": len(delivery_items),
                "items": audit_items,
            },
            description="确认客户送货回单",
        )
        db.commit()
        return _receipt_response(db, receipt.id)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="该送货单已经提交回单") from error
    except Exception:
        db.rollback()
        raise


@router.put("/return_receipts/{receipt_id}")
def update_return_receipt(
    receipt_id: int,
    payload: ReturnReceiptUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    receipt = _return_receipt_for_user(db, receipt_id, user)
    claimed_status = receipt.status
    if claimed_status not in {"confirmed", "cancelled"}:
        raise HTTPException(status_code=409, detail="回单状态不允许修改")
    _claim_return_receipt_status(
        db,
        receipt_id=receipt.id,
        expected_status=claimed_status,
        next_status=claimed_status,
    )
    db.expire(receipt)
    receipt_items = db.scalars(
        select(ReturnReceiptItem)
        .where(ReturnReceiptItem.return_receipt_id == receipt.id)
        .order_by(ReturnReceiptItem.id)
    ).all()
    receipt_item_ids = [item.id for item in receipt_items]
    if receipt_item_ids and db.scalar(
        select(StatementItem.id)
        .where(StatementItem.return_receipt_item_id.in_(receipt_item_ids))
        .limit(1)
    ) is not None:
        raise HTTPException(status_code=409, detail="回单已进入对账，禁止修改")
    delivery_items = db.scalars(
        select(DeliveryItem)
        .where(DeliveryItem.delivery_id == receipt.delivery_id)
        .order_by(DeliveryItem.id)
    ).all()
    requested = {line.delivery_item_id: line for line in payload.items}
    if set(requested) != {item.id for item in delivery_items}:
        raise HTTPException(status_code=400, detail="回单必须包含送货单全部明细")
    existing = {item.delivery_item_id: item for item in receipt_items}
    delivery_by_id = {item.id: item for item in delivery_items}
    _assert_no_later_dispatched_deliveries(
        db,
        receipt=receipt,
        receipt_items=receipt_items,
        delivery_items=delivery_by_id,
    )
    before = _receipt_response(db, receipt.id)
    affected_order_ids: set[int] = set()
    if claimed_status == "confirmed":
        for previous in receipt_items:
            order_id = _apply_receipt_order_effect(
                db,
                delivery_item=delivery_by_id[previous.delivery_item_id],
                actual_received_quantity=previous.actual_received_quantity,
                resolution_action=previous.resolution_action,
                direction=-1,
            )
            if order_id is not None:
                affected_order_ids.add(order_id)
    for delivery_item in delivery_items:
        line = requested[delivery_item.id]
        action, reason = _validated_receipt_resolution(delivery_item, line)
        target = existing[delivery_item.id]
        target.actual_received_quantity = line.actual_received_quantity
        target.resolution_action = action
        target.difference_reason = reason
        order_id = _apply_receipt_order_effect(
            db,
            delivery_item=delivery_item,
            actual_received_quantity=line.actual_received_quantity,
            resolution_action=action,
            direction=1,
        )
        if order_id is not None:
            affected_order_ids.add(order_id)
    receipt.actual_received_date = payload.actual_received_date
    receipt.signed_by = (payload.signed_by or "").strip() or None
    receipt.status = "confirmed"
    _refresh_receipt_order_statuses(db, affected_order_ids)
    _audit(
        db,
        user=user,
        action="UPDATE_RETURN_RECEIPT",
        resource="ReturnReceipt",
        entity_id=receipt.id,
        details={"before": before, "after": payload.model_dump()},
        description="修改客户送货回单",
    )
    db.commit()
    return _receipt_response(db, receipt.id)


def _pending_statement_query(
    customer_id: int,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
):
    query = (
        select(
            ReturnReceiptItem.id.label("return_receipt_item_id"),
            ReturnReceipt.id.label("return_receipt_id"),
            ReturnReceipt.actual_received_date,
            Delivery.id.label("delivery_id"),
            Delivery.delivery_number,
            Delivery.delivery_date,
            Delivery.customer_id,
            Order.id.label("order_id"),
            Order.order_number,
            Order.customer_po,
            Product.product_code,
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
            ReturnReceiptItem.actual_received_quantity,
            OrderItem.unit_price,
            (
                ReturnReceiptItem.actual_received_quantity * OrderItem.unit_price
            ).label("receivable_amount"),
            ReturnReceiptItem.difference_reason,
            StatementItem.id.label("statement_item_id"),
            StatementItem.statement_id,
        )
        .join(
            ReturnReceipt,
            ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
        )
        .join(DeliveryItem, DeliveryItem.id == ReturnReceiptItem.delivery_item_id)
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .outerjoin(
            StatementItem,
            StatementItem.return_receipt_item_id == ReturnReceiptItem.id,
        )
        .where(
            Delivery.customer_id == customer_id,
            ReturnReceipt.status == "confirmed",
        )
        .order_by(
            Delivery.delivery_date,
            Delivery.delivery_number,
            ReturnReceiptItem.id,
        )
    )
    if period_start is not None:
        query = query.where(Delivery.delivery_date >= period_start)
    if period_end is not None:
        query = query.where(Delivery.delivery_date <= period_end)
    return query


def _pending_statement_groups(
    db: Session,
    customer_id: int,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> list[dict]:
    raw_rows = [
        dict(row._mapping)
        for row in db.execute(
            _pending_statement_query(
                customer_id,
                period_start=period_start,
                period_end=period_end,
            )
        )
    ]
    if not raw_rows:
        return []

    registry = build_display_registry(db)
    order_ids = {row["order_id"] for row in raw_rows}
    orders = {
        order.id: order
        for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    }
    grouped: dict[int, dict] = {}
    for data in raw_rows:
        receivable = Decimal(str(data["receivable_amount"] or 0)).quantize(
            MONEY,
            rounding=ROUND_HALF_UP,
        )
        data["receivable_amount"] = receivable
        data["is_reconciled"] = data["statement_item_id"] is not None
        order = orders.get(data["order_id"])
        display = (
            display_order_number(order, registry)
            if order is not None
            else data["order_number"]
        )
        data["order_number"] = display
        data["display_order_number"] = display
        delivery = grouped.setdefault(
            data["delivery_id"],
            {
                "delivery_id": data["delivery_id"],
                "delivery_number": data["delivery_number"],
                "delivery_date": data["delivery_date"],
                "actual_received_date": data["actual_received_date"],
                "return_receipt_id": data["return_receipt_id"],
                "items": [],
            },
        )
        delivery["items"].append(data)

    deliveries = []
    for delivery in grouped.values():
        rows = delivery["items"]
        reconciled_count = sum(1 for row in rows if row["is_reconciled"])
        if reconciled_count == len(rows):
            continue
        selection_blocked = reconciled_count > 0
        delivery.update(
            {
                "item_count": len(rows),
                "pending_item_count": len(rows) - reconciled_count,
                "total_received_quantity": sum(
                    int(row["actual_received_quantity"] or 0) for row in rows
                ),
                "total_receivable_amount": sum(
                    (row["receivable_amount"] for row in rows),
                    Decimal("0"),
                ).quantize(MONEY, rounding=ROUND_HALF_UP),
                "selection_blocked": selection_blocked,
                "exception_reason": (
                    "该送货单已有部分明细进入其他对账单，请先处理原对账单。"
                    if selection_blocked
                    else None
                ),
                "return_receipt_item_ids": [
                    row["return_receipt_item_id"]
                    for row in rows
                    if not row["is_reconciled"]
                ],
            }
        )
        deliveries.append(delivery)
    return deliveries


@router.get("/pending_statements")
def pending_statements(
    customer_id: int = Query(gt=0),
    statement_month: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    require_customer_access(customer_id, user, db)
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    period_start = period_end = None
    if statement_month:
        try:
            period_start, period_end = _statement_period(
                statement_month,
                customer.statement_cycle_start_day,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    deliveries = _pending_statement_groups(
        db,
        customer_id,
        period_start=period_start,
        period_end=period_end,
    )
    # 保留旧版平铺字段供尚未刷新前端的页面只读使用；只返回可整单选择的明细。
    rows = [
        item
        for delivery in deliveries
        if not delivery["selection_blocked"]
        for item in delivery["items"]
    ]
    return {
        "items": rows,
        "deliveries": deliveries,
        "statement_cycle_start_day": customer.statement_cycle_start_day,
        "period_start": period_start,
        "period_end": period_end,
    }


@router.post("/statements", status_code=status.HTTP_201_CREATED)
def create_statement(
    payload: StatementCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    customer = db.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    try:
        period_start, period_end = _statement_period(
            payload.statement_month,
            customer.statement_cycle_start_day,
        )
        compatibility_item_ids = set(payload.return_receipt_item_ids)
        selected_delivery_ids = set(payload.delivery_ids)
        if compatibility_item_ids:
            mapped_rows = db.execute(
                select(ReturnReceiptItem.id, Delivery.id)
                .join(
                    DeliveryItem,
                    DeliveryItem.id == ReturnReceiptItem.delivery_item_id,
                )
                .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
                .where(ReturnReceiptItem.id.in_(compatibility_item_ids))
            ).all()
            if {row[0] for row in mapped_rows} != compatibility_item_ids:
                raise HTTPException(status_code=400, detail="回单明细不存在")
            selected_delivery_ids = {row[1] for row in mapped_rows}

        selected_rows = db.execute(
            select(
                ReturnReceiptItem,
                ReturnReceipt,
                Delivery,
                OrderItem,
                Product,
                StatementItem.id.label("existing_statement_item_id"),
            )
            .join(
                ReturnReceipt,
                ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
            )
            .join(
                DeliveryItem,
                DeliveryItem.id == ReturnReceiptItem.delivery_item_id,
            )
            .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
            .join(Product, Product.id == OrderItem.product_id)
            .outerjoin(
                StatementItem,
                StatementItem.return_receipt_item_id == ReturnReceiptItem.id,
            )
            .where(Delivery.id.in_(selected_delivery_ids))
            .order_by(Delivery.id, ReturnReceiptItem.id)
        ).all()
        found_delivery_ids = {row[2].id for row in selected_rows}
        if found_delivery_ids != selected_delivery_ids:
            raise HTTPException(
                status_code=400,
                detail="所选送货单不存在已确认的客户回单明细",
            )
        all_item_ids = {row[0].id for row in selected_rows}
        if compatibility_item_ids and compatibility_item_ids != all_item_ids:
            raise HTTPException(
                status_code=400,
                detail="送货单必须整单对账，不能只选择其中部分存货编码。",
            )
        claimed_receipt_ids: set[int] = set()
        for row in selected_rows:
            receipt_item, receipt, delivery, _order_item, _product, existing_id = row
            require_customer_access(delivery.customer_id, user, db)
            if delivery.customer_id != payload.customer_id:
                raise HTTPException(status_code=400, detail="送货单客户不匹配")
            if receipt.status != "confirmed":
                raise HTTPException(status_code=409, detail="已取消回单不能生成对账单")
            if existing_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail="该送货单已有明细进入对账单，请先处理原对账单。",
                )
            if not period_start <= delivery.delivery_date <= period_end:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"送货单 {delivery.delivery_number} 的送货日期"
                        f"不属于 {payload.statement_month} 对账周期"
                        f"（{period_start} 至 {period_end}）。"
                    ),
                )
            if receipt.id not in claimed_receipt_ids:
                _claim_return_receipt_status(
                    db,
                    receipt_id=receipt.id,
                    expected_status="confirmed",
                    next_status="confirmed",
                )
                claimed_receipt_ids.add(receipt.id)

        statement = Statement(
            statement_number=_next_statement_number(
                db,
                payload.statement_month,
            ),
            customer_id=payload.customer_id,
            statement_month=payload.statement_month,
            total_receivable=Decimal("0"),
            total_gross_profit=Decimal("0"),
            status="unsettled",
            created_by=user.id,
        )
        db.add(statement)
        db.flush()
        total_receivable = Decimal("0")
        total_profit = Decimal("0")
        for row in selected_rows:
            receipt_item, _receipt, _delivery, order_item, product, _existing_id = row
            unit_price = Decimal(str(order_item.unit_price))
            unit_cost = Decimal(str(product.cost_unit_price or 0))
            quantity = Decimal(receipt_item.actual_received_quantity)
            receivable = (quantity * unit_price).quantize(
                MONEY,
                rounding=ROUND_HALF_UP,
            )
            profit = (quantity * (unit_price - unit_cost)).quantize(
                MONEY,
                rounding=ROUND_HALF_UP,
            )
            db.add(
                StatementItem(
                    statement_id=statement.id,
                    return_receipt_item_id=receipt_item.id,
                    actual_received_quantity=receipt_item.actual_received_quantity,
                    unit_price_snapshot=unit_price,
                    unit_cost_snapshot=unit_cost,
                    receivable_amount=receivable,
                    gross_profit_amount=profit,
                )
            )
            total_receivable += receivable
            total_profit += profit
        statement.total_receivable = total_receivable.quantize(MONEY)
        statement.total_gross_profit = total_profit.quantize(MONEY)
        _audit(
            db,
            user=user,
            action="CREATE_STATEMENT",
            resource="Statement",
            entity_id=statement.id,
            details={
                "statement_number": statement.statement_number,
                "statement_month": statement.statement_month,
                "delivery_count": len(selected_delivery_ids),
                "item_count": len(selected_rows),
                "total_receivable": statement.total_receivable,
                "total_gross_profit": statement.total_gross_profit,
            },
            description="生成客户月结对账单",
        )
        db.commit()
        return _redact_statement_costs(
            {
                "id": statement.id,
                "statement_number": statement.statement_number,
                "customer_id": statement.customer_id,
                "statement_month": statement.statement_month,
                "total_receivable": statement.total_receivable,
                "total_gross_profit": statement.total_gross_profit,
                "status": statement.status,
            },
            user,
        )
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="对账明细已被其他对账单使用") from error
    except Exception:
        db.rollback()
        raise


@router.post("/invoices", status_code=status.HTTP_201_CREATED)
def create_invoice(
    payload: InvoiceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _statement_for_user(db, payload.statement_id, user)
    try:
        updated = db.execute(
            text(
                """
                UPDATE finance_statements
                SET invoiced_amount = invoiced_amount + :amount
                WHERE id = :statement_id
                  AND invoiced_amount + :amount <= total_receivable
                RETURNING id, statement_number, total_receivable,
                          invoiced_amount, settled_amount, status
                """
            ),
            {
                "statement_id": payload.statement_id,
                "amount": str(payload.invoice_amount),
            },
        ).mappings().one_or_none()
        if updated is None:
            if db.get(Statement, payload.statement_id) is None:
                raise HTTPException(status_code=404, detail="对账单不存在")
            raise HTTPException(status_code=409, detail="累计开票金额不能超过应收总额")

        invoice = Invoice(
            statement_id=payload.statement_id,
            invoice_number=payload.invoice_number,
            invoice_date=payload.invoice_date,
            invoice_amount=payload.invoice_amount,
            created_by=user.id,
        )
        db.add(invoice)
        db.flush()
        _audit(
            db,
            user=user,
            action="REGISTER_INVOICE",
            resource="Invoice",
            entity_id=invoice.id,
            details={
                "statement_id": payload.statement_id,
                "invoice_number": payload.invoice_number,
                "invoice_date": payload.invoice_date,
                "invoice_amount": payload.invoice_amount,
                "invoiced_amount": updated["invoiced_amount"],
            },
            description="登记客户发票",
        )
        db.commit()
        return {
            "id": invoice.id,
            "statement_id": payload.statement_id,
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date,
            "invoice_amount": invoice.invoice_amount,
            "total_receivable": updated["total_receivable"],
            "invoiced_amount": updated["invoiced_amount"],
            "settled_amount": updated["settled_amount"],
            "status": updated["status"],
        }
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="发票号码已存在") from error
    except Exception:
        db.rollback()
        raise


@router.put("/statements/{statement_id}/settle")
def settle_statement(
    statement_id: int,
    payload: SettlementCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _statement_for_user(db, statement_id, user)
    try:
        updated = db.execute(
            text(
                """
                UPDATE finance_statements
                SET settled_amount = settled_amount + :amount,
                    status = CASE
                        WHEN settled_amount + :amount = total_receivable
                        THEN 'settled'
                        ELSE 'unsettled'
                    END
                WHERE id = :statement_id
                  AND settled_amount + :amount <= total_receivable
                RETURNING id, statement_number, total_receivable,
                          invoiced_amount, settled_amount, status
                """
            ),
            {
                "statement_id": statement_id,
                "amount": str(payload.amount),
            },
        ).mappings().one_or_none()
        if updated is None:
            if db.get(Statement, statement_id) is None:
                raise HTTPException(status_code=404, detail="对账单不存在")
            raise HTTPException(status_code=409, detail="累计收款金额不能超过应收总额")

        settlement = SettlementRecord(
            statement_id=statement_id,
            settled_amount=payload.amount,
            settlement_date=payload.settlement_date,
            account=payload.account or "",
            created_by=user.id,
        )
        db.add(settlement)
        db.flush()
        _audit(
            db,
            user=user,
            action="SETTLE_STATEMENT",
            resource="Statement",
            entity_id=statement_id,
            details={
                "settlement_record_id": settlement.id,
                "amount": payload.amount,
                "settlement_date": payload.settlement_date,
                "account": payload.account or "",
                "settled_amount": updated["settled_amount"],
                "status": updated["status"],
            },
            description="登记客户收款并核销对账单",
        )
        db.commit()
        return {
            "id": statement_id,
            "statement_number": updated["statement_number"],
            "total_receivable": updated["total_receivable"],
            "invoiced_amount": updated["invoiced_amount"],
            "settled_amount": updated["settled_amount"],
            "status": updated["status"],
            "status_label": (
                "已结清" if updated["status"] == "settled" else "未结清"
            ),
            "settlement_record_id": settlement.id,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/return_receipts/{receipt_id}/cancel")
def cancel_return_receipt(
    receipt_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    receipt = _return_receipt_for_user(db, receipt_id, user)
    if receipt.status != "confirmed":
        raise HTTPException(status_code=409, detail="回单状态已变化，不能重复取消")
    _claim_return_receipt_status(
        db,
        receipt_id=receipt.id,
        expected_status="confirmed",
        next_status="cancelled",
    )
    db.expire(receipt)
    receipt_item_ids = list(
        db.scalars(
            select(ReturnReceiptItem.id).where(
                ReturnReceiptItem.return_receipt_id == receipt.id
            )
        ).all()
    )
    if receipt_item_ids and db.scalar(
        select(StatementItem.id)
        .where(StatementItem.return_receipt_item_id.in_(receipt_item_ids))
        .limit(1)
    ) is not None:
        raise HTTPException(
            status_code=409,
            detail="该送货单已进入对账/结清流程，请先取消或编辑对应对账单。",
        )
    before = _receipt_response(db, receipt.id)
    before["status"] = "confirmed"
    receipt_items = db.scalars(
        select(ReturnReceiptItem).where(
            ReturnReceiptItem.return_receipt_id == receipt.id
        )
    ).all()
    delivery_items = {
        item.id: item
        for item in db.scalars(
            select(DeliveryItem).where(
                DeliveryItem.id.in_([item.delivery_item_id for item in receipt_items])
            )
        ).all()
    }
    _assert_no_later_dispatched_deliveries(
        db,
        receipt=receipt,
        receipt_items=receipt_items,
        delivery_items=delivery_items,
    )
    affected_order_ids: set[int] = set()
    for item in receipt_items:
        order_id = _apply_receipt_order_effect(
            db,
            delivery_item=delivery_items[item.delivery_item_id],
            actual_received_quantity=item.actual_received_quantity,
            resolution_action=item.resolution_action,
            direction=-1,
        )
        if order_id is not None:
            affected_order_ids.add(order_id)
    receipt.signed_by = None
    _refresh_receipt_order_statuses(db, affected_order_ids)
    _audit(
        db,
        user=user,
        action="CANCEL_RETURN_RECEIPT",
        resource="ReturnReceipt",
        entity_id=receipt.id,
        details={"before": before},
        description="撤销客户送货回单",
    )
    db.commit()
    return _receipt_response(db, receipt.id)


@router.get("/statements/{statement_id}")
def get_statement(
    statement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    return _statement_detail_response(db, statement_id, user)


@router.put("/statements/{statement_id}")
def update_statement(
    statement_id: int,
    payload: StatementUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    try:
        statement = _statement_for_user(db, statement_id, user)
        if db.scalar(
            select(Invoice.id).where(Invoice.statement_id == statement.id).limit(1)
        ) is not None:
            raise HTTPException(
                status_code=409,
                detail="该对账单已有开票记录，请先取消开票后再编辑。",
            )
        if db.scalar(
            select(SettlementRecord.id)
            .where(SettlementRecord.statement_id == statement.id)
            .limit(1)
        ) is not None:
            raise HTTPException(
                status_code=409,
                detail="该对账单已有收款记录，请先撤销收款后再编辑。",
            )
        old_month = statement.statement_month
        if payload.statement_month == old_month:
            return _statement_detail_response(db, statement.id, user)
        customer = db.get(Customer, statement.customer_id)
        if customer is None:
            raise HTTPException(status_code=409, detail="对账单关联客户不存在。")
        period_start, period_end = _statement_period(
            payload.statement_month,
            customer.statement_cycle_start_day,
        )
        delivery_rows = db.execute(
            select(Delivery.delivery_number, Delivery.delivery_date)
            .join(DeliveryItem, DeliveryItem.delivery_id == Delivery.id)
            .join(
                ReturnReceiptItem,
                ReturnReceiptItem.delivery_item_id == DeliveryItem.id,
            )
            .join(
                StatementItem,
                StatementItem.return_receipt_item_id == ReturnReceiptItem.id,
            )
            .where(StatementItem.statement_id == statement.id)
            .distinct()
        ).all()
        invalid_deliveries = [
            delivery_number
            for delivery_number, delivery_date in delivery_rows
            if not period_start <= delivery_date <= period_end
        ]
        if invalid_deliveries:
            raise HTTPException(
                status_code=409,
                detail=(
                    "对账月份必须覆盖全部送货单的送货日期；不属于该周期的送货单："
                    + "、".join(invalid_deliveries)
                ),
            )
        before = _statement_detail_response(db, statement.id, user)
        statement.statement_month = payload.statement_month
        statement.statement_number = _next_statement_number(db, payload.statement_month)
        _audit(
            db,
            user=user,
            action="UPDATE_STATEMENT",
            resource="Statement",
            entity_id=statement.id,
            details={"before": before, "after": {"statement_month": payload.statement_month}},
            description="修改月结对账单基础信息",
        )
        db.commit()
        return _statement_detail_response(db, statement.id, user)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/statements/{statement_id}/cancel")
def cancel_statement(
    statement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    try:
        statement = _statement_for_user(db, statement_id, user)
        if db.scalar(
            select(Invoice.id).where(Invoice.statement_id == statement.id).limit(1)
        ) is not None:
            raise HTTPException(
                status_code=409,
                detail="该对账单已有开票记录，请先取消开票后再取消对账单。",
            )
        if db.scalar(
            select(SettlementRecord.id)
            .where(SettlementRecord.statement_id == statement.id)
            .limit(1)
        ) is not None:
            raise HTTPException(
                status_code=409,
                detail="该对账单已有收款记录，请先撤销收款后再取消对账单。",
            )
        before = _statement_detail_response(db, statement.id, user)
        db.execute(delete(StatementItem).where(StatementItem.statement_id == statement.id))
        db.execute(delete(Statement).where(Statement.id == statement.id))
        _audit(
            db,
            user=user,
            action="CANCEL_STATEMENT",
            resource="Statement",
            entity_id=statement.id,
            details={"before": before},
            description="取消月结对账单",
        )
        db.commit()
        return {"status": "cancelled", "statement_id": statement_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
