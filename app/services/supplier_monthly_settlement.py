from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.time_contract import (
    beijing_date_bounds_utc_naive,
    beijing_today,
    utc_naive_to_beijing_date,
    utc_now_naive,
)
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseCancellation,
    ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseOrder,
    ExternalPackagingReceipt,
    ExternalPackagingReceiptItem,
)
from app.models.finance_payable import FinancePayable
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
    PurchaseReceiptFact,
)
from app.models.requisition import Requisition, RequisitionItem
from app.models.supplier import Supplier
from app.models.stock_replenishment import (
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.supplier_settlement import (
    SupplierReceiptSettlementPriceFact,
    SupplierMonthlyAdjustment,
    SupplierMonthlyInvoice,
    SupplierMonthlyPayment,
    SupplierMonthlyStatement,
    SupplierMonthlyStatementLine,
)
from app.models.user import User
from app.services.purchase_receipt_facts import (
    PurchaseReceiptFactValidationError,
    calculate_purchase_sheet_cost_breakdown,
)
from app.services.supplier_master import SupplierLookupError, resolve_supplier
from app.services.supplier_receipt_price_facts import (
    stock_replenishment_uses_paperboard_price,
)


MONEY = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")
ACTIVE_DRAFT_STATUSES = frozenset({"draft", "difference"})
CONFIRMED_STATUSES = frozenset(
    {
        "confirmed_pending_invoice",
        "invoiced_pending_payment",
        "partial_payment",
        "paid",
    }
)


class SupplierSettlementError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class SettlementCandidate:
    supplier_id: int
    supplier_name: str
    source_type: str
    source_key: str
    incoming_receipt_item_id: int | None
    external_receipt_item_id: int | None
    supplier_receipt_price_fact_id: int | None
    purchase_document_number: str
    receipt_number: str
    receipt_date: date
    category_label: str
    specification_snapshot: str | None
    material_or_product_snapshot: str
    received_quantity: Decimal
    quantity_unit: str
    frozen_unit_price: Decimal
    price_unit: str
    currency: str
    tax_basis: str
    tax_rate: Decimal
    erp_amount: Decimal
    tax_amount: Decimal
    source_link: str


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _six(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


def _plain_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _shift_month(value: str, offset: int) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(value or "")):
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_MONTH_INVALID",
            "供应商月结月份格式必须为 YYYY-MM",
            422,
        )
    year, month = (int(part) for part in value.split("-"))
    position = year * 12 + month - 1 + offset
    return f"{position // 12:04d}-{position % 12 + 1:02d}"


def settlement_period(settlement_month: str) -> tuple[date, date]:
    current = date.fromisoformat(f"{_shift_month(settlement_month, 0)}-01")
    previous = date.fromisoformat(f"{_shift_month(settlement_month, -1)}-01")
    return date(previous.year, previous.month, 21), date(current.year, current.month, 20)


def default_closed_settlement_month(today: date | None = None) -> str:
    business_date = today or beijing_today()
    current = business_date.strftime("%Y-%m")
    return current if business_date.day > 20 else _shift_month(current, -1)


def _utc_period_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    start_utc, _ = beijing_date_bounds_utc_naive(start)
    _, end_utc = beijing_date_bounds_utc_naive(end)
    return start_utc, end_utc


def settlement_period_utc_bounds(
    settlement_month: str,
) -> tuple[date, date, datetime, datetime]:
    start, end = settlement_period(settlement_month)
    start_utc, end_utc = _utc_period_bounds(start, end)
    return start, end, start_utc, end_utc


def _issue(
    *,
    source_type: str,
    source_key: str,
    receipt_number: str,
    code: str,
    message: str,
    supplier_name: str | None = None,
    purchase_document_number: str | None = None,
    missing_fields: list[str] | None = None,
    recommended_action: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_type": source_type,
        "source_key": source_key,
        "receipt_number": receipt_number,
        "code": code,
        "message": message,
    }
    if supplier_name:
        payload["supplier_name"] = supplier_name
    if purchase_document_number:
        payload["purchase_document_number"] = purchase_document_number
    if missing_fields:
        payload["missing_fields"] = missing_fields
    if recommended_action:
        payload["recommended_action"] = recommended_action
    return payload


def _paperboard_source(
    db: Session, item: IncomingReceiptItem
) -> tuple[
    SupplierRequisitionOrderItem | RequisitionItem | StockReplenishmentOrderItem | None,
    str,
    str,
]:
    if item.supplier_order_item_id is not None:
        source = db.get(SupplierRequisitionOrderItem, item.supplier_order_item_id)
        if source is None:
            return None, "", ""
        header = db.get(SupplierRequisitionOrder, source.supplier_order_id)
        supplier_name = str(
            source.supplier_name_snapshot
            or (header.supplier_name if header is not None else "")
            or ""
        ).strip()
        document_number = str(
            header.order_number if header is not None else source.supplier_order_id
        )
        return source, supplier_name, document_number
    if item.requisition_item_id is not None:
        source = db.get(RequisitionItem, item.requisition_item_id)
        if source is None:
            return None, "", ""
        header = db.get(Requisition, source.requisition_id)
        supplier_name = str(header.supplier_name if header is not None else "").strip()
        document_number = str(
            header.requisition_number if header is not None else source.requisition_id
        )
        return source, supplier_name, document_number
    if item.stock_replenishment_item_id is not None:
        source = db.get(
            StockReplenishmentOrderItem, item.stock_replenishment_item_id
        )
        if source is None:
            return None, "", ""
        header = db.get(StockReplenishmentOrder, source.replenishment_order_id)
        supplier_name = str(
            header.supplier_name if header is not None else ""
        ).strip()
        document_number = str(
            header.order_number if header is not None else source.replenishment_order_id
        )
        return source, supplier_name, document_number
    return None, "", ""


def _paperboard_dimensions(
    source: SupplierRequisitionOrderItem
    | RequisitionItem
    | StockReplenishmentOrderItem,
) -> tuple[Decimal, Decimal]:
    if isinstance(source, SupplierRequisitionOrderItem):
        length, width = source.report_length_mm, source.report_width_mm
    elif isinstance(source, StockReplenishmentOrderItem):
        length, width = source.report_length_mm, source.report_width_mm
    else:
        length, width = source.cardboard_len, source.cardboard_width
    normalized_length = Decimal(str(length or 0))
    normalized_width = Decimal(str(width or 0))
    if normalized_length <= 0 or normalized_width <= 0:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_DIMENSIONS_MISSING",
            "纸板收料缺少冻结报料长宽，不能进入供应商月结",
        )
    return normalized_length, normalized_width


