from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pydantic import BaseModel, field_validator
from sqlalchemy import and_, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
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
finance_only = RoleChecker(["admin", "finance"])
MONEY = Decimal("0.00")

_CITY_PREFIXES = ("苏州", "昆山", "常熟", "太仓", "上海", "无锡", "南京", "杭州", "深圳", "广州")
_COMPANY_SUFFIXES = ("股份有限公司", "有限责任公司", "科技有限公司", "有限公司")
_ILLEGAL_CHARS = r'/\:*?"<>|'


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
    return_receipt_item_ids: list[int]

    @field_validator("statement_month")
    @classmethod
    def validate_month(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
            raise ValueError("对账月份格式必须为 YYYY-MM")
        return value

    @field_validator("return_receipt_item_ids")
    @classmethod
    def validate_ids(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("至少选择一条待对账明细")
        if len(value) != len(set(value)):
            raise ValueError("待对账明细不能重复")
        return value


class StatementUpdate(BaseModel):
    statement_month: str

    @field_validator("statement_month")
    @classmethod
    def validate_month(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
            raise ValueError("对账月份格式必须为 YYYY-MM")
        return value


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
    _user: User = Depends(finance_only),
) -> dict:
    query = (
        select(Statement, Customer.name.label("customer_name"))
        .join(Customer, Customer.id == Statement.customer_id)
        .order_by(Statement.statement_month.desc(), Statement.id.desc())
    )
    if customer_id is not None:
        query = query.where(Statement.customer_id == customer_id)
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
            }
            for statement, customer_name in rows
        ],
    }


@router.get("/statement-customers")
def list_statement_customers(
    statement_month: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(finance_only),
) -> dict:
    query = (
        select(
            Customer.id,
            Customer.name,
            func.count(ReturnReceiptItem.id).label("pending_count"),
        )
        .join(Delivery, Delivery.customer_id == Customer.id)
        .join(ReturnReceipt, ReturnReceipt.delivery_id == Delivery.id)
        .join(ReturnReceiptItem, ReturnReceiptItem.return_receipt_id == ReturnReceipt.id)
        .outerjoin(
            StatementItem,
            StatementItem.return_receipt_item_id == ReturnReceiptItem.id,
        )
        .where(
            ReturnReceipt.status == "confirmed",
            StatementItem.id.is_(None),
        )
    )
    if statement_month:
        query = query.where(
            func.strftime("%Y-%m", ReturnReceipt.actual_received_date)
            == statement_month
        )
    rows = db.execute(
        query.group_by(Customer.id, Customer.name).order_by(Customer.name)
    ).all()
    return {
        "items": [
            {
                "id": customer_id,
                "name": customer_name,
                "pending_count": pending_count,
            }
            for customer_id, customer_name, pending_count in rows
        ]
    }


