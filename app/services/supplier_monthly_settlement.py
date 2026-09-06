from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import OperationalError
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
from app.models.finance_simplified import FinanceAcceptanceNote
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
    SupplierCreditLot,
    SupplierPaymentBatch,
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
    def __init__(self, code: str, message: str, status_code: int = 409,
                 *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


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


def _effective_cutoff(settlement_month: str, settlement_day: int) -> date:
    if not 1 <= int(settlement_day) <= 31:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_DAY_INVALID", "供应商结算日必须在 1 至 31 日之间", 422
        )
    month_start = date.fromisoformat(f"{_shift_month(settlement_month, 0)}-01")
    last_day = calendar.monthrange(month_start.year, month_start.month)[1]
    return date(month_start.year, month_start.month, min(int(settlement_day), last_day))


def settlement_period(
    settlement_month: str, settlement_day: int = 20
) -> tuple[date, date]:
    current = date.fromisoformat(f"{_shift_month(settlement_month, 0)}-01")
    previous_month = _shift_month(settlement_month, -1)
    previous_cutoff = _effective_cutoff(previous_month, settlement_day)
    return previous_cutoff + timedelta(days=1), _effective_cutoff(
        current.strftime("%Y-%m"), settlement_day
    )


def default_closed_settlement_month(
    today: date | None = None, settlement_day: int = 20
) -> str:
    business_date = today or beijing_today()
    current = business_date.strftime("%Y-%m")
    current_cutoff = _effective_cutoff(current, settlement_day)
    return current if business_date > current_cutoff else _shift_month(current, -1)


def _utc_period_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    start_utc, _ = beijing_date_bounds_utc_naive(start)
    _, end_utc = beijing_date_bounds_utc_naive(end)
    return start_utc, end_utc


def settlement_period_utc_bounds(
    settlement_month: str, settlement_day: int = 20
) -> tuple[date, date, datetime, datetime]:
    start, end = settlement_period(settlement_month, settlement_day)
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
    supplier_id: int | None = None,
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
    if supplier_id is not None:
        payload["supplier_id"] = supplier_id
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
                quantity_overridden = bool(
                    receipt_price_fact.finance_only_test_classification
                )
                if (
                    (
                        not quantity_overridden
                        and Decimal(receipt_price_fact.received_quantity_snapshot)
                        != Decimal(int(item.received_quantity))
                    )
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
                    supplier_id=(
                        int(receipt_price_fact.supplier_id)
                        if receipt_price_fact is not None else None
                    ),
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
                category_label=(
                    "瓦楞纸板（历史测试归类）"
                    if receipt_price_fact is not None
                    and receipt_price_fact.finance_only_test_classification
                    else "瓦楞纸板"
                ),
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
                    supplier_id=int(purchase.supplier_id),
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
    db: Session, *, settlement_month: str, settlement_day: int = 20
) -> tuple[list[SettlementCandidate], list[dict[str, Any]], date, date]:
    start, end, start_utc, end_utc = settlement_period_utc_bounds(
        settlement_month, settlement_day
    )
    candidates, issues = _scan_candidates_for_bounds(
        db, start_utc=start_utc, end_utc=end_utc
    )
    return candidates, issues, start, end


def _scan_candidates_for_bounds(
    db: Session, *, start_utc: datetime, end_utc: datetime
) -> tuple[list[SettlementCandidate], list[dict[str, Any]]]:
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
    return candidates, [*paperboard_issues, *packaging_issues]


def supplier_settlement_period(
    db: Session,
    *,
    supplier_id: int,
    settlement_month: str,
    settlement_day: int,
) -> tuple[date, date]:
    """Return a gap-free supplier period even after its cutoff day changes."""

    default_start, period_end = settlement_period(settlement_month, settlement_day)
    previous_period_end = db.scalar(
        select(func.max(SupplierMonthlyStatement.period_end)).where(
            SupplierMonthlyStatement.supplier_id == supplier_id,
            SupplierMonthlyStatement.settlement_month != settlement_month,
            SupplierMonthlyStatement.period_end < period_end,
            SupplierMonthlyStatement.status != "voided",
        )
    )
    period_start = (
        previous_period_end + timedelta(days=1)
        if previous_period_end is not None
        else default_start
    )
    if period_start > period_end:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_PERIOD_OVERLAP",
            "该供应商结算日变更后与既有周期重叠，请先核对上一期月结",
        )
    return period_start, period_end