def _scan_paperboard(
    db: Session,
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[list[SettlementCandidate], list[dict[str, Any]]]:
    frozen_start_date = utc_naive_to_beijing_date(start_utc)
    frozen_end_exclusive = utc_naive_to_beijing_date(end_utc)
    rows = db.execute(
        select(
            IncomingReceiptItem,
            IncomingReceipt,
            IncomingReceiptPurposeAllocation,
            PurchaseReceiptFact,
            SupplierReceiptSettlementPriceFact,
            IncomingReceiptPurposeReversal.id,
        )
        .join(IncomingReceipt, IncomingReceipt.id == IncomingReceiptItem.receipt_id)
        .outerjoin(
            IncomingReceiptPurposeAllocation,
            IncomingReceiptPurposeAllocation.incoming_receipt_item_id
            == IncomingReceiptItem.id,
        )
        .outerjoin(
            PurchaseReceiptFact,
            PurchaseReceiptFact.id
            == IncomingReceiptPurposeAllocation.purchase_receipt_fact_id,
        )
        .outerjoin(
            SupplierReceiptSettlementPriceFact,
            SupplierReceiptSettlementPriceFact.incoming_receipt_item_id
            == IncomingReceiptItem.id,
        )
        .outerjoin(
            IncomingReceiptPurposeReversal,
            IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id
            == IncomingReceiptPurposeAllocation.id,
        )
        .where(
            IncomingReceipt.status == "posted",
            IncomingReceiptItem.status == "posted",
            or_(
                and_(
                    SupplierReceiptSettlementPriceFact.id.is_not(None),
                    SupplierReceiptSettlementPriceFact.receipt_date_snapshot
                    >= frozen_start_date,
                    SupplierReceiptSettlementPriceFact.receipt_date_snapshot
                    < frozen_end_exclusive,
                ),
                and_(
                    SupplierReceiptSettlementPriceFact.id.is_(None),
                    IncomingReceipt.received_at >= start_utc,
                    IncomingReceipt.received_at < end_utc,
                ),
            ),
        )
        .order_by(IncomingReceipt.received_at, IncomingReceiptItem.id)
    ).all()
    candidates: list[SettlementCandidate] = []
    issues: list[dict[str, Any]] = []
    supplier_cache: dict[str, Supplier] = {}
    for item, receipt, allocation, price_fact, receipt_price_fact, reversal_id in rows:
        source_key = f"paperboard:{int(item.id)}"
        if reversal_id is not None:
            continue
        source, source_supplier_name, source_document_number = _paperboard_source(db, item)
        if (
            receipt_price_fact is None
            and isinstance(source, StockReplenishmentOrderItem)
            and not stock_replenishment_uses_paperboard_price(db, source)
        ):
            issues.append(
                _issue(
                    source_type="finished_replenishment",
                    source_key=source_key,
                    receipt_number=receipt.receipt_number,
                    code="FINISHED_REPLENISHMENT_PAYABLE_SOURCE_MISSING",
                    message=(
                        "成品补库实收没有纸板或外购包材冻结价格来源，"
                        "未计入供应商月结"
                    ),
                    supplier_name=source_supplier_name,
                    purchase_document_number=source_document_number,
                    missing_fields=["正式采购价格来源"],
                    recommended_action="核对该明细是否应通过外购包材采购单收料",
                )
            )
            continue
        if price_fact is None and receipt_price_fact is None:
            missing_fields = ["冻结结算价格"]
            if source is None:
                missing_fields.append("正式采购来源")
            issues.append(
                _issue(
                    source_type="paperboard",
                    source_key=source_key,
                    receipt_number=receipt.receipt_number,
                    code="PAPERBOARD_FROZEN_PRICE_MISSING",
                    message=(
                        "纸板实收缺少冻结结算价格，未计入月结草稿；"
                        "内部采购用途成本不是供应商应付价格"
                    ),
                    supplier_name=source_supplier_name,
                    purchase_document_number=source_document_number,
                    missing_fields=missing_fields,
                    recommended_action=(
                        "先检查历史缺价采用清单；仅唯一匹配供应商和材质的记录可按老板确认采用"
                    ),
                )
            )
            continue
        if receipt_price_fact is None and source is None:
            issues.append(
                _issue(
                    source_type="paperboard",
                    source_key=source_key,
                    receipt_number=receipt.receipt_number,
                    code="PAPERBOARD_PURCHASE_SOURCE_MISSING",
                    message="纸板实收缺少正式采购来源，未计入月结草稿",
                    missing_fields=["正式采购来源"],
                    recommended_action="核对收料明细关联的供应商采购单或旧报料单",
                )
            )
            continue
        try:
            if receipt_price_fact is not None:
                supplier = db.get(Supplier, receipt_price_fact.supplier_id)
                if supplier is None:
                    raise SupplierSettlementError(
                        "PAPERBOARD_SUPPLIER_MISSING",
                        "结算价格事实关联的供应商主档不存在",
                    )
                if (
                    Decimal(receipt_price_fact.received_quantity_snapshot)
                    != Decimal(int(item.received_quantity))
                    or receipt_price_fact.receipt_number_snapshot
                    != receipt.receipt_number
                ):
                    raise SupplierSettlementError(
                        "PAPERBOARD_RECEIPT_PRICE_FACT_MISMATCH",
                        "结算价格事实与当前实收数量或收料单号不一致，已停止月结",
                    )
                length = Decimal(receipt_price_fact.report_length_mm)
                width = Decimal(receipt_price_fact.report_width_mm)
                unit_price = receipt_price_fact.unit_price
                price_unit = receipt_price_fact.price_unit
                tax_included = receipt_price_fact.tax_included
                tax_rate = receipt_price_fact.tax_rate
                currency = receipt_price_fact.currency
                material_snapshot = receipt_price_fact.material_code_snapshot
                document_number = receipt_price_fact.purchase_document_number_snapshot
                receipt_number = receipt_price_fact.receipt_number_snapshot
                quantity = Decimal(receipt_price_fact.received_quantity_snapshot)
                quantity_unit = receipt_price_fact.quantity_unit
                receipt_date = receipt_price_fact.receipt_date_snapshot
            else:
                supplier = supplier_cache.get(source_supplier_name)
                if supplier is None:
                    supplier = resolve_supplier(
                        db, source_supplier_name, require_active=False
                    )
                    supplier_cache[source_supplier_name] = supplier
                assert source is not None and price_fact is not None
                length, width = _paperboard_dimensions(source)
                unit_price = price_fact.unit_price
                price_unit = price_fact.price_unit
                tax_included = price_fact.tax_included
                tax_rate = price_fact.tax_rate
                currency = price_fact.currency
                material_snapshot = price_fact.actual_material_code_snapshot
                document_number = source_document_number
                receipt_number = receipt.receipt_number
                quantity = Decimal(int(item.received_quantity))
                quantity_unit = "张"
                receipt_date = utc_naive_to_beijing_date(receipt.received_at)
            breakdown = calculate_purchase_sheet_cost_breakdown(
                unit_price=unit_price,
                price_unit=price_unit,
                tax_included=tax_included,
                tax_rate=tax_rate,
                report_length_mm=length,
                report_width_mm=width,
            )
        except (SupplierLookupError, SupplierSettlementError, PurchaseReceiptFactValidationError) as error:
            issues.append(
                _issue(
                    source_type="paperboard",
                    source_key=source_key,
                    receipt_number=receipt.receipt_number,
                    code=getattr(error, "code", "PAPERBOARD_PRICE_INVALID"),
                    message=str(error),
                    supplier_name=(
                        receipt_price_fact.supplier_name_snapshot
                        if receipt_price_fact is not None
                        else source_supplier_name
                    ),
                    purchase_document_number=(
                        receipt_price_fact.purchase_document_number_snapshot
                        if receipt_price_fact is not None
                        else source_document_number
                    ),
                    recommended_action="核对冻结价格事实后重新生成月结草稿",
                )
            )
            continue
        display_name = (
            receipt_price_fact.supplier_name_snapshot
            if receipt_price_fact is not None
            else supplier.display_name or supplier.standard_name
        )
        candidates.append(
            SettlementCandidate(
                supplier_id=int(supplier.id),
                supplier_name=display_name,
                source_type="paperboard",
                source_key=source_key,
                incoming_receipt_item_id=int(item.id),
                external_receipt_item_id=None,
                supplier_receipt_price_fact_id=(
                    int(receipt_price_fact.id) if receipt_price_fact is not None else None
                ),
                purchase_document_number=document_number,
                receipt_number=receipt_number,
                receipt_date=receipt_date,
                category_label="瓦楞纸板",
                specification_snapshot=(
                    f"{_plain_decimal(length)}×{_plain_decimal(width)}mm"
                ),
                material_or_product_snapshot=material_snapshot,
                received_quantity=quantity,
                quantity_unit=quantity_unit,
                frozen_unit_price=_six(unit_price),
                price_unit=price_unit,
                currency=currency,
                tax_basis=(
                    "tax_inclusive" if tax_included else "tax_exclusive"
                ),
                tax_rate=_six(tax_rate),
                erp_amount=_money(breakdown.gross_per_sheet * quantity),
                tax_amount=_money(breakdown.tax_per_sheet * quantity),
                source_link=f"/incoming.html?receipt_item_id={int(item.id)}",
            )
        )
    return candidates, issues


def _external_amounts(
    item: ExternalPackagingPurchaseItem, quantity: Decimal
) -> tuple[Decimal, Decimal]:
    line_amount = _money(quantity * Decimal(item.unit_price))
    rate = Decimal(item.tax_rate)
    if item.tax_amount_per_unit is not None:
        tax_amount = _money(quantity * Decimal(item.tax_amount_per_unit))
    elif rate <= 0:
        tax_amount = Decimal("0.00")
    elif item.tax_mode == "tax_exclusive":
        tax_amount = _money(line_amount * rate)
    else:
        tax_amount = _money(line_amount - line_amount / (Decimal("1") + rate))
    total = _money(
        line_amount + tax_amount
        if item.tax_mode == "tax_exclusive"
        else line_amount
    )
    return total, tax_amount


def _scan_external_packaging(
    db: Session,
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[list[SettlementCandidate], list[dict[str, Any]]]:
    rows = db.execute(
        select(
            ExternalPackagingReceiptItem,
            ExternalPackagingReceipt,
            ExternalPackagingPurchaseItem,
            ExternalPackagingPurchaseOrder,
        )
        .join(
            ExternalPackagingReceipt,
            ExternalPackagingReceipt.id == ExternalPackagingReceiptItem.receipt_id,
        )
        .join(
            ExternalPackagingPurchaseItem,
            ExternalPackagingPurchaseItem.id
            == ExternalPackagingReceiptItem.purchase_item_id,
        )
        .join(
            ExternalPackagingPurchaseOrder,
            ExternalPackagingPurchaseOrder.id
            == ExternalPackagingPurchaseItem.purchase_order_id,
        )
        .outerjoin(
            ExternalPackagingPurchaseCancellation,
            ExternalPackagingPurchaseCancellation.purchase_order_id
            == ExternalPackagingPurchaseOrder.id,
        )
        .where(
            ExternalPackagingReceipt.received_at >= start_utc,
            ExternalPackagingReceipt.received_at < end_utc,
            ExternalPackagingPurchaseCancellation.id.is_(None),
        )
        .order_by(
            ExternalPackagingReceipt.received_at,
            ExternalPackagingReceiptItem.id,
        )
    ).all()
    candidates: list[SettlementCandidate] = []
    issues: list[dict[str, Any]] = []
    for receipt_item, receipt, purchase_item, purchase in rows:
        source_key = f"external_packaging:{int(receipt_item.id)}"
        supplier = db.get(Supplier, purchase.supplier_id)
        if supplier is None:
            issues.append(
                _issue(
                    source_type="external_packaging",
                    source_key=source_key,
                    receipt_number=receipt.receipt_number,
                    code="EXTERNAL_PACKAGING_SUPPLIER_MISSING",
                    message="外购包材采购单的供应商主档不存在，未计入月结草稿",
                )
            )
            continue
        quantity = Decimal(receipt_item.received_quantity)
        total, tax = _external_amounts(purchase_item, quantity)
        candidates.append(
            SettlementCandidate(
                supplier_id=int(supplier.id),
                supplier_name=(supplier.display_name or supplier.standard_name),
                source_type="external_packaging",
                source_key=source_key,
                incoming_receipt_item_id=None,
                external_receipt_item_id=int(receipt_item.id),
                supplier_receipt_price_fact_id=None,
                purchase_document_number=purchase.purchase_number,
                receipt_number=receipt.receipt_number,
                receipt_date=utc_naive_to_beijing_date(receipt.received_at),
                category_label=purchase_item.category_code_snapshot,
                specification_snapshot=purchase_item.specification_summary_snapshot,
                material_or_product_snapshot=purchase_item.product_name_snapshot,
                received_quantity=_six(quantity),
                quantity_unit=receipt_item.purchase_unit_snapshot,
                frozen_unit_price=_six(purchase_item.unit_price),
                price_unit=purchase_item.purchase_unit,
                currency=purchase_item.currency,
                tax_basis=purchase_item.tax_mode,
                tax_rate=_six(purchase_item.tax_rate),
                erp_amount=total,
                tax_amount=tax,
                source_link=(
                    f"/incoming.html?external_packaging_receipt_id={int(receipt.id)}"
                ),
            )
        )
    return candidates, issues


def scan_settlement_candidates(
    db: Session, *, settlement_month: str
) -> tuple[list[SettlementCandidate], list[dict[str, Any]], date, date]:
    start, end, start_utc, end_utc = settlement_period_utc_bounds(settlement_month)
    paperboard, paperboard_issues = _scan_paperboard(
        db, start_utc=start_utc, end_utc=end_utc
    )
    packaging, packaging_issues = _scan_external_packaging(
        db, start_utc=start_utc, end_utc=end_utc
    )
    candidates = sorted(
        [*paperboard, *packaging],
        key=lambda row: (row.receipt_date, row.source_type, row.source_key),
    )
    return candidates, [*paperboard_issues, *packaging_issues], start, end


def _statement_number(
    db: Session,
    *,
    supplier_id: int,
    settlement_month: str,
    currency: str,
    tax_basis: str,
) -> str:
    count = int(
        db.scalar(
            select(func.count(SupplierMonthlyStatement.id)).where(
                SupplierMonthlyStatement.supplier_id == supplier_id,
                SupplierMonthlyStatement.settlement_month == settlement_month,
                SupplierMonthlyStatement.currency == currency,
                SupplierMonthlyStatement.tax_basis == tax_basis,
            )
        )
        or 0
    )
    suffix = "I" if tax_basis == "tax_inclusive" else "E"
    return (
        f"AP-{settlement_month.replace('-', '')}-{supplier_id:04d}-"
        f"{currency}-{suffix}-{count + 1:02d}"
    )


def _active_lines(db: Session, statement_id: int) -> list[SupplierMonthlyStatementLine]:
    return list(
        db.scalars(
            select(SupplierMonthlyStatementLine)
            .where(
                SupplierMonthlyStatementLine.statement_id == statement_id,
                SupplierMonthlyStatementLine.active_guard == 1,
            )
            .order_by(
                SupplierMonthlyStatementLine.receipt_date,
                SupplierMonthlyStatementLine.id,
            )
        ).all()
    )


def _recalculate_statement(db: Session, row: SupplierMonthlyStatement) -> bool:
    erp_amount = _money(
        sum((line.erp_amount for line in _active_lines(db, row.id)), Decimal("0"))
    )
    adjustment = _money(
        db.scalar(
            select(func.coalesce(func.sum(SupplierMonthlyAdjustment.amount), 0)).where(
                SupplierMonthlyAdjustment.statement_id == row.id
            )
        )
        or 0
    )
    adjusted = _money(erp_amount + adjustment)
    if adjusted < 0:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NEGATIVE_AMOUNT",
            "应付调整后金额不能小于 0",
            422,
        )
    status = row.status
    if status in ACTIVE_DRAFT_STATUSES:
        status = (
            "difference"
            if row.supplier_statement_amount is not None
            and _money(row.supplier_statement_amount) != adjusted
            else "draft"
        )
    before = (
        _money(row.erp_amount),
        _money(row.adjustment_amount),
        _money(row.adjusted_amount),
        row.status,
    )
    after = (erp_amount, adjustment, adjusted, status)
    if before == after:
        return False
    row.erp_amount = erp_amount
    row.adjustment_amount = adjustment
    row.adjusted_amount = adjusted
    row.status = status
    return True


def generate_or_refresh_settlements(
    db: Session,
    *,
    settlement_month: str,
    user: User,
    business_date: date | None = None,
) -> dict[str, Any]:
    candidates, issues, period_start, period_end = scan_settlement_candidates(
        db, settlement_month=settlement_month
    )
    if period_end >= (business_date or beijing_today()):
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_PERIOD_OPEN",
            f"{settlement_month} 月结周期要到 {period_end.isoformat()} 才能生成草稿",
            409,
        )
    valid_source_keys = {row.source_key for row in candidates}
    now = utc_now_naive()
    added = 0
    released = 0
    changed_statement_ids: set[int] = set()
    draft_statements = list(
        db.scalars(
            select(SupplierMonthlyStatement).where(
                SupplierMonthlyStatement.settlement_month == settlement_month,
                SupplierMonthlyStatement.active_guard == 1,
                SupplierMonthlyStatement.status.in_(ACTIVE_DRAFT_STATUSES),
            )
        ).all()
    )
    for statement in draft_statements:
        for line in _active_lines(db, statement.id):
            if line.source_key in valid_source_keys:
                continue
            line.active_guard = None
            line.released_at = now
            released += 1
            changed_statement_ids.add(statement.id)
    db.flush()

    active_source_keys = set(
        db.scalars(
            select(SupplierMonthlyStatementLine.source_key).where(
                SupplierMonthlyStatementLine.active_guard == 1
            )
        ).all()
    )
    statements_by_group = {
        (row.supplier_id, row.currency, row.tax_basis): row
        for row in db.scalars(
            select(SupplierMonthlyStatement).where(
                SupplierMonthlyStatement.settlement_month == settlement_month,
                SupplierMonthlyStatement.active_guard == 1,
            )
        ).all()
    }
    for candidate in candidates:
        if candidate.source_key in active_source_keys:
            continue
        group = (candidate.supplier_id, candidate.currency, candidate.tax_basis)
        statement = statements_by_group.get(group)
        if statement is not None and statement.status not in ACTIVE_DRAFT_STATUSES:
            issues.append(
                _issue(
                    source_type=candidate.source_type,
                    source_key=candidate.source_key,
                    receipt_number=candidate.receipt_number,
                    code="SUPPLIER_SETTLEMENT_ALREADY_CONFIRMED",
                    message="该周期已确认应付；请先受控重开后再刷新晚到实收",
                )
            )
            continue
        if statement is None:
            statement = SupplierMonthlyStatement(
                statement_number=_statement_number(
                    db,
                    supplier_id=candidate.supplier_id,
                    settlement_month=settlement_month,
                    currency=candidate.currency,
                    tax_basis=candidate.tax_basis,
                ),
                supplier_id=candidate.supplier_id,
                supplier_name_snapshot=candidate.supplier_name,
                settlement_month=settlement_month,
                period_start=period_start,
                period_end=period_end,
                currency=candidate.currency,
                tax_basis=candidate.tax_basis,
                generated_by=user.id,
            )
            db.add(statement)
            db.flush()
            statements_by_group[group] = statement
        db.add(
            SupplierMonthlyStatementLine(
                statement_id=statement.id,
                source_type=candidate.source_type,
                source_key=candidate.source_key,
                incoming_receipt_item_id=candidate.incoming_receipt_item_id,
                external_receipt_item_id=candidate.external_receipt_item_id,
                supplier_receipt_price_fact_id=(
                    candidate.supplier_receipt_price_fact_id
                ),
                purchase_document_number=candidate.purchase_document_number,
                receipt_number=candidate.receipt_number,
                receipt_date=candidate.receipt_date,
                category_label=candidate.category_label,
                specification_snapshot=candidate.specification_snapshot,
                material_or_product_snapshot=candidate.material_or_product_snapshot,
                received_quantity=candidate.received_quantity,
                quantity_unit=candidate.quantity_unit,
                frozen_unit_price=candidate.frozen_unit_price,
                price_unit=candidate.price_unit,
                currency=candidate.currency,
                tax_basis=candidate.tax_basis,
                tax_rate=candidate.tax_rate,
                erp_amount=candidate.erp_amount,
                tax_amount=candidate.tax_amount,
                source_link=candidate.source_link,
            )
        )
        added += 1
        active_source_keys.add(candidate.source_key)
        changed_statement_ids.add(statement.id)
    db.flush()

    for statement in statements_by_group.values():
        if statement.status not in ACTIVE_DRAFT_STATUSES:
            continue
        active_count = len(_active_lines(db, statement.id))
        if active_count == 0 and not statement.adjustments:
            statement.status = "voided"
            statement.active_guard = None
            statement.voided_at = now
            statement.voided_by = user.id
            changed_statement_ids.add(statement.id)
            continue
        if _recalculate_statement(db, statement):
            changed_statement_ids.add(statement.id)
    for statement_id in changed_statement_ids:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        if statement is not None:
            statement.version += 1
    db.flush()
    return {
        "settlement_month": settlement_month,
        "period_start": period_start,
        "period_end": period_end,
        "added_line_count": added,
        "released_line_count": released,
        "changed_statement_count": len(changed_statement_ids),
        "issues": issues,
        "items": list_statement_responses(db, settlement_month=settlement_month),
    }


