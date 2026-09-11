"""Freeze supplier payable prices in the receiving transaction.

New receipts all get a per-receipt snapshot. Modern order receipts copy their
approved PurchaseReceiptFact, never internal purpose costs or a newer quote.
Historical adoption remains an explicit, separate hash-guarded operation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import re
import unicodedata
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_contract import utc_naive_to_beijing_date
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
from app.models.material import Material
from app.models.order import OrderItem
from app.models.product import Product
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    PurchaseReceiptFact,
)
from app.models.requisition import Requisition, RequisitionItem
from app.models.stock_replenishment import (
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.supplier import Supplier
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
from app.models.user import User
from app.services.material_purchase_contract import (
    CONFIRMED_PURCHASE_CURRENCY,
    CONFIRMED_PURCHASE_TAX_INCLUDED,
    CONFIRMED_PURCHASE_TAX_RATE,
    normalize_purchase_price_unit,
    purchase_price_contract_issues,
)
from app.services.box_type_rules import box_type_code
from app.services.purchase_receipt_facts import (
    PurchaseReceiptFactValidationError,
    calculate_purchase_sheet_cost_breakdown,
    canonical_purchase_receipt_hash,
)
from app.services.supplier_master import (
    SupplierLookupError,
    normalize_supplier_identity,
    resolve_supplier,
)


HISTORICAL_ADOPTION_REASON = "2026-09-02 老板确认采用当前主数据"
CONFIRMED_SHIPPING_FEE_MODE = "included"
MONEY = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")


class SupplierReceiptPriceFactError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ReceiptSourceContext:
    receipt_item_id: int
    receipt_number: str
    receipt_date: date
    received_quantity: int
    source_kind: str
    purchase_document_number: str
    supplier_name: str
    material_id: int | None
    material_code: str
    report_length_mm: Decimal
    report_width_mm: Decimal


@dataclass(frozen=True, slots=True)
class ReceiptPriceAdoptionPlan:
    incoming_receipt_item_id: int
    receipt_number: str
    receipt_date: date
    received_quantity: int
    quantity_unit: str
    source_kind: str
    purchase_document_number: str
    supplier_id: int
    supplier_name: str
    material_id: int
    material_code: str
    material_version: int
    unit_price: Decimal
    price_unit: str
    currency: str
    tax_included: bool
    tax_rate: Decimal
    shipping_fee_mode: str
    report_length_mm: Decimal
    report_width_mm: Decimal
    match_strategy: str
    source_hash: str
    erp_amount: Decimal
    tax_amount: Decimal

    def response(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_key"] = f"paperboard:{self.incoming_receipt_item_id}"
        payload["adoption_reason"] = HISTORICAL_ADOPTION_REASON
        return payload


def stock_replenishment_uses_paperboard_price(
    db: Session,
    item: StockReplenishmentOrderItem,
) -> bool:
    external_purchase_item_id = db.scalar(
        select(ExternalPackagingPurchaseItem.id)
        .where(ExternalPackagingPurchaseItem.stock_replenishment_item_id == item.id)
        .limit(1)
    )
    if external_purchase_item_id is not None:
        return False
    if item.procurement_route_snapshot == "external_packaging":
        return False
    if item.procurement_route_snapshot == "paperboard":
        return True
    product = db.get(Product, item.product_id) if item.product_id is not None else None
    if item.target_inventory_type == "semi_finished":
        return True
    if item.target_inventory_type != "finished" or item.product_id is None:
        return False
    return product is not None and box_type_code(product.box_style) == "liner"


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _six(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


def _display_name(supplier: Supplier) -> str:
    return str(supplier.display_name or supplier.standard_name).strip()


def _positive_dimension(value: Any, label: str) -> Decimal:
    result = Decimal(str(value or 0))
    if result <= 0:
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_DIMENSIONS_MISSING",
            f"纸板收料缺少有效{label}，不能冻结或采用供应商结算价格",
            422,
        )
    return result


def _candidate_material_codes(value: str) -> tuple[str, ...]:
    compact = unicodedata.normalize("NFKC", str(value or "")).upper()
    compact = re.sub(r"\s+", "", compact)
    if not compact:
        return ()
    candidates = [compact]
    if "/" in compact:
        prefix = compact.split("/", 1)[0]
        if prefix and prefix not in candidates:
            candidates.append(prefix)
    return tuple(candidates)


def _receipt_source_context(db: Session, item: IncomingReceiptItem, *, allow_historical_dimensions: bool = False) -> ReceiptSourceContext:
    receipt = db.get(IncomingReceipt, item.receipt_id)
    if receipt is None or receipt.status != "posted" or item.status != "posted":
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_NOT_POSTED",
            "只有有效已收料记录可以冻结或采用供应商结算价格",
        )
    receipt_date = utc_naive_to_beijing_date(receipt.received_at)
    if item.supplier_order_item_id is not None:
        source = db.get(SupplierRequisitionOrderItem, item.supplier_order_item_id)
        header = (
            db.get(SupplierRequisitionOrder, source.supplier_order_id)
            if source is not None
            else None
        )
        if source is None or header is None:
            raise SupplierReceiptPriceFactError(
                "PAPERBOARD_PURCHASE_SOURCE_MISSING",
                "纸板实收缺少供应商采购来源",
            )
        report_length, report_width = source.report_length_mm, source.report_width_mm
        if allow_historical_dimensions and (report_length is None or report_width is None):
            # Historical adoption only: use the stable linked order's frozen
            # dimensions, never current product data or a multi-line header.
            order_item = db.get(OrderItem, source.order_item_id) if source.order_item_id else None
            if order_item is not None:
                frozen = (order_item.snapshot_report_length_mm, order_item.snapshot_report_width_mm)
                current = (order_item.cardboard_len, order_item.cardboard_width)
                if all(v is not None and v > 0 for v in frozen) and all(v is None or Decimal(v) == Decimal(f) for pair in (current, (report_length, report_width)) for v, f in zip(pair, frozen)):
                    report_length = report_length if report_length is not None else frozen[0]
                    report_width = report_width if report_width is not None else frozen[1]
        return ReceiptSourceContext(
            receipt_item_id=int(item.id),
            receipt_number=str(receipt.receipt_number),
            receipt_date=receipt_date,
            received_quantity=int(item.received_quantity),
            source_kind="supplier_order_item",
            purchase_document_number=str(header.order_number),
            supplier_name=str(
                source.supplier_name_snapshot or header.supplier_name or ""
            ).strip(),
            material_id=(int(source.material_id) if source.material_id is not None else None),
            material_code=str(source.material_code_snapshot or "").strip(),
            report_length_mm=_positive_dimension(report_length, "报料长"),
            report_width_mm=_positive_dimension(report_width, "报料宽"),
        )
    if item.requisition_item_id is not None:
        source = db.get(RequisitionItem, item.requisition_item_id)
        header = db.get(Requisition, source.requisition_id) if source is not None else None
        order_item = db.get(OrderItem, source.order_item_id) if source is not None else None
        if source is None or header is None or order_item is None:
            raise SupplierReceiptPriceFactError(
                "PAPERBOARD_PURCHASE_SOURCE_MISSING",
                "纸板实收缺少旧报料采购来源",
            )
        return ReceiptSourceContext(
            receipt_item_id=int(item.id),
            receipt_number=str(receipt.receipt_number),
            receipt_date=receipt_date,
            received_quantity=int(item.received_quantity),
            source_kind="requisition_item",
            purchase_document_number=str(header.requisition_number),
            supplier_name=str(header.supplier_name or "").strip(),
            material_id=(
                int(order_item.material_id) if order_item.material_id is not None else None
            ),
            material_code=str(source.material_snapshot or "").strip(),
            report_length_mm=_positive_dimension(source.cardboard_len, "报料长"),
            report_width_mm=_positive_dimension(source.cardboard_width, "报料宽"),
        )
    if item.stock_replenishment_item_id is not None:
        source = db.get(StockReplenishmentOrderItem, item.stock_replenishment_item_id)
        header = (
            db.get(StockReplenishmentOrder, source.replenishment_order_id)
            if source is not None
            else None
        )
        if source is None or header is None:
            raise SupplierReceiptPriceFactError(
                "PAPERBOARD_PURCHASE_SOURCE_MISSING",
                "补库实收缺少补库采购来源",
            )
        if not stock_replenishment_uses_paperboard_price(db, source):
            raise SupplierReceiptPriceFactError(
                "SUPPLIER_RECEIPT_NOT_PAPERBOARD",
                "该成品补库没有对应的纸板或外购包材冻结价格来源",
                422,
            )
        product = db.get(Product, source.product_id) if source.product_id else None
        material_id = source.material_id or (
            product.material_id if product is not None else None
        )
        material = db.get(Material, material_id) if material_id is not None else None
        return ReceiptSourceContext(
            receipt_item_id=int(item.id),
            receipt_number=str(receipt.receipt_number),
            receipt_date=receipt_date,
            received_quantity=int(item.received_quantity),
            source_kind="stock_replenishment_item",
            purchase_document_number=str(header.order_number),
            supplier_name=str(header.supplier_name or "").strip(),
            material_id=(int(material_id) if material_id is not None else None),
            material_code=str(
                source.material_code_snapshot
                or (material.code if material is not None else "")
            ).strip(),
            report_length_mm=_positive_dimension(
                source.report_length_mm
                or (product.report_length_mm if product is not None else None),
                "报料长",
            ),
            report_width_mm=_positive_dimension(
                source.report_width_mm
                or (product.report_width_mm if product is not None else None),
                "报料宽",
            ),
        )
    raise SupplierReceiptPriceFactError(
        "PAPERBOARD_PURCHASE_SOURCE_MISSING",
        "纸板实收缺少可识别的采购来源",
    )


def _material_matches_supplier(material: Material, supplier: Supplier) -> bool:
    material_supplier = normalize_supplier_identity(material.supplier_name)
    if not material_supplier:
        return False
    supplier_names = {
        normalized
        for normalized in (
            normalize_supplier_identity(supplier.standard_name),
            normalize_supplier_identity(supplier.display_name),
        )
        if normalized
    }
    supplier_names.update(
        normalized
        for normalized in (
            normalize_supplier_identity(alias.alias_name) for alias in supplier.aliases
        )
        if normalized
    )
    return material_supplier in supplier_names


def _resolve_material(
    db: Session,
    *,
    context: ReceiptSourceContext,
    supplier: Supplier,
    allow_supplier_code_fallback: bool,
) -> tuple[Material, str]:
    if context.material_id is not None:
        material = db.get(Material, context.material_id)
        if (
            material is not None
            and material.is_active
            and _material_matches_supplier(material, supplier)
        ):
            return material, "stable_material_id"
    if not allow_supplier_code_fallback:
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_STABLE_MATERIAL_REQUIRED",
            "收料没有与供应商一致的有效稳定材质，已阻止收料，避免月结猜价",
            422,
        )
    codes = set(_candidate_material_codes(context.material_code))
    if not codes:
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_MATERIAL_CODE_MISSING",
            "旧收料缺少可匹配的材质编码",
            422,
        )
    rows = list(db.scalars(select(Material).where(Material.is_active.is_(True))).all())
    matches = [
        row
        for row in rows
        if _material_matches_supplier(row, supplier)
        and any(code in codes for code in _candidate_material_codes(row.code))
    ]
    unique = {int(row.id): row for row in matches}
    if len(unique) != 1:
        raise SupplierReceiptPriceFactError(
            (
                "SUPPLIER_RECEIPT_MATERIAL_AMBIGUOUS"
                if unique
                else "SUPPLIER_RECEIPT_MATERIAL_NOT_FOUND"
            ),
            (
                "供应商与材质编码匹配到多条当前主数据，必须人工核对"
                if unique
                else "供应商与材质编码没有唯一当前主数据，必须人工核对"
            ),
            422,
        )
    return next(iter(unique.values())), "supplier_unique_material_code"


def _price_plan(
    db: Session,
    *,
    item: IncomingReceiptItem,
    allow_supplier_code_fallback: bool,
    purchase_receipt_fact: PurchaseReceiptFact | None = None,
) -> ReceiptPriceAdoptionPlan:
    context = _receipt_source_context(db, item, allow_historical_dimensions=allow_supplier_code_fallback)
    try:
        supplier = resolve_supplier(
            db,
            context.supplier_name,
            require_active=not allow_supplier_code_fallback,
        )
    except SupplierLookupError as error:
        raise SupplierReceiptPriceFactError(error.code, error.message, 422) from error
    if purchase_receipt_fact is not None:
        native = purchase_receipt_fact
        if not (
            (item.supplier_order_item_id is not None
             and native.supplier_requisition_order_item_id == item.supplier_order_item_id)
            or (item.supplier_order_item_id is None
                and item.requisition_item_id is not None
                and native.material_requisition_item_id == item.requisition_item_id)
        ):
            raise SupplierReceiptPriceFactError(
                "SUPPLIER_RECEIPT_PRICE_SOURCE_MISMATCH", "冻结采购价不属于本次收料来源"
            )
        context = replace(context, material_id=native.actual_material_id)
    material, strategy = _resolve_material(
        db,
        context=context,
        supplier=supplier,
        allow_supplier_code_fallback=allow_supplier_code_fallback,
    )
    native = purchase_receipt_fact
    unit_price = native.unit_price if native is not None else material.quote_price
    price_unit = native.price_unit if native is not None else material.price_unit
    currency = native.currency if native is not None else material.purchase_currency
    tax_included = native.tax_included if native is not None else material.purchase_tax_included
    tax_rate = native.tax_rate if native is not None else material.purchase_tax_rate
    material_version = native.actual_material_version if native is not None else material.version
    material_code = native.actual_material_code_snapshot if native is not None else material.code
    if native is not None:
        strategy = "purchase_receipt_fact"
    issues = purchase_price_contract_issues(
        quote_price=unit_price,
        price_unit=price_unit,
        purchase_currency=currency,
        purchase_tax_included=tax_included,
        purchase_tax_rate=tax_rate,
    )
    if issues:
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_MASTER_PRICE_INVALID",
            "当前供应商材质价格合同不完整：" + "；".join(issues),
            422,
        )
    if (
        str(currency or "").strip().upper()
        != CONFIRMED_PURCHASE_CURRENCY
        or tax_included is not CONFIRMED_PURCHASE_TAX_INCLUDED
        or Decimal(str(tax_rate))
        != CONFIRMED_PURCHASE_TAX_RATE
    ):
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_MASTER_TAX_CONTRACT_INVALID",
            "当前材质采购价必须是人民币、含13%税且含运的最终供应商价格",
            422,
        )
    price_unit = normalize_purchase_price_unit(price_unit)
    assert price_unit is not None
    # Facts persist price and tax rate at six decimals.  Build the plan from the
    # same frozen precision, otherwise a high-precision master quote can make
    # the dry-run total differ from the immutable fact just created.
    frozen_unit_price = _six(unit_price)
    frozen_tax_rate = _six(tax_rate)
    try:
        breakdown = calculate_purchase_sheet_cost_breakdown(
            unit_price=frozen_unit_price,
            price_unit=price_unit,
            tax_included=bool(tax_included),
            tax_rate=frozen_tax_rate,
            report_length_mm=context.report_length_mm,
            report_width_mm=context.report_width_mm,
        )
    except PurchaseReceiptFactValidationError as error:
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_MASTER_PRICE_INVALID", str(error), 422
        ) from error
    source_payload = {
        "incoming_receipt_item_id": context.receipt_item_id,
        "receipt_number": context.receipt_number,
        "receipt_date": context.receipt_date,
        "received_quantity": context.received_quantity,
        "source_kind": context.source_kind,
        "purchase_document_number": context.purchase_document_number,
        "supplier_id": int(supplier.id),
        "supplier_name": _display_name(supplier),
        "material_id": int(material.id),
        "material_code": str(material_code or "").strip(),
        "material_version": int(material_version),
        "unit_price": frozen_unit_price,
        "price_unit": price_unit,
        "currency": str(currency).strip().upper(),
        "tax_included": bool(tax_included),
        "tax_rate": frozen_tax_rate,
        "shipping_fee_mode": CONFIRMED_SHIPPING_FEE_MODE,
        "report_length_mm": context.report_length_mm,
        "report_width_mm": context.report_width_mm,
        "match_strategy": strategy,
    }
    if native is not None:
        source_payload["purchase_receipt_fact_id"] = native.id
        source_payload["purchase_receipt_fact_hash"] = native.request_hash
    quantity = Decimal(context.received_quantity)
    return ReceiptPriceAdoptionPlan(
        incoming_receipt_item_id=context.receipt_item_id,
        receipt_number=context.receipt_number,
        receipt_date=context.receipt_date,
        received_quantity=context.received_quantity,
        quantity_unit="张",
        source_kind=context.source_kind,
        purchase_document_number=context.purchase_document_number,
        supplier_id=int(supplier.id),
        supplier_name=_display_name(supplier),
        material_id=int(material.id),
        material_code=str(material_code or "").strip(),
        material_version=int(material_version),
        unit_price=frozen_unit_price,
        price_unit=price_unit,
        currency=str(currency).strip().upper(),
        tax_included=bool(tax_included),
        tax_rate=frozen_tax_rate,
        shipping_fee_mode=CONFIRMED_SHIPPING_FEE_MODE,
        report_length_mm=context.report_length_mm,
        report_width_mm=context.report_width_mm,
        match_strategy=strategy,
        source_hash=canonical_purchase_receipt_hash(source_payload),
        erp_amount=_money(breakdown.gross_per_sheet * quantity),
        tax_amount=_money(breakdown.tax_per_sheet * quantity),
    )


def _fact_from_plan(
    plan: ReceiptPriceAdoptionPlan,
    *,
    origin: str,
    user: User,
    adoption_evidence_reference: str | None = None,
) -> SupplierReceiptSettlementPriceFact:
    return SupplierReceiptSettlementPriceFact(
        incoming_receipt_item_id=plan.incoming_receipt_item_id,
        supplier_id=plan.supplier_id,
        supplier_name_snapshot=plan.supplier_name,
        material_id=plan.material_id,
        material_code_snapshot=plan.material_code,
        source_material_version=plan.material_version,
        unit_price=plan.unit_price,
        price_unit=plan.price_unit,
        currency=plan.currency,
        tax_included=plan.tax_included,
        tax_rate=plan.tax_rate,
        shipping_fee_mode=plan.shipping_fee_mode,
        fact_origin=origin,
        match_strategy=plan.match_strategy,
        source_hash=plan.source_hash,
        adoption_reason=(HISTORICAL_ADOPTION_REASON if origin == "historical_master_adoption" else None),
        adoption_evidence_reference=(
            adoption_evidence_reference
            if origin == "historical_master_adoption"
            else None
        ),
        source_kind=plan.source_kind,
        purchase_document_number_snapshot=plan.purchase_document_number,
        receipt_number_snapshot=plan.receipt_number,
        receipt_date_snapshot=plan.receipt_date,
        received_quantity_snapshot=plan.received_quantity,
        quantity_unit=plan.quantity_unit,
        report_length_mm=plan.report_length_mm,
        report_width_mm=plan.report_width_mm,
        created_by=int(user.id),
    )


def freeze_stock_replenishment_price(
    db: Session,
    *,
    receipt_item: IncomingReceiptItem,
    user: User,
) -> SupplierReceiptSettlementPriceFact:
    """Freeze one replenishment price before its inventory posting commits."""

    if receipt_item.stock_replenishment_item_id is None:
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_STOCK_SOURCE_REQUIRED",
            "当前收料不是补库来源",
            422,
        )
    return freeze_receipt_settlement_price(db, receipt_item=receipt_item, user=user)


def freeze_receipt_settlement_price(
    db: Session,
    *,
    receipt_item: IncomingReceiptItem,
    user: User,
    purchase_receipt_fact: PurchaseReceiptFact | None = None,
) -> SupplierReceiptSettlementPriceFact:
    """Common gate, called before inventory, progress and audit are posted."""
    existing = db.scalar(
        select(SupplierReceiptSettlementPriceFact).where(
            SupplierReceiptSettlementPriceFact.incoming_receipt_item_id
            == receipt_item.id
        )
    )
    if existing is not None:
        if (
            existing.fact_origin != "receipt_frozen"
            or Decimal(existing.received_quantity_snapshot)
            != Decimal(int(receipt_item.received_quantity))
            or existing.incoming_receipt_item_id != receipt_item.id
        ):
            raise SupplierReceiptPriceFactError(
                "SUPPLIER_RECEIPT_PRICE_FACT_CONFLICT",
                "该实收已有不同的结算价格事实，已阻止重复入账",
            )
        return existing
    plan = _price_plan(
        db,
        item=receipt_item,
        allow_supplier_code_fallback=False,
        purchase_receipt_fact=purchase_receipt_fact,
    )
    fact = _fact_from_plan(plan, origin="receipt_frozen", user=user)
    db.add(fact)
    db.flush()
    return fact


def _native_price_receipt_item_ids(db: Session) -> set[int]:
    return {
        int(value)
        for value in db.scalars(
            select(IncomingReceiptPurposeAllocation.incoming_receipt_item_id)
            .join(
                PurchaseReceiptFact,
                PurchaseReceiptFact.id
                == IncomingReceiptPurposeAllocation.purchase_receipt_fact_id,
            )
        ).all()
    }


def assert_receipt_settlement_price(
    db: Session, *, receipt_item: IncomingReceiptItem
) -> SupplierReceiptSettlementPriceFact:
    """Validate a new/restored receipt without ever adopting or repairing it."""
    fact = db.scalar(select(SupplierReceiptSettlementPriceFact).where(
        SupplierReceiptSettlementPriceFact.incoming_receipt_item_id == receipt_item.id
    ))
    context = _receipt_source_context(db, receipt_item)
    if fact is None or (
        fact.fact_origin != "receipt_frozen"
        or fact.finance_only_test_classification
        or Decimal(fact.received_quantity_snapshot) != Decimal(context.received_quantity)
        or fact.receipt_number_snapshot != context.receipt_number
        or fact.receipt_date_snapshot != context.receipt_date
        or fact.source_kind != context.source_kind
        or fact.purchase_document_number_snapshot != context.purchase_document_number
    ):
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_PRICE_FACT_REQUIRED", "本次有效实收必须有匹配的不可变供应商结算价"
        )
    return fact


def _period_items(
    db: Session, *, start_utc: datetime, end_utc: datetime
) -> list[IncomingReceiptItem]:
    return list(
        db.scalars(
            select(IncomingReceiptItem)
            .join(IncomingReceipt, IncomingReceipt.id == IncomingReceiptItem.receipt_id)
            .where(
                IncomingReceipt.status == "posted",
                IncomingReceiptItem.status == "posted",
                IncomingReceipt.received_at >= start_utc,
                IncomingReceipt.received_at < end_utc,
            )
            .order_by(IncomingReceipt.received_at, IncomingReceiptItem.id)
        ).all()
    )


def preview_historical_price_adoptions(
    db: Session,
    *,
    settlement_month: str,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[str, Any]:
    native_ids = _native_price_receipt_item_ids(db)
    existing_ids = {
        int(value)
        for value in db.scalars(
            select(SupplierReceiptSettlementPriceFact.incoming_receipt_item_id)
        ).all()
    }
    eligible: list[ReceiptPriceAdoptionPlan] = []
    rejected: list[dict[str, Any]] = []
    for item in _period_items(db, start_utc=start_utc, end_utc=end_utc):
        if int(item.id) in native_ids or int(item.id) in existing_ids:
            continue
        try:
            eligible.append(
                _price_plan(
                    db,
                    item=item,
                    allow_supplier_code_fallback=True,
                )
            )
        except SupplierReceiptPriceFactError as error:
            if error.code == "SUPPLIER_RECEIPT_NOT_PAPERBOARD":
                continue
            receipt = db.get(IncomingReceipt, item.receipt_id)
            rejected.append(
                {
                    "incoming_receipt_item_id": int(item.id),
                    "source_key": f"paperboard:{int(item.id)}",
                    "receipt_number": (
                        str(receipt.receipt_number) if receipt is not None else ""
                    ),
                    "code": error.code,
                    "message": error.message,
                    "recommended_action": "核对供应商、采购来源、材质、报料长宽和有效采购价",
                }
            )
    response_rows = [row.response() for row in eligible]
    return {
        "settlement_month": settlement_month,
        "eligible_count": len(response_rows),
        "rejected_count": len(rejected),
        "eligible": response_rows,
        "rejected": rejected,
        "plan_hash": canonical_purchase_receipt_hash(
            [
                {
                    "incoming_receipt_item_id": row.incoming_receipt_item_id,
                    "source_hash": row.source_hash,
                }
                for row in eligible
            ]
        ),
        "adoption_reason": HISTORICAL_ADOPTION_REASON,
        "writes_performed": False,
    }


def adopt_historical_price_facts(
    db: Session,
    *,
    settlement_month: str,
    start_utc: datetime,
    end_utc: datetime,
    selections: Iterable[tuple[int, str]],
    confirmation_text: str,
    plan_hash: str,
    backup_reference: str,
    user: User,
) -> dict[str, Any]:
    if str(confirmation_text or "").strip() != HISTORICAL_ADOPTION_REASON:
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_ADOPTION_CONFIRMATION_REQUIRED",
            "历史价格采用确认文字不匹配，未写入任何事实",
            422,
        )
    normalized_backup_reference = str(backup_reference or "").strip()
    if len(normalized_backup_reference) < 8 or len(normalized_backup_reference) > 255:
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_ADOPTION_BACKUP_REQUIRED",
            "请填写已验证的正式库备份文件名或独立执行回执编号",
            422,
        )
    selected = [
        (int(item_id), str(source_hash).strip().lower())
        for item_id, source_hash in selections
    ]
    if (
        not selected
        or len(selected) > 500
        or len({row[0] for row in selected}) != len(selected)
    ):
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_ADOPTION_SELECTION_INVALID",
            "请选择 1 至 500 条不重复的历史收料采用计划",
            422,
        )
    normalized_plan_hash = str(plan_hash or "").strip().lower()
    period_ids = {
        int(item.id): item
        for item in _period_items(db, start_utc=start_utc, end_utc=end_utc)
    }
    native_ids = _native_price_receipt_item_ids(db)
    existing_by_item_id = {
        int(row.incoming_receipt_item_id): row
        for row in db.scalars(
            select(SupplierReceiptSettlementPriceFact).where(
                SupplierReceiptSettlementPriceFact.incoming_receipt_item_id.in_(
                    [row[0] for row in selected]
                )
            )
        ).all()
    }
    if len(existing_by_item_id) == len(selected):
        reused = []
        for item_id, expected_hash in selected:
            existing = existing_by_item_id[item_id]
            if (
                existing.fact_origin != "historical_master_adoption"
                or existing.source_hash != expected_hash
                or existing.adoption_reason != HISTORICAL_ADOPTION_REASON
                or existing.adoption_evidence_reference
                != normalized_backup_reference
            ):
                raise SupplierReceiptPriceFactError(
                    "SUPPLIER_RECEIPT_PRICE_FACT_CONFLICT",
                    f"收料明细 #{item_id} 已有不同结算价格事实",
                )
            reused.append(existing)
        return {
            "settlement_month": settlement_month,
            "created_count": 0,
            "reused_count": len(reused),
            "fact_ids": [int(row.id) for row in reused],
            "adoption_reason": HISTORICAL_ADOPTION_REASON,
            "plan_hash": normalized_plan_hash,
            "backup_reference": normalized_backup_reference,
            "writes_performed": False,
        }
    preview = preview_historical_price_adoptions(
        db,
        settlement_month=settlement_month,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    if normalized_plan_hash != preview["plan_hash"]:
        raise SupplierReceiptPriceFactError(
            "SUPPLIER_RECEIPT_ADOPTION_PLAN_STALE",
            "历史缺价采用清单已变化，请重新预览并核对备份后再执行",
            409,
        )
    created: list[SupplierReceiptSettlementPriceFact] = []
    reused: list[SupplierReceiptSettlementPriceFact] = []
    for item_id, expected_hash in selected:
        item = period_ids.get(item_id)
        if item is None:
            raise SupplierReceiptPriceFactError(
                "SUPPLIER_RECEIPT_ADOPTION_OUTSIDE_PERIOD",
                f"收料明细 #{item_id} 不属于 {settlement_month} 结算周期",
                422,
            )
        existing = existing_by_item_id.get(item_id)
        if existing is not None:
            if (
                existing.fact_origin != "historical_master_adoption"
                or existing.source_hash != expected_hash
                or existing.adoption_reason != HISTORICAL_ADOPTION_REASON
                or existing.adoption_evidence_reference
                != normalized_backup_reference
            ):
                raise SupplierReceiptPriceFactError(
                    "SUPPLIER_RECEIPT_PRICE_FACT_CONFLICT",
                    f"收料明细 #{item_id} 已有不同结算价格事实",
                )
            reused.append(existing)
            continue
        if item_id in native_ids:
            raise SupplierReceiptPriceFactError(
                "SUPPLIER_RECEIPT_NATIVE_PRICE_EXISTS",
                f"收料明细 #{item_id} 已有原始冻结价，无需历史采用",
            )
        plan = _price_plan(db, item=item, allow_supplier_code_fallback=True)
        if plan.source_hash != expected_hash:
            raise SupplierReceiptPriceFactError(
                "SUPPLIER_RECEIPT_ADOPTION_PLAN_STALE",
                f"收料明细 #{item_id} 的供应商、材质或价格已变化，请重新 dry-run",
            )
        fact = _fact_from_plan(
            plan,
            origin="historical_master_adoption",
            user=user,
            adoption_evidence_reference=normalized_backup_reference,
        )
        db.add(fact)
        db.flush()
        created.append(fact)
    return {
        "settlement_month": settlement_month,
        "created_count": len(created),
        "reused_count": len(reused),
        "fact_ids": [int(row.id) for row in [*created, *reused]],
        "adoption_reason": HISTORICAL_ADOPTION_REASON,
        "plan_hash": normalized_plan_hash,
        "backup_reference": normalized_backup_reference,
        "writes_performed": bool(created),
    }