def _candidate_source_hash(candidates: list[SettlementCandidate]) -> str:
    payload = [
        {
            "source_key": item.source_key,
            "receipt_date": item.receipt_date.isoformat(),
            "quantity": _plain_decimal(item.received_quantity),
            "unit_price": _plain_decimal(item.frozen_unit_price),
            "amount": _plain_decimal(item.erp_amount),
            "tax_amount": _plain_decimal(item.tax_amount),
        }
        for item in sorted(candidates, key=lambda value: value.source_key)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _statement_line_source_hash(
    lines: list[SupplierMonthlyStatementLine],
) -> str:
    payload = [
        {
            "source_key": item.source_key,
            "receipt_date": item.receipt_date.isoformat(),
            "quantity": _plain_decimal(item.received_quantity),
            "unit_price": _plain_decimal(item.frozen_unit_price),
            "amount": _plain_decimal(item.erp_amount),
            "tax_amount": _plain_decimal(item.tax_amount),
        }
        for item in sorted(lines, key=lambda value: value.source_key)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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


def _add_candidate_line(
    db: Session,
    *,
    statement_id: int,
    candidate: SettlementCandidate,
) -> None:
    db.add(
        SupplierMonthlyStatementLine(
            statement_id=statement_id,
            source_type=candidate.source_type,
            source_key=candidate.source_key,
            incoming_receipt_item_id=candidate.incoming_receipt_item_id,
            external_receipt_item_id=candidate.external_receipt_item_id,
            supplier_receipt_price_fact_id=candidate.supplier_receipt_price_fact_id,
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


def _create_statement_from_candidates(
    db: Session,
    *,
    supplier: Supplier,
    settlement_month: str,
    period_start: date,
    period_end: date,
    candidates: list[SettlementCandidate],
    user: User,
    generation_origin: str,
    document_revision: int = 1,
    supersedes_statement_id: int | None = None,
    supersede_reason: str | None = None,
) -> SupplierMonthlyStatement:
    first = candidates[0]
    row = SupplierMonthlyStatement(
        statement_number=_statement_number(
            db,
            supplier_id=supplier.id,
            settlement_month=settlement_month,
            currency=first.currency,
            tax_basis=first.tax_basis,
        ),
        supplier_id=supplier.id,
        supplier_name_snapshot=first.supplier_name,
        settlement_month=settlement_month,
        period_start=period_start,
        period_end=period_end,
        currency=first.currency,
        tax_basis=first.tax_basis,
        document_revision=document_revision,
        settlement_day_snapshot=int(supplier.settlement_day or 20),
        generation_origin=generation_origin,
        source_hash=_candidate_source_hash(candidates),
        supersedes_statement_id=supersedes_statement_id,
        supersede_reason=supersede_reason,
        generated_by=user.id,
    )
    db.add(row)
    db.flush()
    for candidate in candidates:
        _add_candidate_line(db, statement_id=row.id, candidate=candidate)
    db.flush()
    _recalculate_statement(db, row)
    return row


def _replace_draft_statement(
    db: Session,
    *,
    row: SupplierMonthlyStatement,
    supplier: Supplier,
    candidates: list[SettlementCandidate],
    user: User,
    reason: str,
    period_start: date | None = None,
    period_end: date | None = None,
) -> tuple[SupplierMonthlyStatement, int]:
    if row.status not in ACTIVE_DRAFT_STATUSES or row.active_guard != 1:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_REGENERATION_BLOCKED",
            "只有未确认的当前草稿可以重生成；已确认、已开票或已付款记录必须保留",
        )
    if row.invoices or row.payments or row.finance_payable_id is not None:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_REGENERATION_DOWNSTREAM_EXISTS",
            "该月结已有应付、发票或付款事实，不能重生成",
        )
    if not candidates:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_REGENERATION_EMPTY",
            "当前周期没有可生成的有效实收，原草稿保持不变",
        )
    expected_version = int(row.version)
    statement_id = int(row.id)
    settlement_month = row.settlement_month
    original_period_start = row.period_start
    original_period_end = row.period_end
    next_document_revision = int(row.document_revision or 1) + 1
    now = utc_now_naive()
    statement_result = db.execute(
        update(SupplierMonthlyStatement)
        .where(
            SupplierMonthlyStatement.id == statement_id,
            SupplierMonthlyStatement.active_guard == 1,
            SupplierMonthlyStatement.status.in_(ACTIVE_DRAFT_STATUSES),
            SupplierMonthlyStatement.version == expected_version,
        )
        .values(
            active_guard=None,
            status="voided",
            voided_by=user.id,
            voided_at=now,
            version=expected_version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if statement_result.rowcount != 1:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_STALE",
            "供应商月结草稿已被其他操作修改，请刷新后重试",
            409,
        )
    released_result = db.execute(
        update(SupplierMonthlyStatementLine)
        .where(
            SupplierMonthlyStatementLine.statement_id == statement_id,
            SupplierMonthlyStatementLine.active_guard == 1,
        )
        .values(active_guard=None, released_at=now)
        .execution_options(synchronize_session=False)
    )
    released = int(released_result.rowcount or 0)
    db.expire(row)
    db.flush()
    replacement = _create_statement_from_candidates(
        db,
        supplier=supplier,
        settlement_month=settlement_month,
        period_start=period_start or original_period_start,
        period_end=period_end or original_period_end,
        candidates=candidates,
        user=user,
        generation_origin="regenerate",
        document_revision=next_document_revision,
        supersedes_statement_id=statement_id,
        supersede_reason=reason,
    )
    return replacement, released


def _supplier_scan_issues(
    db: Session, *, supplier: Supplier, issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Scope missing facts by stable supplier identity, never fuzzy name matching.

    An unresolvable owner cannot safely be assumed to belong to another supplier;
    leave that issue blocking until its source identity can be established.
    """
    selected = []
    for issue in issues:
        owner_id = issue.get("supplier_id")
        if owner_id is not None and db.get(Supplier, owner_id) is not None:
            if owner_id == supplier.id:
                selected.append(issue)
            continue
        try:
            owner = resolve_supplier(db, issue.get("supplier_name"), require_active=False)
        except SupplierLookupError:
            selected.append(issue)
        else:
            if owner.id == supplier.id:
                selected.append(issue)
    return selected


def _blocked_period(
    *, supplier: Supplier, settlement_month: str, start: date, end: date,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    receipt_numbers = list(dict.fromkeys(str(row.get("receipt_number") or "") for row in issues))
    first_receipt = next((value for value in receipt_numbers if value), "来源待核对")
    return {
        "status": "blocked", "code": "SUPPLIER_SETTLEMENT_INCOMPLETE",
        "supplier_id": supplier.id, "supplier_name": supplier.standard_name,
        "settlement_month": settlement_month, "period_start": start, "period_end": end,
        "issue_count": len(issues),
        "source_keys": list(dict.fromkeys(str(row.get("source_key") or "") for row in issues)),
        "receipt_numbers": receipt_numbers,
        "message": (
            f"{supplier.standard_name} {start.isoformat()} 至 {end.isoformat()} "
            f"有 {len(issues)} 条实收价格或来源待处理（{first_receipt}）；"
            "该账期已停止生成或确认，请处理后重生成"
        ),
    }


def settlement_completeness_summary(blocked_periods: list[dict[str, Any]]) -> dict[str, Any]:
    issue_count = len({key for row in blocked_periods for key in row["source_keys"]})
    return {
        "status": "blocked" if blocked_periods else "complete",
        "blocked_supplier_count": len({row["supplier_id"] for row in blocked_periods}),
        "issue_count": issue_count,
        "message": (
            f"有 {len(blocked_periods)} 个供应商账期存在 {issue_count} 条实收缺口，已停止生成；请处理后重试"
            if blocked_periods else "本次检查账期的实收价格和来源完整"
        ),
    }


def _require_complete_period(
    db: Session, *, supplier: Supplier, settlement_month: str, start: date, end: date,
    issues: list[dict[str, Any]],
) -> None:
    blocking = _supplier_scan_issues(db, supplier=supplier, issues=issues)
    if blocking:
        summary = _blocked_period(supplier=supplier, settlement_month=settlement_month,
                                  start=start, end=end, issues=blocking)
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_INCOMPLETE", summary["message"],
            details={"completeness": summary, "issues": blocking},
        )


def _supplier_candidates(
    db: Session,
    *,
    supplier: Supplier,
    settlement_month: str,
) -> tuple[list[SettlementCandidate], list[dict[str, Any]], date, date]:
    start, end = supplier_settlement_period(
        db,
        supplier_id=supplier.id,
        settlement_month=settlement_month,
        settlement_day=int(supplier.settlement_day or 20),
    )
    start_utc, end_utc = _utc_period_bounds(start, end)
    candidates, issues = _scan_candidates_for_bounds(
        db, start_utc=start_utc, end_utc=end_utc
    )
    return (
        [item for item in candidates if item.supplier_id == supplier.id],
        _supplier_scan_issues(db, supplier=supplier, issues=issues),
        start,
        end,
    )


def supplier_settlement_overview(
    db: Session, *, settlement_month: str
) -> tuple[list[SettlementCandidate], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[SettlementCandidate] = []
    issues: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    issue_keys: set[tuple[str, str]] = set()
    suppliers = list(
        db.scalars(select(Supplier).order_by(Supplier.sort_order, Supplier.id)).all()
    )
    for supplier in suppliers:
        supplier_candidates, supplier_issues, start, end = _supplier_candidates(
            db, supplier=supplier, settlement_month=settlement_month
        )
        candidates.extend(supplier_candidates)
        periods.append(
            {
                "supplier_id": supplier.id,
                "supplier_name": supplier.standard_name,
                "settlement_day": int(supplier.settlement_day or 20),
                "period_start": start,
                "period_end": end,
                "completeness": (
                    _blocked_period(supplier=supplier, settlement_month=settlement_month,
                                    start=start, end=end, issues=supplier_issues)
                    if supplier_issues else {"status": "complete"}
                ),
            }
        )
        for issue in supplier_issues:
            key = (str(issue.get("code") or ""), str(issue.get("source_key") or ""))
            if key not in issue_keys:
                issue_keys.add(key)
                issues.append(issue)
    return (
        sorted(candidates, key=lambda item: (item.receipt_date, item.source_key)),
        issues,
        periods,
    )


def _begin_settlement_write_snapshot(db: Session) -> None:
    # pysqlite's legacy SELECT starts only a SQLAlchemy virtual transaction.
    # Reserve the SQLite writer before scanning so new receipts cannot commit
    # between the source snapshot and the draft/confirmed payable write.
    connection = db.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        except OperationalError as error:
            sqlite_code = getattr(error.orig, "sqlite_errorcode", 0)
            if sqlite_code & 0xFF in {5, 6}:
                raise SupplierSettlementError(
                    "SUPPLIER_SETTLEMENT_CONCURRENT_WRITE",
                    "实收或财务数据正在更新，请稍后重试月结操作",
                ) from error
            raise


def generate_or_refresh_settlements(
    db: Session,
    *,
    settlement_month: str,
    user: User,
    business_date: date | None = None,
    generation_origin: str = "manual",
    replace_changed_drafts: bool = True,
    supplier_ids: set[int] | None = None,
) -> dict[str, Any]:
    _begin_settlement_write_snapshot(db)
    business_today = business_date or beijing_today()
    if generation_origin not in {"automatic", "manual"}:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_ORIGIN_INVALID", "月结草稿生成来源无效", 422
        )
    suppliers = list(
        db.scalars(
            select(Supplier)
            .order_by(Supplier.sort_order, Supplier.id)
        ).all()
    )
    if supplier_ids is not None:
        suppliers = [item for item in suppliers if item.id in supplier_ids]
    issues: list[dict[str, Any]] = []
    issue_keys: set[tuple[str, str]] = set()
    added = 0
    released = 0
    changed_statement_ids: set[int] = set()
    supplier_periods: list[dict[str, Any]] = []
    blocked_periods: list[dict[str, Any]] = []
    any_closed_period = False

    for supplier in suppliers:
        candidates, scan_issues, period_start, period_end = _supplier_candidates(
            db, supplier=supplier, settlement_month=settlement_month
        )
        supplier_periods.append(
            {
                "supplier_id": supplier.id,
                "supplier_name": supplier.standard_name,
                "settlement_day": int(supplier.settlement_day or 20),
                "period_start": period_start,
                "period_end": period_end,
                "closed": period_end < business_today,
            }
        )
        if period_end >= business_today:
            continue
        any_closed_period = True
        for issue in scan_issues:
            key = (str(issue.get("code") or ""), str(issue.get("source_key") or ""))
            if key not in issue_keys:
                issue_keys.add(key)
                issues.append(issue)

        if scan_issues:
            blocked_periods.append(_blocked_period(
                supplier=supplier, settlement_month=settlement_month,
                start=period_start, end=period_end, issues=scan_issues,
            ))
            continue

        groups: dict[tuple[str, str], list[SettlementCandidate]] = {}
        for candidate in candidates:
            groups.setdefault((candidate.currency, candidate.tax_basis), []).append(
                candidate
            )
        for (currency, tax_basis), group_candidates in groups.items():
            existing = db.scalar(
                select(SupplierMonthlyStatement).where(
                    SupplierMonthlyStatement.supplier_id == supplier.id,
                    SupplierMonthlyStatement.settlement_month == settlement_month,
                    SupplierMonthlyStatement.currency == currency,
                    SupplierMonthlyStatement.tax_basis == tax_basis,
                    SupplierMonthlyStatement.active_guard == 1,
                )
            )
            desired_hash = _candidate_source_hash(group_candidates)
            if existing is None:
                created = _create_statement_from_candidates(
                    db,
                    supplier=supplier,
                    settlement_month=settlement_month,
                    period_start=period_start,
                    period_end=period_end,
                    candidates=group_candidates,
                    user=user,
                    generation_origin=generation_origin,
                )
                added += len(group_candidates)
                changed_statement_ids.add(created.id)
                continue
            metadata_matches = (
                existing.period_start == period_start
                and existing.period_end == period_end
                and int(existing.settlement_day_snapshot or 20)
                == int(supplier.settlement_day or 20)
            )
            if existing.source_hash == desired_hash and metadata_matches:
                continue
            existing_lines = _active_lines(db, existing.id)
            if (
                existing.source_hash is None
                and _statement_line_source_hash(existing_lines) == desired_hash
                and metadata_matches
            ):
                existing.source_hash = desired_hash
                continue
            if existing.status not in ACTIVE_DRAFT_STATUSES:
                issues.append(
                    _issue(
                        source_type="supplier_statement",
                        source_key=str(existing.id),
                        receipt_number=existing.statement_number,
                        supplier_name=supplier.standard_name,
                        code="SUPPLIER_SETTLEMENT_ALREADY_CONFIRMED",
                        message="该周期已确认应付；新增或变化的实收不会静默改写历史",
                        recommended_action="登记受控调整，不能重生成已确认月结",
                    )
                )
                continue
            if not replace_changed_drafts:
                issues.append(
                    _issue(
                        source_type="supplier_statement",
                        source_key=str(existing.id),
                        receipt_number=existing.statement_number,
                        supplier_name=supplier.standard_name,
                        code="SUPPLIER_SETTLEMENT_REGENERATION_REQUIRED",
                        message="草稿来源已变化，请人工核对后重生成新业务修订",
                        recommended_action="点击重生成，旧草稿和明细将完整保留",
                    )
                )
                continue
            replacement, released_count = _replace_draft_statement(
                db,
                row=existing,
                supplier=supplier,
                candidates=group_candidates,
                user=user,
                reason="人工重新生成：结算周期或实收来源发生变化",
                period_start=period_start,
                period_end=period_end,
            )
            added += len(group_candidates)
            released += released_count
            changed_statement_ids.add(replacement.id)
    if not any_closed_period:
        _start, period_end = settlement_period(settlement_month)
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_PERIOD_OPEN",
            f"{settlement_month} 月结周期尚未完整结束（默认截止 {period_end.isoformat()}）",
            409,
        )
    db.flush()
    period_start, period_end = settlement_period(settlement_month)
    items = list_statement_responses(db, settlement_month=settlement_month)
    blocked_by_supplier = {row["supplier_id"]: row for row in blocked_periods}
    for item in items:
        if item["status"] in ACTIVE_DRAFT_STATUSES and item["supplier_id"] in blocked_by_supplier:
            item["completeness"] = blocked_by_supplier[item["supplier_id"]]
    return {
        "settlement_month": settlement_month,
        "period_start": period_start,
        "period_end": period_end,
        "supplier_periods": supplier_periods,
        "added_line_count": added,
        "released_line_count": released,
        "changed_statement_count": len(changed_statement_ids),
        "blocked_periods": blocked_periods,
        "completeness": settlement_completeness_summary(blocked_periods),
        "issues": issues,
        "items": items,
    }


def regenerate_statement(
    db: Session,
    *,
    statement_id: int,
    expected_version: int,
    reason: str | None,
    user: User,
) -> SupplierMonthlyStatement:
    _begin_settlement_write_snapshot(db)
    row = _editable_statement(
        db, statement_id=statement_id, expected_version=expected_version
    )
    supplier = db.get(Supplier, row.supplier_id)
    if supplier is None:
        raise SupplierSettlementError(
            "SUPPLIER_NOT_FOUND", "供应商主数据不存在，不能重生成月结", 404
        )
    period_start, period_end = supplier_settlement_period(
        db,
        supplier_id=supplier.id,
        settlement_month=row.settlement_month,
        settlement_day=int(supplier.settlement_day or 20),
    )
    start_utc, end_utc = _utc_period_bounds(period_start, period_end)
    candidates, issues = _scan_candidates_for_bounds(
        db, start_utc=start_utc, end_utc=end_utc
    )
    _require_complete_period(db, supplier=supplier, settlement_month=row.settlement_month,
                             start=period_start, end=period_end, issues=issues)
    selected = [
        item
        for item in candidates
        if item.supplier_id == row.supplier_id
        and item.currency == row.currency
        and item.tax_basis == row.tax_basis
    ]
    desired_hash = _candidate_source_hash(selected) if selected else None
    if (
        desired_hash == row.source_hash
        and row.period_start == period_start
        and row.period_end == period_end
        and int(row.settlement_day_snapshot or 20) == int(supplier.settlement_day or 20)
    ):
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_REGENERATION_NO_CHANGE",
            "周期内实收来源没有变化，无需重生成",
            409,
        )
    replacement, _released = _replace_draft_statement(
        db,
        row=row,
        supplier=supplier,
        candidates=selected,
        user=user,
        reason=str(reason or "").strip() or "人工核对后重生成",
        period_start=period_start,
        period_end=period_end,
    )
    db.flush()
    return replacement


def generate_due_supplier_settlements(
    db: Session, *, user: User, business_date: date | None = None
) -> dict[str, Any]:
    today = business_date or beijing_today()
    suppliers = list(db.scalars(select(Supplier)).all())
    grouped: dict[str, set[int]] = {}
    for supplier in suppliers:
        month = default_closed_settlement_month(today, int(supplier.settlement_day or 20))
        grouped.setdefault(month, set()).add(supplier.id)
    results = []
    for month, ids in sorted(grouped.items()):
        results.append(
            generate_or_refresh_settlements(
                db,
                settlement_month=month,
                user=user,
                business_date=today,
                generation_origin="automatic",
                replace_changed_drafts=False,
                supplier_ids=ids,
            )
        )
    blocked_periods = [period for result in results for period in result["blocked_periods"]]
    issues = {
        (issue.get("source_key"), issue.get("code")): issue
        for result in results for issue in result["issues"]
    }
    return {
        "business_date": today,
        "generated_months": sorted(grouped),
        "results": results,
        "blocked_periods": blocked_periods,
        "completeness": settlement_completeness_summary(blocked_periods),
        "issues": list(issues.values()),
        "changed_statement_count": sum(
            int(item["changed_statement_count"]) for item in results
        ),
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
        "statement_version_before": row.statement_version_before,
        "amount_before": row.amount_before,
        "amount_after": row.amount_after,
        "is_post_confirmation": row.is_post_confirmation,
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
    labels = {"bank": "银行付款", "acceptance": "承兑背书", "credit": "供应商余额抵扣"}
    return {
        "id": row.id,
        "payment_date": row.payment_date,
        "amount": row.amount,
        "payment_method": row.payment_method,
        "payment_method_label": labels.get(row.payment_method, row.payment_method),
        "acceptance_note_id": row.acceptance_note_id,
        "payment_batch_id": row.payment_batch_id,
        "supplier_credit_id": row.supplier_credit_id,
        "reference": row.reference,
        "created_at": row.created_at,
    }


def _payment_batch_response(row: SupplierPaymentBatch) -> dict[str, Any]:
    return {
        "id": row.id,
        "payment_date": row.payment_date,
        "credit_applied_amount": row.credit_applied_amount,
        "acceptance_note_id": row.acceptance_note_id,
        "acceptance_face_amount": row.acceptance_face_amount,
        "acceptance_applied_amount": row.acceptance_applied_amount,
        "bank_amount": row.bank_amount,
        "credit_created_amount": row.credit_created_amount,
        "settled_amount": row.settled_amount,
        "bank_reference": row.bank_reference,
        "created_at": row.created_at,
    }


def _credit_response(row: SupplierCreditLot) -> dict[str, Any]:
    return {
        "id": row.id,
        "supplier_id": row.supplier_id,
        "source_type": row.source_type,
        "source_acceptance_note_id": row.source_acceptance_note_id,
        "source_statement_id": row.source_statement_id,
        "source_payment_batch_id": row.source_payment_batch_id,
        "original_amount": row.original_amount,
        "available_amount": row.available_amount,
        "status": row.status,
        "version": row.version,
        "created_at": row.created_at,
    }


def statement_response(db: Session, row: SupplierMonthlyStatement) -> dict[str, Any]:
    lines = (
        _active_lines(db, row.id)
        if row.active_guard == 1
        else list(
            db.scalars(
                select(SupplierMonthlyStatementLine)
                .where(SupplierMonthlyStatementLine.statement_id == row.id)
                .order_by(SupplierMonthlyStatementLine.receipt_date, SupplierMonthlyStatementLine.id)
            ).all()
        )
    )
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
    payment_batches = list(
        db.scalars(
            select(SupplierPaymentBatch)
            .where(SupplierPaymentBatch.statement_id == row.id)
            .order_by(SupplierPaymentBatch.payment_date, SupplierPaymentBatch.id)
        ).all()
    )
    available_credits = list(
        db.scalars(
            select(SupplierCreditLot)
            .where(
                SupplierCreditLot.supplier_id == row.supplier_id,
                SupplierCreditLot.status.in_({"available", "partial"}),
                SupplierCreditLot.available_amount > 0,
            )
            .order_by(SupplierCreditLot.id)
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
    # paid_amount is the current allocation after any post-confirmation
    # adjustment; immutable payment rows remain the cash/acceptance history.
    paid = _money(row.paid_amount)
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
        "active": row.active_guard == 1,
        "document_revision": row.document_revision,
        "settlement_day_snapshot": row.settlement_day_snapshot,
        "generation_origin": row.generation_origin,
        "source_hash": row.source_hash,
        "supersedes_statement_id": row.supersedes_statement_id,
        "supersede_reason": row.supersede_reason,
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
        "can_regenerate": row.active_guard == 1 and row.status in ACTIVE_DRAFT_STATUSES,
        "can_post_adjustment": row.active_guard == 1 and row.status in CONFIRMED_STATUSES,
        "can_reopen": (
            row.status in CONFIRMED_STATUSES
            and paid == 0
            and not invoices
        ),
        "available_credits": [_credit_response(item) for item in available_credits],
        "credit_available_amount": _money(
            sum((item.available_amount for item in available_credits), Decimal("0"))
        ),
        "lines": [_line_response(item) for item in lines],
        "adjustments": [_adjustment_response(item) for item in adjustments],
        "invoices": [_invoice_response(item) for item in invoices],
        "payments": [_payment_response(item) for item in payments],
        "payment_batches": [_payment_batch_response(item) for item in payment_batches],
    }


def list_statement_responses(
    db: Session, *, settlement_month: str,
    supplier_periods: list[dict[str, Any]] | None = None,
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
    items = [statement_response(db, row) for row in rows]
    completeness_by_supplier = {
        period["supplier_id"]: period["completeness"] for period in (supplier_periods or [])
        if period.get("completeness", {}).get("status") == "blocked"
    }
    for item in items:
        if item["status"] in ACTIVE_DRAFT_STATUSES and item["supplier_id"] in completeness_by_supplier:
            item["completeness"] = completeness_by_supplier[item["supplier_id"]]
    return items


def list_statement_history_responses(
    db: Session, *, statement_id: int
) -> list[dict[str, Any]]:
    current = db.get(SupplierMonthlyStatement, statement_id)
    if current is None:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NOT_FOUND", "供应商月结单不存在", 404
        )
    rows = db.scalars(
        select(SupplierMonthlyStatement)
        .where(
            SupplierMonthlyStatement.supplier_id == current.supplier_id,
            SupplierMonthlyStatement.settlement_month == current.settlement_month,
            SupplierMonthlyStatement.currency == current.currency,
            SupplierMonthlyStatement.tax_basis == current.tax_basis,
        )
        .order_by(SupplierMonthlyStatement.document_revision.desc(), SupplierMonthlyStatement.id.desc())
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
    if row.status not in ACTIVE_DRAFT_STATUSES | CONFIRMED_STATUSES:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_FROZEN", "当前月结状态不能增加差异调整"
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
    is_post_confirmation = row.status in CONFIRMED_STATUSES
    erp_amount = _money(row.erp_amount)
    adjustment_amount = _money(row.adjustment_amount + normalized_amount)
    amount_before = _money(
        row.confirmed_amount if is_post_confirmation else row.adjusted_amount
    )
    amount_after = _money(amount_before + normalized_amount)
    if amount_after < 0 or (is_post_confirmation and amount_after == 0):
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NEGATIVE_AMOUNT",
            "应付调整后金额必须大于 0",
            422,
        )
    now = utc_now_naive()
    next_values: dict[str, Any] = {
        "adjustment_amount": adjustment_amount,
        "adjusted_amount": amount_after,
        "version": expected_version + 1,
        "updated_at": now,
    }
    credit_created = Decimal("0")
    if is_post_confirmation:
        paid_before = _money(row.paid_amount)
        effective_paid = min(paid_before, amount_after)
        credit_created = _money(max(paid_before - amount_after, Decimal("0")))
        invoiced = _money(row.invoice_allocated_amount)
        if effective_paid >= amount_after:
            next_status = "paid"
        elif effective_paid > 0:
            next_status = "partial_payment"
        elif invoiced > 0:
            next_status = "invoiced_pending_payment"
        else:
            next_status = "confirmed_pending_invoice"
        next_values.update(
            confirmed_amount=amount_after,
            paid_amount=effective_paid,
            status=next_status,
        )
    else:
        next_status = (
            "difference"
            if row.supplier_statement_amount is not None
            and _money(row.supplier_statement_amount) != amount_after
            else "draft"
        )
        next_values["status"] = next_status
    result = db.execute(
        update(SupplierMonthlyStatement)
        .where(
            SupplierMonthlyStatement.id == row.id,
            SupplierMonthlyStatement.active_guard == 1,
            SupplierMonthlyStatement.status == row.status,
            SupplierMonthlyStatement.version == expected_version,
        )
        .values(**next_values)
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
        statement_version_before=expected_version,
        amount_before=amount_before,
        amount_after=amount_after,
        is_post_confirmation=is_post_confirmation,
        created_by=user.id,
    )
    db.add(adjustment)
    db.flush()
    if is_post_confirmation:
        payable = (
            db.get(FinancePayable, row.finance_payable_id)
            if row.finance_payable_id is not None
            else None
        )
        if payable is not None:
            payable_values: dict[str, Any] = {
                "status": "paid" if next_status == "paid" else "confirmed",
                "amount": amount_after,
                "version": payable.version + 1,
                "updated_at": now,
            }
            if next_status == "paid":
                payable_values.update(paid_by=user.id, paid_at=now)
            else:
                payable_values.update(paid_by=None, paid_at=None)
            payable_result = db.execute(
                update(FinancePayable)
                .where(
                    FinancePayable.id == payable.id,
                    FinancePayable.status == payable.status,
                    FinancePayable.version == payable.version,
                )
                .values(**payable_values)
                .execution_options(synchronize_session=False)
            )
            if payable_result.rowcount != 1:
                raise SupplierSettlementError(
                    "SUPPLIER_PAYABLE_STALE",
                    "关联应付状态已变化，请刷新后重试",
                )
        if credit_created > 0:
            db.add(
                SupplierCreditLot(
                    supplier_id=row.supplier_id,
                    supplier_name_snapshot=row.supplier_name_snapshot,
                    source_type="statement_adjustment",
                    source_statement_id=row.id,
                    original_amount=credit_created,
                    available_amount=credit_created,
                    created_by=user.id,
                )
            )
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
    _begin_settlement_write_snapshot(db)
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
    supplier = db.get(Supplier, row.supplier_id)
    if supplier is None:
        raise SupplierSettlementError("SUPPLIER_NOT_FOUND", "供应商主数据不存在，不能确认月结", 404)
    start_utc, end_utc = _utc_period_bounds(row.period_start, row.period_end)
    candidates, issues = _scan_candidates_for_bounds(db, start_utc=start_utc, end_utc=end_utc)
    _require_complete_period(db, supplier=supplier, settlement_month=row.settlement_month,
                             start=row.period_start, end=row.period_end, issues=issues)
    selected = [item for item in candidates if item.supplier_id == row.supplier_id
                and item.currency == row.currency and item.tax_basis == row.tax_basis]
    if _candidate_source_hash(selected) != _statement_line_source_hash(_active_lines(db, row.id)):
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_REGENERATION_REQUIRED",
            "账期实收来源已变化，请先重生成并核对新草稿后再确认应付",
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
        note=(
            f"供应商{row.settlement_day_snapshot}日月结 "
            f"{row.statement_number}（业务修订N{row.document_revision}）"
        ),
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
    invoice_count = int(
        db.scalar(
            select(func.count(SupplierMonthlyInvoice.id)).where(
                SupplierMonthlyInvoice.statement_id == row.id
            )
        )
        or 0
    )
    if invoice_count:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_INVOICE_EXISTS",
            "该月结单已有供应商发票事实，只能登记正负调整，不能重开",
        )
    paid = _money(row.paid_amount)
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
    paid = _money(row.paid_amount)
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


def payment_options(
    db: Session, *, statement_id: int
) -> dict[str, Any]:
    row = db.get(SupplierMonthlyStatement, statement_id)
    if row is None or row.active_guard != 1:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NOT_FOUND", "供应商月结单不存在", 404
        )
    credits = list(
        db.scalars(
            select(SupplierCreditLot)
            .where(
                SupplierCreditLot.supplier_id == row.supplier_id,
                SupplierCreditLot.status.in_({"available", "partial"}),
                SupplierCreditLot.available_amount > 0,
            )
            .order_by(SupplierCreditLot.id)
        ).all()
    )
    acceptances = list(
        db.scalars(
            select(FinanceAcceptanceNote)
            .where(FinanceAcceptanceNote.status == "held")
            .order_by(FinanceAcceptanceNote.received_date, FinanceAcceptanceNote.id)
        ).all()
    )
    return {
        "statement_id": row.id,
        "statement_version": row.version,
        "remaining_payable_amount": _money(
            max(_money(row.confirmed_amount) - _money(row.paid_amount), Decimal("0"))
        ),
        "remaining_invoice_amount": _money(
            max(_money(row.invoice_allocated_amount) - _money(row.paid_amount), Decimal("0"))
        ),
        "credits": [_credit_response(item) for item in credits],
        "acceptances": [
            {
                "id": item.id,
                "bill_number": item.bill_number,
                "customer_id": item.customer_id,
                "customer_name": item.customer_name_snapshot,
                "amount": item.amount,
                "received_date": item.received_date,
                "maturity_date": item.maturity_date,
                "version": item.version,
            }
            for item in acceptances
        ],
    }


def post_payment_batch(
    db: Session,
    *,
    statement_id: int,
    expected_version: int,
    payment_date: date,
    credit_applications: list[dict[str, Any]],
    acceptance_note_id: int | None,
    expected_acceptance_version: int | None,
    bank_amount: Decimal,
    bank_reference: str | None,
    user: User,
) -> tuple[SupplierMonthlyStatement, SupplierPaymentBatch, SupplierCreditLot | None]:
    row = _editable_statement(
        db, statement_id=statement_id, expected_version=expected_version
    )
    if row.status not in CONFIRMED_STATUSES or row.status == "paid":
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_NOT_PAYABLE", "当前月结状态不能登记组合付款"
        )
    confirmed = _money(row.confirmed_amount)
    paid = _money(row.paid_amount)
    invoiced = _money(row.invoice_allocated_amount)
    remaining_payable = _money(max(confirmed - paid, Decimal("0")))
    remaining_invoice = _money(max(invoiced - paid, Decimal("0")))
    if remaining_payable <= 0:
        raise SupplierSettlementError(
            "SUPPLIER_SETTLEMENT_ALREADY_PAID", "当前月结已经结清"
        )
    if remaining_invoice <= 0:
        raise SupplierSettlementError(
            "SUPPLIER_INVOICE_REQUIRED", "请先登记足额供应商发票再付款"
        )

    selected_credits: list[tuple[SupplierCreditLot, Decimal, int]] = []
    seen_credit_ids: set[int] = set()
    credit_total = Decimal("0")
    for application in credit_applications or []:
        credit_id = int(application.get("credit_id") or 0)
        expected_credit_version = int(application.get("expected_version") or 0)
        amount = _money(application.get("amount"))
        if credit_id <= 0 or expected_credit_version <= 0 or amount <= 0:
            raise SupplierSettlementError(
                "SUPPLIER_CREDIT_APPLICATION_INVALID", "供应商余额抵扣明细无效", 422
            )
        if credit_id in seen_credit_ids:
            raise SupplierSettlementError(
                "SUPPLIER_CREDIT_DUPLICATED", "同一供应商余额不能重复选择", 422
            )
        seen_credit_ids.add(credit_id)
        credit = db.get(SupplierCreditLot, credit_id)
        if (
            credit is None
            or credit.supplier_id != row.supplier_id
            or credit.status not in {"available", "partial"}
            or int(credit.version) != expected_credit_version
            or amount > _money(credit.available_amount)
        ):
            raise SupplierSettlementError(
                "SUPPLIER_CREDIT_STALE",
                "供应商余额已变化或不属于当前供应商，请刷新后重试",
            )
        selected_credits.append((credit, amount, expected_credit_version))
        credit_total = _money(credit_total + amount)
    if credit_total > remaining_payable or credit_total > remaining_invoice:
        raise SupplierSettlementError(
            "SUPPLIER_CREDIT_EXCEEDS_SETTLEMENT",
            "供应商余额抵扣不能超过本期未付应付或已收发票金额",
            422,
        )

    after_credit_payable = _money(remaining_payable - credit_total)
    after_credit_invoice = _money(remaining_invoice - credit_total)
    acceptance: FinanceAcceptanceNote | None = None
    acceptance_face = Decimal("0")
    acceptance_applied = Decimal("0")
    credit_created = Decimal("0")
    if acceptance_note_id is not None:
        if expected_acceptance_version is None or expected_acceptance_version <= 0:
            raise SupplierSettlementError(
                "ACCEPTANCE_VERSION_REQUIRED", "请选择最新承兑票据后再提交", 422
            )
        acceptance = db.get(FinanceAcceptanceNote, acceptance_note_id)
        if (
            acceptance is None
            or acceptance.status != "held"
            or int(acceptance.version) != int(expected_acceptance_version)
        ):
            raise SupplierSettlementError(
                "ACCEPTANCE_NOTE_STALE", "承兑票据状态已变化，请刷新后重试"
            )
        if payment_date < acceptance.received_date:
            raise SupplierSettlementError(
                "ACCEPTANCE_ENDORSE_DATE_INVALID", "背书日期不能早于承兑收到日期", 422
            )
        acceptance_face = _money(acceptance.amount)
        acceptance_applied = _money(
            min(acceptance_face, after_credit_payable, after_credit_invoice)
        )
        if acceptance_applied <= 0:
            raise SupplierSettlementError(
                "ACCEPTANCE_NOT_APPLICABLE", "当前应付没有可由该承兑结算的金额", 422
            )
        if acceptance_face > acceptance_applied:
            if acceptance_applied != after_credit_payable:
                raise SupplierSettlementError(
                    "ACCEPTANCE_EXCEEDS_INVOICE",
                    "当前发票金额不足，承兑超出部分不能误记为供应商余额",
                    422,
                )
            credit_created = _money(acceptance_face - acceptance_applied)

    bank = _money(bank_amount)
    if bank < 0:
        raise SupplierSettlementError(
            "SUPPLIER_BANK_AMOUNT_INVALID", "银行付款金额不能小于 0", 422
        )
    if credit_created > 0 and bank > 0:
        raise SupplierSettlementError(
            "SUPPLIER_BANK_WITH_ACCEPTANCE_OVERAGE",
            "承兑已超过本期应付并形成供应商余额，本次不能再填写银行付款",
            422,
        )
    after_nonbank_payable = _money(
        after_credit_payable - acceptance_applied
    )
    after_nonbank_invoice = _money(after_credit_invoice - acceptance_applied)
    if bank > after_nonbank_payable:
        raise SupplierSettlementError(
            "SUPPLIER_PAYMENT_EXCEEDS_PAYABLE", "银行付款不能超过本期剩余应付", 422
        )
    if bank > after_nonbank_invoice:
        raise SupplierSettlementError(
            "SUPPLIER_PAYMENT_EXCEEDS_INVOICE", "银行付款不能超过剩余发票金额", 422
        )
    settled = _money(credit_total + acceptance_applied + bank)
    if settled <= 0:
        raise SupplierSettlementError(
            "SUPPLIER_PAYMENT_BATCH_EMPTY", "请至少填写一种有效付款方式", 422
        )

    batch = SupplierPaymentBatch(
        statement_id=row.id,
        supplier_id=row.supplier_id,
        acceptance_note_id=(acceptance.id if acceptance is not None else None),
        payment_date=payment_date,
        credit_applied_amount=credit_total,
        acceptance_face_amount=acceptance_face,
        acceptance_applied_amount=acceptance_applied,
        bank_amount=bank,
        credit_created_amount=credit_created,
        settled_amount=settled,
        bank_reference=str(bank_reference or "").strip() or None,
        created_by=user.id,
    )
    db.add(batch)
    db.flush()

    for credit, amount, expected_credit_version in selected_credits:
        next_available = _money(credit.available_amount - amount)
        credit_result = db.execute(
            update(SupplierCreditLot)
            .where(
                SupplierCreditLot.id == credit.id,
                SupplierCreditLot.version == expected_credit_version,
                SupplierCreditLot.available_amount == credit.available_amount,
                SupplierCreditLot.status.in_({"available", "partial"}),
            )
            .values(
                available_amount=next_available,
                status="exhausted" if next_available == 0 else "partial",
                version=expected_credit_version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if credit_result.rowcount != 1:
            raise SupplierSettlementError(
                "SUPPLIER_CREDIT_STALE", "供应商余额已被其他操作使用，请刷新后重试"
            )
        db.add(
            SupplierMonthlyPayment(
                statement_id=row.id,
                payment_date=payment_date,
                amount=amount,
                payment_method="credit",
                payment_batch_id=batch.id,
                supplier_credit_id=credit.id,
                reference=f"供应商余额 #{credit.id}",
                created_by=user.id,
            )
        )

    acceptance_payment: SupplierMonthlyPayment | None = None
    if acceptance is not None:
        acceptance_result = db.execute(
            update(FinanceAcceptanceNote)
            .where(
                FinanceAcceptanceNote.id == acceptance.id,
                FinanceAcceptanceNote.status == "held",
                FinanceAcceptanceNote.version == expected_acceptance_version,
            )
            .values(
                status="endorsed",
                supplier_id=row.supplier_id,
                supplier_name_snapshot=row.supplier_name_snapshot,
                supplier_statement_id=row.id,
                endorsed_date=payment_date,
                updated_by=user.id,
                updated_at=utc_now_naive(),
                version=int(expected_acceptance_version) + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if acceptance_result.rowcount != 1:
            raise SupplierSettlementError(
                "ACCEPTANCE_NOTE_STALE", "承兑票据状态已变化，请刷新后重试"
            )
        acceptance_payment = SupplierMonthlyPayment(
            statement_id=row.id,
            payment_date=payment_date,
            amount=acceptance_applied,
            payment_method="acceptance",
            acceptance_note_id=acceptance.id,
            payment_batch_id=batch.id,
            reference=f"承兑 {acceptance.bill_number}",
            created_by=user.id,
        )
        db.add(acceptance_payment)
        db.flush()
        db.execute(
            update(FinanceAcceptanceNote)
            .where(FinanceAcceptanceNote.id == acceptance.id)
            .values(supplier_payment_id=acceptance_payment.id)
        )

    if bank > 0:
        db.add(
            SupplierMonthlyPayment(
                statement_id=row.id,
                payment_date=payment_date,
                amount=bank,
                payment_method="bank",
                payment_batch_id=batch.id,
                reference=str(bank_reference or "").strip() or None,
                created_by=user.id,
            )
        )

    created_credit: SupplierCreditLot | None = None
    if credit_created > 0 and acceptance is not None:
        created_credit = SupplierCreditLot(
            supplier_id=row.supplier_id,
            supplier_name_snapshot=row.supplier_name_snapshot,
            source_type="acceptance_overpayment",
            source_acceptance_note_id=acceptance.id,
            source_statement_id=row.id,
            source_payment_batch_id=batch.id,
            original_amount=credit_created,
            available_amount=credit_created,
            created_by=user.id,
        )
        db.add(created_credit)

    new_paid = _money(paid + settled)
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
        )
    payable = (
        db.get(FinancePayable, row.finance_payable_id)
        if row.finance_payable_id is not None
        else None
    )
    if payable is not None and next_status == "paid":
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
                "SUPPLIER_PAYABLE_STALE", "关联应付状态已变化，请刷新后重试"
            )
    db.flush()
    db.expire(row)
    db.refresh(row)
    return row, batch, created_credit


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