def _line_response(row: SupplierMonthlyStatementLine) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_type": row.source_type,
        "source_key": row.source_key,
        "supplier_receipt_price_fact_id": row.supplier_receipt_price_fact_id,
        "price_fact_origin": (
            row.supplier_receipt_price_fact.fact_origin
            if row.supplier_receipt_price_fact is not None
            else "native_receipt_fact"
        ),
        "price_fact_adoption_reason": (
            row.supplier_receipt_price_fact.adoption_reason
            if row.supplier_receipt_price_fact is not None
            else None
        ),
        "purchase_document_number": row.purchase_document_number,
        "receipt_number": row.receipt_number,
        "receipt_date": row.receipt_date,
        "category_label": row.category_label,
        "specification_snapshot": row.specification_snapshot,
        "material_or_product_snapshot": row.material_or_product_snapshot,
        "received_quantity": row.received_quantity,
        "quantity_unit": row.quantity_unit,
        "frozen_unit_price": row.frozen_unit_price,
        "price_unit": row.price_unit,
        "currency": row.currency,
        "tax_basis": row.tax_basis,
        "tax_rate": row.tax_rate,
        "erp_amount": row.erp_amount,
        "tax_amount": row.tax_amount,
        "source_link": row.source_link,
    }


def _adjustment_response(row: SupplierMonthlyAdjustment) -> dict[str, Any]:
    return {
        "id": row.id,
        "statement_line_id": row.statement_line_id,
        "difference_type": row.difference_type,
        "amount": row.amount,
        "note": row.note,
        "created_at": row.created_at,
    }