def _statement_detail_response(db: Session, statement_id: int) -> dict:
    row = db.execute(
        select(Statement, Customer.name.label("customer_name"))
        .join(Customer, Customer.id == Statement.customer_id)
        .where(Statement.id == statement_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="对账单不存在")
    statement, customer_name = row
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
    return {
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
    }


@router.get("/statements/{statement_id}/export")
def export_statement_excel(
    statement_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(finance_only),
) -> StreamingResponse:
    row = db.execute(
        select(Statement, Customer)
        .join(Customer, Customer.id == Statement.customer_id)
        .where(Statement.id == statement_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="对账单不存在")
    statement, customer = row
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
    _user: User = Depends(finance_only),
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
        query = query.where(Invoice.statement_id == statement_id)
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


def _receipt_response(db: Session, receipt_id: int) -> dict:
    receipt = db.get(ReturnReceipt, receipt_id)
    rows = db.execute(
        select(
            ReturnReceiptItem.id,
            ReturnReceiptItem.delivery_item_id,
            DeliveryItem.delivered_quantity,
            ReturnReceiptItem.actual_received_quantity,
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
    _user: User = Depends(finance_only),
) -> dict:
    if db.get(ReturnReceipt, receipt_id) is None:
        raise HTTPException(status_code=404, detail="回单不存在")
    return _receipt_response(db, receipt_id)


@router.post("/return_receipts", status_code=status.HTTP_201_CREATED)
def create_return_receipt(
    payload: ReturnReceiptCreate,
    db: Session = Depends(get_db),
    user: User = Depends(finance_only),
) -> dict:
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
        for item in delivery_items:
            line = requested[item.id]
            if (
                line.actual_received_quantity < 0
                or line.actual_received_quantity > item.delivered_quantity
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"送货明细{item.id}实收数量超出有效范围",
                )
            reason = (line.difference_reason or "").strip()
            if (
                line.actual_received_quantity < item.delivered_quantity
                and not reason
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"送货明细{item.id}存在数量差异，必须填写原因",
                )
            db.add(
                ReturnReceiptItem(
                    return_receipt_id=receipt.id,
                    delivery_item_id=item.id,
                    actual_received_quantity=line.actual_received_quantity,
                    difference_reason=reason or None,
                )
            )
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
    user: User = Depends(finance_only),
) -> dict:
    receipt = db.get(ReturnReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="回单不存在")
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
    before = _receipt_response(db, receipt.id)
    for delivery_item in delivery_items:
        line = requested[delivery_item.id]
        if (
            line.actual_received_quantity < 0
            or line.actual_received_quantity > delivery_item.delivered_quantity
        ):
            raise HTTPException(
                status_code=400,
                detail=f"送货明细{delivery_item.id}实收数量超出有效范围",
            )
        reason = (line.difference_reason or "").strip()
        if (
            line.actual_received_quantity != delivery_item.delivered_quantity
            and not reason
        ):
            raise HTTPException(
                status_code=400,
                detail=f"送货明细{delivery_item.id}存在数量差异，必须填写原因",
            )
        target = existing[delivery_item.id]
        target.actual_received_quantity = line.actual_received_quantity
        target.difference_reason = reason or None
    receipt.actual_received_date = payload.actual_received_date
    receipt.signed_by = (payload.signed_by or "").strip() or None
    receipt.status = "confirmed"
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


def _pending_statement_query(customer_id: int):
    return (
        select(
            ReturnReceiptItem.id.label("return_receipt_item_id"),
            ReturnReceipt.actual_received_date,
            Delivery.delivery_number,
            Delivery.delivery_date,
            Delivery.customer_id,
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
            StatementItem.id.is_(None),
        )
        .order_by(
            ReturnReceipt.actual_received_date,
            Delivery.delivery_number,
            ReturnReceiptItem.id,
        )
    )


@router.get("/pending_statements")
def pending_statements(
    customer_id: int = Query(gt=0),
    db: Session = Depends(get_db),
    _user: User = Depends(finance_only),
) -> dict:
    registry = build_display_registry(db)
    order_ids = {
        row[0]
        for row in db.execute(
            select(Order.id).where(Order.customer_id == customer_id)
        ).all()
    }
    orders = {
        order.id: order for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    } if order_ids else {}
    rows = []
    for row in db.execute(_pending_statement_query(customer_id)):
        data = dict(row._mapping)
        data["receivable_amount"] = (
            Decimal(str(data["receivable_amount"] or 0))
            .quantize(MONEY, rounding=ROUND_HALF_UP)
        )
        rows.append(data)
    if rows:
        pending_ids = {
            row[0]: row[1]
            for row in db.execute(
                select(ReturnReceiptItem.id, Order.id)
                .join(DeliveryItem, DeliveryItem.id == ReturnReceiptItem.delivery_item_id)
                .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(ReturnReceiptItem.id.in_([item["return_receipt_item_id"] for item in rows]))
            ).all()
        }
        for item in rows:
            order = orders.get(pending_ids.get(item["return_receipt_item_id"]))
            display = display_order_number(order, registry) if order is not None else item["order_number"]
            item["order_number"] = display
            item["display_order_number"] = display
    return {"items": rows}


@router.post("/statements", status_code=status.HTTP_201_CREATED)
def create_statement(
    payload: StatementCreate,
    db: Session = Depends(get_db),
    user: User = Depends(finance_only),
) -> dict:
    if db.get(Customer, payload.customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    try:
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
        for receipt_item_id in payload.return_receipt_item_ids:
            row = db.execute(
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
                    StatementItem.return_receipt_item_id
                    == ReturnReceiptItem.id,
                )
                .where(ReturnReceiptItem.id == receipt_item_id)
            ).one_or_none()
            if row is None:
                raise HTTPException(status_code=400, detail="回单明细不存在")
            receipt_item, receipt, delivery, order_item, product, existing_id = row
            if existing_id is not None:
                raise HTTPException(status_code=409, detail="回单明细已完成对账")
            if delivery.customer_id != payload.customer_id:
                raise HTTPException(status_code=400, detail="回单明细客户不匹配")
            if receipt.actual_received_date.strftime("%Y-%m") != payload.statement_month:
                raise HTTPException(status_code=400, detail="回单日期不属于对账月份")

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
                "item_count": len(payload.return_receipt_item_ids),
                "total_receivable": statement.total_receivable,
                "total_gross_profit": statement.total_gross_profit,
            },
            description="生成客户月结对账单",
        )
        db.commit()
        return {
            "id": statement.id,
            "statement_number": statement.statement_number,
            "customer_id": statement.customer_id,
            "statement_month": statement.statement_month,
            "total_receivable": statement.total_receivable,
            "total_gross_profit": statement.total_gross_profit,
            "status": statement.status,
        }
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
    user: User = Depends(finance_only),
) -> dict:
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
    user: User = Depends(finance_only),
) -> dict:
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
    user: User = Depends(finance_only),
) -> dict:
    receipt = db.get(ReturnReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="回单不存在")
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
    receipt.status = "cancelled"
    receipt.signed_by = None
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
    _user: User = Depends(finance_only),
) -> dict:
    return _statement_detail_response(db, statement_id)


@router.put("/statements/{statement_id}")
def update_statement(
    statement_id: int,
    payload: StatementUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(finance_only),
) -> dict:
    try:
        statement = db.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="对账单不存在")
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
            return _statement_detail_response(db, statement.id)
        receipt_months = {
            row[0]
            for row in db.execute(
                select(func.strftime("%Y-%m", ReturnReceipt.actual_received_date))
                .join(
                    ReturnReceiptItem,
                    ReturnReceiptItem.return_receipt_id == ReturnReceipt.id,
                )
                .join(
                    StatementItem,
                    StatementItem.return_receipt_item_id == ReturnReceiptItem.id,
                )
                .where(StatementItem.statement_id == statement.id)
            ).all()
        }
        if receipt_months and receipt_months != {payload.statement_month}:
            raise HTTPException(
                status_code=409,
                detail="对账月份必须与该对账单明细的回单月份一致。",
            )
        before = _statement_detail_response(db, statement.id)
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
        return _statement_detail_response(db, statement.id)
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
    user: User = Depends(finance_only),
) -> dict:
    try:
        statement = db.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="对账单不存在")
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
        before = _statement_detail_response(db, statement.id)
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