def _invoice_response(row: SupplierMonthlyInvoice) -> dict[str, Any]:
    return {
        "id": row.id,
        "invoice_number": row.invoice_number,
        "invoice_date": row.invoice_date,
        "received_date": row.received_date,
        "invoice_total_amount": row.invoice_total_amount,
        "allocated_amount": row.allocated_amount,
        "tax_amount": row.tax_amount,
        "note": row.note,
        "attachment_original_name": row.attachment_original_name,
        "attachment_content_hash": row.attachment_content_hash,
        "attachment_size": row.attachment_size,
        "attachment_url": (
            f"/api/finance/supplier-settlements/{row.statement_id}/invoices/"
            f"{row.id}/attachment"
            if row.attachment_content_hash
            else None
        ),
        "created_at": row.created_at,
    }


def _payment_response(row: SupplierMonthlyPayment) -> dict[str, Any]:
    return {
        "id": row.id,
        "payment_date": row.payment_date,
        "amount": row.amount,
        "payment_method": row.payment_method,
        "payment_method_label": "承兑背书" if row.payment_method == "acceptance" else "银行付款",
        "acceptance_note_id": row.acceptance_note_id,
        "reference": row.reference,
        "created_at": row.created_at,
    }


def statement_response(db: Session, row: SupplierMonthlyStatement) -> dict[str, Any]:
    lines = _active_lines(db, row.id)
    adjustments = list(
        db.scalars(
            select(SupplierMonthlyAdjustment)
            .where(SupplierMonthlyAdjustment.statement_id == row.id)
            .order_by(SupplierMonthlyAdjustment.id)
        ).all()
    )
    invoices = list(
        db.scalars(
            select(SupplierMonthlyInvoice)
            .where(SupplierMonthlyInvoice.statement_id == row.id)
            .order_by(SupplierMonthlyInvoice.invoice_date, SupplierMonthlyInvoice.id)
        ).all()
    )
    payments = list(
        db.scalars(
            select(SupplierMonthlyPayment)
            .where(SupplierMonthlyPayment.statement_id == row.id)
            .order_by(SupplierMonthlyPayment.payment_date, SupplierMonthlyPayment.id)
        ).all()
    )
    adjusted = _money(row.adjusted_amount)
    supplier_amount = (
        _money(row.supplier_statement_amount)
        if row.supplier_statement_amount is not None
        else None
    )
    confirmed = (
        _money(row.confirmed_amount) if row.confirmed_amount is not None else None
    )
    invoiced = _money(sum((item.allocated_amount for item in invoices), Decimal("0")))
    paid = _money(sum((item.amount for item in payments), Decimal("0")))
    return {
        "id": row.id,
        "statement_number": row.statement_number,
        "supplier_id": row.supplier_id,
        "supplier_name": row.supplier_name_snapshot,
        "settlement_month": row.settlement_month,
        "period_start": row.period_start,
        "period_end": row.period_end,
        "currency": row.currency,
        "tax_basis": row.tax_basis,
        "status": row.status,
        "erp_amount": _money(row.erp_amount),
        "adjustment_amount": _money(row.adjustment_amount),
        "adjusted_amount": adjusted,
        "supplier_statement_number": row.supplier_statement_number,
        "supplier_statement_date": row.supplier_statement_date,
        "supplier_statement_amount": supplier_amount,
        "difference_amount": (
            _money(supplier_amount - adjusted) if supplier_amount is not None else None
        ),
        "confirmed_amount": confirmed,
        "invoice_allocated_amount": invoiced,
        "invoice_difference_amount": (
            _money(invoiced - confirmed) if confirmed is not None else None
        ),
        "paid_amount": paid,
        "remaining_payable_amount": (
            _money(max(confirmed - paid, Decimal("0")))
            if confirmed is not None
            else None
        ),
        "version": row.version,
        "can_reopen": row.status in CONFIRMED_STATUSES and paid == 0,
        "lines": [_line_response(item) for item in lines],
        "adjustments": [_adjustment_response(item) for item in adjustments],
        "invoices": [_invoice_response(item) for item in invoices],
        "payments": [_payment_response(item) for item in payments],
    }


def list_statement_responses(
    db: Session, *, settlement_month: str
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(SupplierMonthlyStatement)
        .where(
            SupplierMonthlyStatement.settlement_month == settlement_month,
            SupplierMonthlyStatement.active_guard == 1,
        )
        .order_by(
            SupplierMonthlyStatement.supplier_name_snapshot,
            SupplierMonthlyStatement.currency,
            SupplierMonthlyStatement.tax_basis,
        )
    ).all()
    return [statement_response(db, row) for row in rows]


def _editable_statement(
    db: Session, *, statement_id: int, expected_version: int
) -> SupplierMonthlyStatement:
    row = db.get(SupplierMonthlyStatement, statement_id)
    if row is None or row.active_guard != 1:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NOT_FOUND", "供应商月结单不存在", 404
        )
    if int(row.version) != int(expected_version):
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_STALE",
            "供应商月结单已被其他人修改，请刷新后重试",
            409,
        )
    return row


def review_statement(
    db: Session,
    *,
    statement_id: int,
    expected_version: int,
    supplier_statement_number: str,
    supplier_statement_date: date,
    supplier_statement_amount: Decimal,
    user: User,
) -> SupplierMonthlyStatement:
    row = _editable_statement(
        db, statement_id=statement_id, expected_version=expected_version
    )
    if row.status not in ACTIVE_DRAFT_STATUSES:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_FROZEN", "已确认月结单不能直接改写供应商账单"
        )
    amount = _money(supplier_statement_amount)
    if amount < 0:
        raise SupplierSettlementError(
            "SUPPLIER_STATEMENT_AMOUNT_INVALID", "供应商账单金额不能小于 0", 422
        )
    number = str(supplier_statement_number or "").strip()
    if not number:
        raise SupplierSettlementError(
            "SUPPLIER_STATEMENT_NUMBER_REQUIRED", "供应商对账单号不能为空", 422
        )
    erp_amount = _money(
        sum((line.erp_amount for line in _active_lines(db, row.id)), Decimal("0"))
    )
    adjustment_amount = _money(
        db.scalar(
            select(func.coalesce(func.sum(SupplierMonthlyAdjustment.amount), 0)).where(
                SupplierMonthlyAdjustment.statement_id == row.id
            )
        )
        or 0
    )
    adjusted_amount = _money(erp_amount + adjustment_amount)
    next_status = "difference" if amount != adjusted_amount else "draft"
    now = utc_now_naive()
    result = db.execute(
        update(SupplierMonthlyStatement)
        .where(
            SupplierMonthlyStatement.id == row.id,
            SupplierMonthlyStatement.active_guard == 1,
            SupplierMonthlyStatement.status.in_(ACTIVE_DRAFT_STATUSES),
            SupplierMonthlyStatement.version == expected_version,
        )
        .values(
            supplier_statement_number=number,
            supplier_statement_date=supplier_statement_date,
            supplier_statement_amount=amount,
            erp_amount=erp_amount,
            adjustment_amount=adjustment_amount,
            adjusted_amount=adjusted_amount,
            status=next_status,
            reviewed_by=user.id,
            reviewed_at=now,
            version=expected_version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_STALE",
            "供应商月结单已被其他人修改，请刷新后重试",
            409,
        )
    db.expire(row)
    db.refresh(row)
    return row


def add_adjustment(
    db: Session,
    *,
    statement_id: int,
    expected_version: int,
    statement_line_id: int | None,
    difference_type: str,
    amount: Decimal,
    note: str | None,
    user: User,
) -> tuple[SupplierMonthlyStatement, SupplierMonthlyAdjustment]:
    row = _editable_statement(
        db, statement_id=statement_id, expected_version=expected_version
    )
    if row.status not in ACTIVE_DRAFT_STATUSES:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_FROZEN", "已确认月结单不能直接增加差异调整"
        )
    normalized_type = str(difference_type or "").strip()
    if normalized_type not in {"price", "quantity", "tax_rounding", "other"}:
        raise SupplierSettlementError(
            "SUPPLIER_ADJUSTMENT_TYPE_INVALID", "差异类型无效", 422
        )
    normalized_amount = _money(amount)
    if normalized_amount == 0:
        raise SupplierSettlementError(
            "SUPPLIER_ADJUSTMENT_AMOUNT_INVALID", "差异调整金额不能为 0", 422
        )
    if statement_line_id is not None:
        line = db.get(SupplierMonthlyStatementLine, statement_line_id)
        if (
            line is None
            or line.statement_id != row.id
            or line.active_guard != 1
        ):
            raise SupplierSettlementError(
                "SUPPLIER_ADJUSTMENT_LINE_INVALID", "差异调整明细不属于当前月结单", 422
            )
    erp_amount = _money(
        sum((line.erp_amount for line in _active_lines(db, row.id)), Decimal("0"))
    )
    adjustment_amount = _money(
        (
            db.scalar(
                select(func.coalesce(func.sum(SupplierMonthlyAdjustment.amount), 0)).where(
                    SupplierMonthlyAdjustment.statement_id == row.id
                )
            )
            or 0
        )
        + normalized_amount
    )
    adjusted_amount = _money(erp_amount + adjustment_amount)
    if adjusted_amount < 0:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NEGATIVE_AMOUNT",
            "应付调整后金额不能小于 0",
            422,
        )
    next_status = (
        "difference"
        if row.supplier_statement_amount is not None
        and _money(row.supplier_statement_amount) != adjusted_amount
        else "draft"
    )
    now = utc_now_naive()
    result = db.execute(
        update(SupplierMonthlyStatement)
        .where(
            SupplierMonthlyStatement.id == row.id,
            SupplierMonthlyStatement.active_guard == 1,
            SupplierMonthlyStatement.status.in_(ACTIVE_DRAFT_STATUSES),
            SupplierMonthlyStatement.version == expected_version,
        )
        .values(
            erp_amount=erp_amount,
            adjustment_amount=adjustment_amount,
            adjusted_amount=adjusted_amount,
            status=next_status,
            version=expected_version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_STALE",
            "供应商月结单已被其他人修改，请刷新后重试",
            409,
        )
    adjustment = SupplierMonthlyAdjustment(
        statement_id=row.id,
        statement_line_id=statement_line_id,
        difference_type=normalized_type,
        amount=normalized_amount,
        note=str(note or "").strip() or None,
        created_by=user.id,
    )
    db.add(adjustment)
    db.flush()
    db.expire(row)
    db.refresh(row)
    return row, adjustment


def confirm_statement(
    db: Session,
    *,
    statement_id: int,
    expected_version: int,
    user: User,
) -> SupplierMonthlyStatement:
    row = _editable_statement(
        db, statement_id=statement_id, expected_version=expected_version
    )
    if row.status == "difference":
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_DIFFERENCE_UNRESOLVED",
            "供应商账单金额仍与调整后金额不一致，请先处理差异",
        )
    if row.status != "draft":
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NOT_CONFIRMABLE", "当前月结状态不能确认"
        )
    if row.supplier_statement_amount is None or row.supplier_statement_date is None:
        raise SupplierSettlementError(
            "SUPPLIER_STATEMENT_REVIEW_REQUIRED", "请先登记供应商账单日期、单号和金额"
        )
    if _money(row.adjusted_amount) <= 0:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_AMOUNT_EMPTY", "确认应付金额必须大于 0"
        )
    if _money(row.supplier_statement_amount) != _money(row.adjusted_amount):
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_DIFFERENCE_UNRESOLVED",
            "供应商账单金额仍与调整后金额不一致，请先处理差异",
        )
    now = utc_now_naive()
    payable = FinancePayable(
        supplier_id=row.supplier_id,
        counterparty_name=row.supplier_name_snapshot,
        category="material",
        document_number=row.supplier_statement_number,
        # The payable remains in the ERP settlement period.  The supplier's
        # actual statement date is preserved separately on the settlement.
        document_date=row.period_end,
        amount=_money(row.adjusted_amount),
        status="confirmed",
        note=f"供应商20日月结 {row.statement_number}",
        idempotency_key=f"supplier-monthly-confirm:{row.id}:v{row.version}",
        confirmed_by=user.id,
        confirmed_at=now,
        created_by=user.id,
    )
    db.add(payable)
    db.flush()
    next_status = (
        "invoiced_pending_payment"
        if _money(row.invoice_allocated_amount) > 0
        else "confirmed_pending_invoice"
    )
    result = db.execute(
        update(SupplierMonthlyStatement)
        .where(
            SupplierMonthlyStatement.id == row.id,
            SupplierMonthlyStatement.active_guard == 1,
            SupplierMonthlyStatement.status == "draft",
            SupplierMonthlyStatement.version == expected_version,
        )
        .values(
            finance_payable_id=payable.id,
            confirmed_amount=_money(row.adjusted_amount),
            confirmed_by=user.id,
            confirmed_at=now,
            status=next_status,
            version=expected_version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_STALE",
            "供应商月结单已被其他人修改，请刷新后重试",
            409,
        )
    db.expire(row)
    db.refresh(row)
    return row


def reopen_statement(
    db: Session,
    *,
    statement_id: int,
    expected_version: int,
    user: User,
) -> SupplierMonthlyStatement:
    row = _editable_statement(
        db, statement_id=statement_id, expected_version=expected_version
    )
    if row.status not in CONFIRMED_STATUSES or row.status == "paid":
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NOT_REOPENABLE", "当前月结状态不能重开"
        )
    paid = _money(
        db.scalar(
            select(func.coalesce(func.sum(SupplierMonthlyPayment.amount), 0)).where(
                SupplierMonthlyPayment.statement_id == row.id
            )
        )
        or 0
    )
    if paid > 0:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_PAYMENT_EXISTS",
            "该月结单已有付款事实，只能增加正负调整，不能重开",
        )
    payable = (
        db.get(FinancePayable, row.finance_payable_id)
        if row.finance_payable_id is not None
        else None
    )
    now = utc_now_naive()
    next_note = None
    if payable is not None and payable.status != "voided":
        next_note = "；".join(
            value for value in (payable.note, "供应商月结受控重开") if value
        )
    next_status = (
        "difference"
        if row.supplier_statement_amount is not None
        and _money(row.supplier_statement_amount) != _money(row.adjusted_amount)
        else "draft"
    )
    statement_result = db.execute(
        update(SupplierMonthlyStatement)
        .where(
            SupplierMonthlyStatement.id == row.id,
            SupplierMonthlyStatement.active_guard == 1,
            SupplierMonthlyStatement.status == row.status,
            SupplierMonthlyStatement.version == expected_version,
            SupplierMonthlyStatement.paid_amount == 0,
        )
        .values(
            finance_payable_id=None,
            confirmed_amount=None,
            reopened_by=user.id,
            reopened_at=now,
            status=next_status,
            version=expected_version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if statement_result.rowcount != 1:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_STALE",
            "供应商月结单付款状态已变化，不能重开，请刷新后重试",
            409,
        )
    if payable is not None and payable.status != "voided":
        payable_result = db.execute(
            update(FinancePayable)
            .where(
                FinancePayable.id == payable.id,
                FinancePayable.status == payable.status,
                FinancePayable.version == payable.version,
            )
            .values(
                status="voided",
                voided_by=user.id,
                voided_at=now,
                note=next_note,
                version=payable.version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if payable_result.rowcount != 1:
            raise SupplierSettlementError(
                "SUPPLIER_PAYABLE_STALE",
                "关联应付状态已变化，不能重开，请刷新后重试",
                409,
            )
    db.expire(row)
    db.refresh(row)
    return row


def add_invoice(
    db: Session,
    *,
    statement_id: int,
    expected_version: int,
    invoice_number: str,
    invoice_date: date,
    received_date: date,
    invoice_total_amount: Decimal,
    allocated_amount: Decimal,
    tax_amount: Decimal,
    note: str | None,
    user: User,
) -> tuple[SupplierMonthlyStatement, SupplierMonthlyInvoice]:
    row = _editable_statement(
        db, statement_id=statement_id, expected_version=expected_version
    )
    if row.status not in CONFIRMED_STATUSES:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NOT_CONFIRMED", "供应商月结确认后才能登记发票"
        )
    number = str(invoice_number or "").strip()
    if not number:
        raise SupplierSettlementError(
            "SUPPLIER_INVOICE_NUMBER_REQUIRED", "供应商发票号码不能为空", 422
        )
    total = _money(invoice_total_amount)
    allocated = _money(allocated_amount)
    tax = _money(tax_amount)
    if total <= 0 or allocated <= 0 or tax < 0 or tax > allocated:
        raise SupplierSettlementError(
            "SUPPLIER_INVOICE_AMOUNT_INVALID", "发票总额、分配金额或税额无效", 422
        )
    same_invoice = db.scalars(
        select(SupplierMonthlyInvoice).where(
            SupplierMonthlyInvoice.supplier_id == row.supplier_id,
            SupplierMonthlyInvoice.invoice_number == number,
        )
    ).all()
    for existing in same_invoice:
        if (
            existing.invoice_date != invoice_date
            or _money(existing.invoice_total_amount) != total
        ):
            raise SupplierSettlementError(
                "SUPPLIER_INVOICE_ALLOCATION_CONFLICT",
                "同一供应商发票跨期分配时，发票日期和总额必须保持一致",
            )
    existing_allocated = _money(
        sum((item.allocated_amount for item in same_invoice), Decimal("0"))
    )
    if existing_allocated + allocated > total:
        raise SupplierSettlementError(
            "SUPPLIER_INVOICE_OVER_ALLOCATED",
            "该发票跨期累计分配金额不能超过发票总额",
            422,
        )
    current_statement_invoiced = _money(
        db.scalar(
            select(func.coalesce(func.sum(SupplierMonthlyInvoice.allocated_amount), 0)).where(
                SupplierMonthlyInvoice.statement_id == row.id
            )
        )
        or 0
    )
    next_invoice_allocated = _money(current_statement_invoiced + allocated)
    next_status = (
        row.status
        if row.status in {"partial_payment", "paid"}
        else "invoiced_pending_payment"
    )
    statement_result = db.execute(
        update(SupplierMonthlyStatement)
        .where(
            SupplierMonthlyStatement.id == row.id,
            SupplierMonthlyStatement.active_guard == 1,
            SupplierMonthlyStatement.status == row.status,
            SupplierMonthlyStatement.version == expected_version,
        )
        .values(
            invoice_allocated_amount=next_invoice_allocated,
            status=next_status,
            version=expected_version + 1,
            updated_at=utc_now_naive(),
        )
        .execution_options(synchronize_session=False)
    )
    if statement_result.rowcount != 1:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_STALE",
            "供应商月结单发票或付款状态已变化，请刷新后重试",
            409,
        )
    invoice = SupplierMonthlyInvoice(
        statement_id=row.id,
        supplier_id=row.supplier_id,
        invoice_number=number,
        invoice_date=invoice_date,
        received_date=received_date,
        invoice_total_amount=total,
        allocated_amount=allocated,
        tax_amount=tax,
        note=str(note or "").strip() or None,
        created_by=user.id,
    )
    db.add(invoice)
    db.flush()
    db.expire(row)
    db.refresh(row)
    return row, invoice


def add_payment(
    db: Session,
    *,
    statement_id: int,
    expected_version: int,
    payment_date: date,
    amount: Decimal,
    reference: str | None,
    payment_method: str = "bank",
    acceptance_note_id: int | None = None,
    user: User,
) -> tuple[SupplierMonthlyStatement, SupplierMonthlyPayment]:
    row = _editable_statement(
        db, statement_id=statement_id, expected_version=expected_version
    )
    if row.status not in CONFIRMED_STATUSES or row.status == "paid":
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NOT_PAYABLE", "当前月结状态不能登记付款"
        )
    normalized_amount = _money(amount)
    if normalized_amount <= 0:
        raise SupplierSettlementError(
            "SUPPLIER_PAYMENT_AMOUNT_INVALID", "付款金额必须大于 0", 422
        )
    normalized_method = str(payment_method or "bank").strip()
    if normalized_method not in {"bank", "acceptance"}:
        raise SupplierSettlementError(
            "SUPPLIER_PAYMENT_METHOD_INVALID", "付款方式无效", 422
        )
    if (normalized_method == "acceptance") != (acceptance_note_id is not None):
        raise SupplierSettlementError(
            "SUPPLIER_PAYMENT_ACCEPTANCE_LINK_INVALID",
            "承兑背书付款必须关联承兑票据，银行付款不能关联承兑票据",
            422,
        )
    confirmed = _money(row.confirmed_amount)
    invoiced = _money(
        db.scalar(
            select(func.coalesce(func.sum(SupplierMonthlyInvoice.allocated_amount), 0)).where(
                SupplierMonthlyInvoice.statement_id == row.id
            )
        )
        or 0
    )
    paid = _money(
        db.scalar(
            select(func.coalesce(func.sum(SupplierMonthlyPayment.amount), 0)).where(
                SupplierMonthlyPayment.statement_id == row.id
            )
        )
        or 0
    )
    if invoiced <= 0:
        raise SupplierSettlementError(
            "SUPPLIER_INVOICE_REQUIRED", "请先登记已收到的供应商发票再登记付款"
        )
    if paid + normalized_amount > confirmed:
        raise SupplierSettlementError(
            "SUPPLIER_PAYMENT_EXCEEDS_PAYABLE", "累计付款不能超过确认应付金额", 422
        )
    if paid + normalized_amount > invoiced:
        raise SupplierSettlementError(
            "SUPPLIER_PAYMENT_EXCEEDS_INVOICE",
            "累计付款不能超过当前已登记发票分配金额",
            422,
        )
    new_paid = _money(paid + normalized_amount)
    next_status = "paid" if new_paid == confirmed else "partial_payment"
    statement_result = db.execute(
        update(SupplierMonthlyStatement)
        .where(
            SupplierMonthlyStatement.id == row.id,
            SupplierMonthlyStatement.active_guard == 1,
            SupplierMonthlyStatement.status == row.status,
            SupplierMonthlyStatement.version == expected_version,
        )
        .values(
            paid_amount=new_paid,
            status=next_status,
            version=expected_version + 1,
            updated_at=utc_now_naive(),
        )
        .execution_options(synchronize_session=False)
    )
    if statement_result.rowcount != 1:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_STALE",
            "供应商月结单付款状态已被其他人修改，请刷新后重试",
            409,
        )
    payable = (
        db.get(FinancePayable, row.finance_payable_id)
        if row.finance_payable_id is not None
        else None
    )
    if new_paid == confirmed:
        if payable is not None:
            payable_result = db.execute(
                update(FinancePayable)
                .where(
                    FinancePayable.id == payable.id,
                    FinancePayable.status == "confirmed",
                    FinancePayable.version == payable.version,
                )
                .values(
                    status="paid",
                    paid_by=user.id,
                    paid_at=utc_now_naive(),
                    version=payable.version + 1,
                    updated_at=utc_now_naive(),
                )
                .execution_options(synchronize_session=False)
            )
            if payable_result.rowcount != 1:
                raise SupplierSettlementError(
                    "SUPPLIER_PAYABLE_STALE",
                    "关联应付状态已变化，请刷新后重试",
                    409,
                )
    payment = SupplierMonthlyPayment(
        statement_id=row.id,
        payment_date=payment_date,
        amount=normalized_amount,
        payment_method=normalized_method,
        acceptance_note_id=acceptance_note_id,
        reference=str(reference or "").strip() or None,
        created_by=user.id,
    )
    db.add(payment)
    db.flush()
    db.expire(row)
    db.refresh(row)
    return row, payment


def assert_receipt_item_not_in_confirmed_statement(
    db: Session, incoming_receipt_item_id: int
) -> None:
    statement = db.scalar(
        select(SupplierMonthlyStatement)
        .join(
            SupplierMonthlyStatementLine,
            SupplierMonthlyStatementLine.statement_id == SupplierMonthlyStatement.id,
        )
        .where(
            SupplierMonthlyStatementLine.incoming_receipt_item_id
            == incoming_receipt_item_id,
            SupplierMonthlyStatementLine.active_guard == 1,
            SupplierMonthlyStatement.status.in_(CONFIRMED_STATUSES),
        )
        .limit(1)
    )
    if statement is not None:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_RECEIPT_FROZEN",
            f"该实收已进入供应商月结 {statement.statement_number}，请先受控重开月结单",
            409,
        )


def partial_paid_amounts_by_payable(
    db: Session, payable_ids: list[int]
) -> dict[int, Decimal]:
    if not payable_ids:
        return {}
    rows = db.execute(
        select(
            SupplierMonthlyStatement.finance_payable_id,
            SupplierMonthlyStatement.paid_amount,
        ).where(
            SupplierMonthlyStatement.finance_payable_id.in_(payable_ids),
            SupplierMonthlyStatement.active_guard == 1,
        )
    ).all()
    return {
        int(payable_id): _money(paid_amount)
        for payable_id, paid_amount in rows
        if payable_id is not None
    }
