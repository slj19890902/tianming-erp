"""P1-81 canonical final-price facts and deterministic sheet-cost helpers.

This module never posts a receipt.  Callers must keep the returned fact and the
later receipt/allocation/inventory writes in their own outer transaction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
import hashlib
import hmac
import json
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.material import Material
from app.models.purchase_receipt import (
    PurchaseReceiptFact,
    PurchaseReceiptMaterialVariance,
    PurchaseReceiptMaterialVarianceApproval,
)
from app.models.requisition import RequisitionItem
from app.models.supplier_requisition_order import (
    PurchasePurposeSourceSnapshot,
    SupplierRequisitionOrderItem,
)


MONEY_QUANTUM = Decimal("0.000001")
AREA_DIVISOR = Decimal("1000000")


class PurchaseReceiptFactError(ValueError):
    code = "purchase_receipt_fact_error"


class PurchaseReceiptFactValidationError(PurchaseReceiptFactError):
    code = "purchase_receipt_fact_invalid"


class PurchaseReceiptFactStaleError(PurchaseReceiptFactError):
    code = "purchase_receipt_fact_stale"


class PurchaseReceiptFactIdempotencyConflict(PurchaseReceiptFactError):
    code = "purchase_receipt_fact_idempotency_conflict"


def material_calculation_fingerprint(material: Material) -> str:
    """Hash every mutable master field used by receipt allocation/cost posting."""

    return canonical_purchase_receipt_hash(
        {
            "material_id": int(material.id),
            "version": int(material.version),
            "code": str(material.code or "").strip(),
            "layer_count": material.layer_count,
            "flute_type": str(material.flute_type or "").strip() or None,
            "is_active": bool(material.is_active),
        }
    )


@dataclass(frozen=True, slots=True)
class PurchaseSheetCost:
    net_per_sheet: Decimal
    tax_per_sheet: Decimal
    gross_per_sheet: Decimal
    capitalized_per_sheet: Decimal
    area_square_meters: Decimal | None


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PurchaseReceiptFactValidationError("价格事实不能包含非有限数字")
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            return int(normalized)
        return format(normalized, "f")
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise PurchaseReceiptFactValidationError("价格事实不能包含非有限数字")
        return _canonical_value(Decimal(str(value)))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    if hasattr(value, "__dataclass_fields__"):
        return _canonical_value(asdict(value))
    raise PurchaseReceiptFactValidationError(
        f"价格事实包含不支持的类型：{type(value).__name__}"
    )


def canonical_purchase_receipt_json(payload: Any) -> str:
    return json.dumps(
        _canonical_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_purchase_receipt_hash(payload: Any) -> str:
    return hashlib.sha256(
        canonical_purchase_receipt_json(payload).encode("utf-8")
    ).hexdigest()


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise PurchaseReceiptFactValidationError(f"{label}必须是有效数字")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PurchaseReceiptFactValidationError(f"{label}必须是有效数字") from exc
    if not result.is_finite():
        raise PurchaseReceiptFactValidationError(f"{label}必须是有限数字")
    return result


def _positive_int(value: Any, label: str) -> int:
    number = _decimal(value, label)
    if number != number.to_integral_value() or number <= 0:
        raise PurchaseReceiptFactValidationError(f"{label}必须是正整数")
    return int(number)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def calculate_purchase_sheet_cost_breakdown(
    *,
    unit_price: Any,
    price_unit: str,
    tax_included: bool,
    tax_rate: Any,
    report_length_mm: Any | None = None,
    report_width_mm: Any | None = None,
) -> PurchaseSheetCost:
    """Normalize a final quote to one received sheet; capitalized cost is gross."""

    price = _decimal(unit_price, "最终采购单价")
    if price <= 0:
        raise PurchaseReceiptFactValidationError("最终采购单价必须大于0")
    rate = _decimal(tax_rate, "税率")
    if rate < 0 or rate > 1:
        raise PurchaseReceiptFactValidationError("税率必须在0到1之间")
    normalized_unit = str(price_unit or "").strip().lower()
    area: Decimal | None = None
    if normalized_unit == "per_sheet":
        quoted_per_sheet = price
    elif normalized_unit == "per_square_meter":
        length = _decimal(report_length_mm, "报料长")
        width = _decimal(report_width_mm, "报料宽")
        if length <= 0 or width <= 0:
            raise PurchaseReceiptFactValidationError("按平方米计价时必须提供正数报料长宽")
        area = (length * width / AREA_DIVISOR).quantize(
            Decimal("0.000000001"), rounding=ROUND_HALF_UP
        )
        quoted_per_sheet = price * area
    else:
        raise PurchaseReceiptFactValidationError(
            "价格单位必须是per_sheet或per_square_meter"
        )

    if bool(tax_included):
        gross = quoted_per_sheet
        net = quoted_per_sheet / (Decimal("1") + rate)
    else:
        net = quoted_per_sheet
        gross = quoted_per_sheet * (Decimal("1") + rate)
    net = _money(net)
    gross = _money(gross)
    tax = _money(gross - net)
    return PurchaseSheetCost(
        net_per_sheet=net,
        tax_per_sheet=tax,
        gross_per_sheet=gross,
        capitalized_per_sheet=gross,
        area_square_meters=area,
    )


def calculate_per_sheet_cost(
    *,
    unit_price: Any,
    price_unit: str,
    tax_included: bool,
    tax_rate: Any,
    report_length_mm: Any | None = None,
    report_width_mm: Any | None = None,
) -> Decimal:
    """Return the deterministic gross/capitalized cost of one physical sheet."""

    return calculate_purchase_sheet_cost_breakdown(
        unit_price=unit_price,
        price_unit=price_unit,
        tax_included=tax_included,
        tax_rate=tax_rate,
        report_length_mm=report_length_mm,
        report_width_mm=report_width_mm,
    ).capitalized_per_sheet


def assert_purchase_receipt_fact_replay(
    *,
    stored_request_hash: str | None,
    stored_actor_id: int | None,
    submitted_request_hash: str,
    submitted_actor_id: int,
) -> None:
    stored_hash = str(stored_request_hash or "").strip().lower()
    submitted_hash = str(submitted_request_hash or "").strip().lower()
    if stored_actor_id is None or int(stored_actor_id) != int(submitted_actor_id):
        raise PurchaseReceiptFactIdempotencyConflict(
            "同一价格事实幂等键不能由不同操作者重放"
        )
    if (
        len(stored_hash) != 64
        or len(submitted_hash) != 64
        or not hmac.compare_digest(stored_hash, submitted_hash)
    ):
        raise PurchaseReceiptFactIdempotencyConflict(
            "同一价格事实幂等键不能提交不同载荷"
        )


def _request_payload(
    *,
    supplier_requisition_order_item_id: int | None,
    material_requisition_item_id: int | None,
    source_key: str,
    receipt_fact_version: int,
    expected_source_version: int,
    purchase_purpose_source_snapshot_id: int,
    purpose_snapshot_version: int,
    receipt_plan_fingerprint: str,
    actual_material_id: int,
    actual_material_code_snapshot: str,
    actual_material_version: int,
    actual_material_layer_count_snapshot: int | None,
    actual_material_flute_type_snapshot: str | None,
    actual_material_is_active_snapshot: bool,
    actual_material_fingerprint: str,
    expected_material_id: int | None,
    expected_material_code_snapshot: str,
    material_change_confirmed: bool,
    material_variance_approval_id: int | None,
    unit_price: Decimal,
    currency: str,
    price_unit: str,
    tax_included: bool,
    tax_rate: Decimal,
) -> dict[str, Any]:
    return {
        "supplier_requisition_order_item_id": supplier_requisition_order_item_id,
        "material_requisition_item_id": material_requisition_item_id,
        "source_key": source_key,
        "receipt_fact_version": receipt_fact_version,
        "expected_source_version": expected_source_version,
        "purchase_purpose_source_snapshot_id": purchase_purpose_source_snapshot_id,
        "purpose_snapshot_version": purpose_snapshot_version,
        "receipt_plan_fingerprint": receipt_plan_fingerprint,
        "actual_material_id": actual_material_id,
        "actual_material_code_snapshot": actual_material_code_snapshot,
        "actual_material_version": actual_material_version,
        "actual_material_layer_count_snapshot": actual_material_layer_count_snapshot,
        "actual_material_flute_type_snapshot": actual_material_flute_type_snapshot,
        "actual_material_is_active_snapshot": bool(actual_material_is_active_snapshot),
        "actual_material_fingerprint": actual_material_fingerprint,
        "expected_material_id": expected_material_id,
        "expected_material_code_snapshot": expected_material_code_snapshot,
        "material_change_confirmed": bool(material_change_confirmed),
        "material_variance_approval_id": material_variance_approval_id,
        "unit_price": unit_price,
        "currency": currency,
        "price_unit": price_unit,
        "tax_included": bool(tax_included),
        "tax_rate": tax_rate,
    }


def _normalize_currency(value: str) -> str:
    currency = str(value or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise PurchaseReceiptFactValidationError("币种必须是3位字母代码")
    return currency


def _normalize_hash(value: str, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise PurchaseReceiptFactValidationError(f"{label}必须是64位SHA-256")
    return normalized


def _load_source(
    db: Session,
    *,
    supplier_requisition_order_item_id: int | None,
    material_requisition_item_id: int | None,
) -> SupplierRequisitionOrderItem | RequisitionItem:
    supplier_id = (
        _positive_int(supplier_requisition_order_item_id, "供应商报料明细ID")
        if supplier_requisition_order_item_id is not None
        else None
    )
    requisition_id = (
        _positive_int(material_requisition_item_id, "报料明细ID")
        if material_requisition_item_id is not None
        else None
    )
    if (supplier_id is None) == (requisition_id is None):
        raise PurchaseReceiptFactValidationError("价格事实必须且只能绑定一种报料来源")
    if supplier_id is not None:
        statement = (
            select(SupplierRequisitionOrderItem)
            .where(SupplierRequisitionOrderItem.id == supplier_id)
            .with_for_update()
        )
    else:
        statement = (
            select(RequisitionItem)
            .where(RequisitionItem.id == requisition_id)
            .with_for_update()
        )
    source = db.scalar(statement)
    if source is None:
        raise PurchaseReceiptFactValidationError("报料来源不存在")
    if source.purpose_contract_status != "frozen":
        raise PurchaseReceiptFactValidationError("历史未冻结用途来源不能建立正式价格事实")
    return source


def _load_purpose_snapshot(
    db: Session,
    *,
    snapshot_id: int,
    supplier_requisition_order_item_id: int | None,
    material_requisition_item_id: int | None,
) -> PurchasePurposeSourceSnapshot:
    snapshot = db.scalar(
        select(PurchasePurposeSourceSnapshot).where(
            PurchasePurposeSourceSnapshot.id == _positive_int(snapshot_id, "用途快照ID")
        )
    )
    if snapshot is None:
        raise PurchaseReceiptFactValidationError("采购用途快照不存在")
    if (
        snapshot.supplier_requisition_order_item_id
        != supplier_requisition_order_item_id
        or snapshot.material_requisition_item_id != material_requisition_item_id
    ):
        raise PurchaseReceiptFactValidationError("采购用途快照与报料来源不匹配")
    return snapshot


def create_or_replay_material_variance(
    db: Session,
    *,
    supplier_requisition_order_item_id: int | None = None,
    material_requisition_item_id: int | None = None,
    purchase_purpose_source_snapshot_id: int,
    expected_source_version: int,
    purpose_snapshot_version: int,
    receipt_plan_fingerprint: str,
    actual_material_id: int,
    reason: str,
    idempotency_key: str,
    requested_by: int,
) -> PurchaseReceiptMaterialVariance:
    actor_id = _positive_int(requested_by, "申请人ID")
    key = str(idempotency_key or "").strip()
    clean_reason = str(reason or "").strip()
    if not key or not clean_reason:
        raise PurchaseReceiptFactValidationError("材质差异幂等键和原因不能为空")
    source = _load_source(
        db,
        supplier_requisition_order_item_id=supplier_requisition_order_item_id,
        material_requisition_item_id=material_requisition_item_id,
    )
    source_version = _positive_int(expected_source_version, "来源版本")
    if int(source.version) != source_version:
        raise PurchaseReceiptFactStaleError("报料来源已变化，请刷新后重试")
    supplier_id = int(source.id) if isinstance(source, SupplierRequisitionOrderItem) else None
    requisition_id = int(source.id) if isinstance(source, RequisitionItem) else None
    snapshot = _load_purpose_snapshot(
        db,
        snapshot_id=purchase_purpose_source_snapshot_id,
        supplier_requisition_order_item_id=supplier_id,
        material_requisition_item_id=requisition_id,
    )
    snapshot_version = _positive_int(purpose_snapshot_version, "用途快照版本")
    plan_fingerprint = _normalize_hash(receipt_plan_fingerprint, "收料用途指纹")
    if int(snapshot.snapshot_version) != snapshot_version or not hmac.compare_digest(
        snapshot.preview_fingerprint.lower(), plan_fingerprint
    ):
        raise PurchaseReceiptFactStaleError("采购用途版本或指纹已变化，请刷新后重试")
    material = db.get(Material, _positive_int(actual_material_id, "实际材质ID"))
    if material is None or not material.is_active:
        raise PurchaseReceiptFactValidationError("实际材质不存在或已停用")
    actual_code = str(material.code or "").strip()
    expected_id = source.material_id if isinstance(source, SupplierRequisitionOrderItem) else None
    expected_code = str(
        (source.material_code_snapshot or "")
        if isinstance(source, SupplierRequisitionOrderItem)
        else (source.material_snapshot or "")
    ).strip()
    if not expected_code or actual_code == expected_code:
        raise PurchaseReceiptFactValidationError("只有实际材质与采购快照不一致时才需要差异确认")
    material_fingerprint = material_calculation_fingerprint(material)
    request_hash = canonical_purchase_receipt_hash(
        {
            "supplier_requisition_order_item_id": supplier_id,
            "material_requisition_item_id": requisition_id,
            "purchase_purpose_source_snapshot_id": snapshot.id,
            "expected_source_version": source_version,
            "purpose_snapshot_version": snapshot_version,
            "receipt_plan_fingerprint": plan_fingerprint,
            "actual_material_id": material.id,
            "actual_material_fingerprint": material_fingerprint,
            "reason": clean_reason,
        }
    )
    existing = db.scalar(
        select(PurchaseReceiptMaterialVariance).where(
            PurchaseReceiptMaterialVariance.idempotency_key == key
        )
    )
    if existing is not None:
        assert_purchase_receipt_fact_replay(
            stored_request_hash=existing.request_hash,
            stored_actor_id=existing.requested_by,
            submitted_request_hash=request_hash,
            submitted_actor_id=actor_id,
        )
        return existing
    variance = PurchaseReceiptMaterialVariance(
        supplier_requisition_order_item_id=supplier_id,
        material_requisition_item_id=requisition_id,
        purchase_purpose_source_snapshot_id=snapshot.id,
        source_key=snapshot.source_key,
        expected_source_version=source_version,
        purpose_snapshot_version=snapshot_version,
        receipt_plan_fingerprint=plan_fingerprint,
        expected_material_id=expected_id,
        expected_material_code_snapshot=expected_code,
        actual_material_id=material.id,
        actual_material_code_snapshot=actual_code,
        actual_material_version=int(material.version),
        actual_material_layer_count_snapshot=material.layer_count,
        actual_material_flute_type_snapshot=str(material.flute_type or "").strip() or None,
        actual_material_fingerprint=material_fingerprint,
        reason=clean_reason,
        idempotency_key=key,
        request_hash=request_hash,
        requested_by=actor_id,
    )
    db.add(variance)
    db.flush()
    return variance


def confirm_or_replay_material_variance(
    db: Session,
    *,
    material_variance_id: int,
    idempotency_key: str,
    confirmed_by: int,
) -> PurchaseReceiptMaterialVarianceApproval:
    actor_id = _positive_int(confirmed_by, "确认人ID")
    key = str(idempotency_key or "").strip()
    if not key:
        raise PurchaseReceiptFactValidationError("材质差异确认幂等键不能为空")
    variance = db.get(
        PurchaseReceiptMaterialVariance,
        _positive_int(material_variance_id, "材质差异ID"),
    )
    if variance is None:
        raise PurchaseReceiptFactValidationError("材质差异事实不存在")
    if int(variance.requested_by) == actor_id:
        raise PurchaseReceiptFactValidationError("材质差异申请人不能自我确认")
    request_hash = canonical_purchase_receipt_hash(
        {"material_variance_id": variance.id, "decision": "confirmed"}
    )
    existing = db.scalar(
        select(PurchaseReceiptMaterialVarianceApproval).where(
            PurchaseReceiptMaterialVarianceApproval.idempotency_key == key
        )
    )
    if existing is not None:
        assert_purchase_receipt_fact_replay(
            stored_request_hash=existing.request_hash,
            stored_actor_id=existing.confirmed_by,
            submitted_request_hash=request_hash,
            submitted_actor_id=actor_id,
        )
        return existing
    prior = db.scalar(
        select(PurchaseReceiptMaterialVarianceApproval).where(
            PurchaseReceiptMaterialVarianceApproval.material_variance_id == variance.id
        )
    )
    if prior is not None:
        raise PurchaseReceiptFactIdempotencyConflict("该材质差异已由其他确认事实处理")
    approval = PurchaseReceiptMaterialVarianceApproval(
        material_variance_id=variance.id,
        idempotency_key=key,
        request_hash=request_hash,
        confirmed_by=actor_id,
    )
    db.add(approval)
    db.flush()
    return approval


def serialize_material_variance(variance: PurchaseReceiptMaterialVariance) -> dict[str, Any]:
    return {
        "material_variance_id": variance.id,
        "source_key": variance.source_key,
        "purchase_purpose_source_snapshot_id": variance.purchase_purpose_source_snapshot_id,
        "expected_source_version": variance.expected_source_version,
        "purpose_snapshot_version": variance.purpose_snapshot_version,
        "receipt_plan_fingerprint": variance.receipt_plan_fingerprint,
        "expected_material_id": variance.expected_material_id,
        "expected_material_code": variance.expected_material_code_snapshot,
        "actual_material_id": variance.actual_material_id,
        "actual_material_code": variance.actual_material_code_snapshot,
        "actual_material_version": variance.actual_material_version,
        "actual_material_fingerprint": variance.actual_material_fingerprint,
        "reason": variance.reason,
        "requested_by": variance.requested_by,
    }


def serialize_material_variance_approval(
    approval: PurchaseReceiptMaterialVarianceApproval,
) -> dict[str, Any]:
    return {
        "material_variance_approval_id": approval.id,
        "material_variance_id": approval.material_variance_id,
        "confirmed_by": approval.confirmed_by,
    }


def create_or_replay_purchase_receipt_fact(
    db: Session,
    *,
    supplier_requisition_order_item_id: int | None = None,
    material_requisition_item_id: int | None = None,
    purchase_purpose_source_snapshot_id: int,
    expected_source_version: int,
    purpose_snapshot_version: int,
    receipt_plan_fingerprint: str,
    actual_material_id: int,
    material_change_confirmed: bool,
    unit_price: Any,
    currency: str,
    price_unit: str,
    tax_included: bool,
    tax_rate: Any,
    idempotency_key: str,
    created_by: int,
    material_variance_approval_id: int | None = None,
    submitted_request_hash: str | None = None,
    expected_latest_receipt_fact_version: int | None = None,
) -> PurchaseReceiptFact:
    """Create a locked source-version fact or replay the same actor/payload."""

    actor_id = _positive_int(created_by, "操作者ID")
    key = str(idempotency_key or "").strip()
    if not key:
        raise PurchaseReceiptFactValidationError("价格事实幂等键不能为空")
    existing = db.scalar(
        select(PurchaseReceiptFact).where(PurchaseReceiptFact.idempotency_key == key)
    )
    if existing is not None:
        submitted_supplier_id = (
            _positive_int(
                supplier_requisition_order_item_id, "供应商报料明细ID"
            )
            if supplier_requisition_order_item_id is not None
            else None
        )
        submitted_requisition_id = (
            _positive_int(material_requisition_item_id, "报料明细ID")
            if material_requisition_item_id is not None
            else None
        )
        if (submitted_supplier_id is None) == (submitted_requisition_id is None):
            raise PurchaseReceiptFactValidationError(
                "价格事实必须且只能绑定一种报料来源"
            )
        normalized_price = _money(_decimal(unit_price, "最终采购单价"))
        normalized_tax_rate = _decimal(tax_rate, "税率").quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        if normalized_price <= 0 or normalized_tax_rate < 0 or normalized_tax_rate > 1:
            raise PurchaseReceiptFactValidationError("最终价格或税率无效")
        normalized_price_unit = str(price_unit or "").strip().lower()
        if normalized_price_unit not in {"per_sheet", "per_square_meter"}:
            raise PurchaseReceiptFactValidationError(
                "价格单位必须是per_sheet或per_square_meter"
            )
        request_payload = _request_payload(
            supplier_requisition_order_item_id=submitted_supplier_id,
            material_requisition_item_id=submitted_requisition_id,
            source_key=existing.source_key,
            receipt_fact_version=int(existing.receipt_fact_version),
            expected_source_version=_positive_int(
                expected_source_version, "来源版本"
            ),
            purchase_purpose_source_snapshot_id=_positive_int(
                purchase_purpose_source_snapshot_id, "用途快照ID"
            ),
            purpose_snapshot_version=_positive_int(
                purpose_snapshot_version, "用途快照版本"
            ),
            receipt_plan_fingerprint=_normalize_hash(
                receipt_plan_fingerprint, "收料用途指纹"
            ),
            actual_material_id=_positive_int(actual_material_id, "实际材质ID"),
            actual_material_code_snapshot=existing.actual_material_code_snapshot,
            actual_material_version=int(existing.actual_material_version),
            actual_material_layer_count_snapshot=existing.actual_material_layer_count_snapshot,
            actual_material_flute_type_snapshot=existing.actual_material_flute_type_snapshot,
            actual_material_is_active_snapshot=bool(existing.actual_material_is_active_snapshot),
            actual_material_fingerprint=existing.actual_material_fingerprint,
            expected_material_id=existing.expected_material_id,
            expected_material_code_snapshot=existing.expected_material_code_snapshot,
            material_change_confirmed=bool(material_change_confirmed),
            material_variance_approval_id=(
                _positive_int(material_variance_approval_id, "材质差异确认ID")
                if material_variance_approval_id is not None
                else None
            ),
            unit_price=normalized_price,
            currency=_normalize_currency(currency),
            price_unit=normalized_price_unit,
            tax_included=bool(tax_included),
            tax_rate=normalized_tax_rate,
        )
        request_hash = canonical_purchase_receipt_hash(request_payload)
        if submitted_request_hash is not None:
            submitted_hash = _normalize_hash(submitted_request_hash, "请求哈希")
            if not hmac.compare_digest(request_hash, submitted_hash):
                raise PurchaseReceiptFactStaleError(
                    "价格事实请求内容与请求哈希不一致"
                )
        assert_purchase_receipt_fact_replay(
            stored_request_hash=existing.request_hash,
            stored_actor_id=existing.created_by,
            submitted_request_hash=request_hash,
            submitted_actor_id=actor_id,
        )
        return existing

    source = _load_source(
        db,
        supplier_requisition_order_item_id=supplier_requisition_order_item_id,
        material_requisition_item_id=material_requisition_item_id,
    )
    source_version = _positive_int(expected_source_version, "来源版本")
    if int(source.version) != source_version:
        raise PurchaseReceiptFactStaleError("报料来源已变化，请刷新后重试")
    supplier_id = (
        int(source.id) if isinstance(source, SupplierRequisitionOrderItem) else None
    )
    requisition_id = int(source.id) if isinstance(source, RequisitionItem) else None

    snapshot = _load_purpose_snapshot(
        db,
        snapshot_id=purchase_purpose_source_snapshot_id,
        supplier_requisition_order_item_id=supplier_id,
        material_requisition_item_id=requisition_id,
    )
    snapshot_version = _positive_int(purpose_snapshot_version, "用途快照版本")
    plan_fingerprint = _normalize_hash(receipt_plan_fingerprint, "收料用途指纹")
    if int(snapshot.snapshot_version) != snapshot_version:
        raise PurchaseReceiptFactStaleError("采购用途版本已变化，请刷新后重试")
    if not hmac.compare_digest(snapshot.preview_fingerprint.lower(), plan_fingerprint):
        raise PurchaseReceiptFactStaleError("采购用途指纹已变化，请刷新后重试")

    material_id = _positive_int(actual_material_id, "实际材质ID")
    actual_material = db.get(Material, material_id)
    if actual_material is None or not actual_material.is_active:
        raise PurchaseReceiptFactValidationError("实际材质不存在或已停用")
    actual_code = str(actual_material.code or "").strip()
    if not actual_code:
        raise PurchaseReceiptFactValidationError("实际材质代码不能为空")
    if isinstance(source, SupplierRequisitionOrderItem):
        expected_material_id = source.material_id
        expected_code = str(source.material_code_snapshot or "").strip()
    else:
        expected_material_id = None
        expected_code = str(source.material_snapshot or "").strip()
    if not expected_code:
        raise PurchaseReceiptFactValidationError("采购来源缺少预期材质快照")
    actual_fingerprint = material_calculation_fingerprint(actual_material)
    approval = None
    if actual_code != expected_code:
        if material_variance_approval_id is None:
            raise PurchaseReceiptFactValidationError("实际材质变化必须先由独立有权限人员确认")
        approval = db.get(
            PurchaseReceiptMaterialVarianceApproval,
            _positive_int(material_variance_approval_id, "材质差异确认ID"),
        )
        if approval is None:
            raise PurchaseReceiptFactValidationError("材质差异确认事实不存在")
        variance = db.get(PurchaseReceiptMaterialVariance, approval.material_variance_id)
        if (
            variance is None
            or variance.supplier_requisition_order_item_id != supplier_id
            or variance.material_requisition_item_id != requisition_id
            or variance.purchase_purpose_source_snapshot_id != snapshot.id
            or variance.actual_material_id != material_id
            or variance.actual_material_fingerprint != actual_fingerprint
            or variance.expected_source_version != source_version
            or variance.purpose_snapshot_version != snapshot_version
            or not hmac.compare_digest(
                variance.receipt_plan_fingerprint.lower(), plan_fingerprint
            )
        ):
            raise PurchaseReceiptFactStaleError("材质差异确认与当前采购事实不匹配，请重新发起确认")
        if int(approval.confirmed_by) == actor_id:
            raise PurchaseReceiptFactValidationError("录价人不能同时确认本次材质差异")
        material_change_confirmed = True
    elif material_variance_approval_id is not None or material_change_confirmed:
        raise PurchaseReceiptFactValidationError("实际材质未变化，不能附带材质差异确认")

    normalized_price = _money(_decimal(unit_price, "最终采购单价"))
    if normalized_price <= 0:
        raise PurchaseReceiptFactValidationError("最终采购单价必须大于0")
    normalized_tax_rate = _decimal(tax_rate, "税率").quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    if normalized_tax_rate < 0 or normalized_tax_rate > 1:
        raise PurchaseReceiptFactValidationError("税率必须在0到1之间")
    normalized_currency = _normalize_currency(currency)
    normalized_price_unit = str(price_unit or "").strip().lower()
    if normalized_price_unit not in {"per_sheet", "per_square_meter"}:
        raise PurchaseReceiptFactValidationError(
            "价格单位必须是per_sheet或per_square_meter"
        )

    latest = db.scalar(
        select(PurchaseReceiptFact)
        .where(
            PurchaseReceiptFact.supplier_requisition_order_item_id == supplier_id,
            PurchaseReceiptFact.material_requisition_item_id == requisition_id,
            PurchaseReceiptFact.purchase_purpose_source_snapshot_id == snapshot.id,
        )
        .order_by(PurchaseReceiptFact.receipt_fact_version.desc())
        .limit(1)
        .with_for_update()
    )
    latest_version = int(latest.receipt_fact_version) if latest is not None else 0
    if existing is not None:
        fact_version = int(existing.receipt_fact_version)
    else:
        if expected_latest_receipt_fact_version is None:
            raise PurchaseReceiptFactStaleError("必须提交当前最终价格版本后才能新增价格事实")
        if int(expected_latest_receipt_fact_version) != latest_version:
            raise PurchaseReceiptFactStaleError("最终价格版本已变化，请刷新后重试")
        fact_version = latest_version + 1

    request_payload = _request_payload(
        supplier_requisition_order_item_id=supplier_id,
        material_requisition_item_id=requisition_id,
        source_key=snapshot.source_key,
        receipt_fact_version=fact_version,
        expected_source_version=source_version,
        purchase_purpose_source_snapshot_id=int(snapshot.id),
        purpose_snapshot_version=snapshot_version,
        receipt_plan_fingerprint=plan_fingerprint,
        actual_material_id=material_id,
        actual_material_code_snapshot=actual_code,
        actual_material_version=int(actual_material.version),
        actual_material_layer_count_snapshot=actual_material.layer_count,
        actual_material_flute_type_snapshot=(
            str(actual_material.flute_type or "").strip() or None
        ),
        actual_material_is_active_snapshot=bool(actual_material.is_active),
        actual_material_fingerprint=actual_fingerprint,
        expected_material_id=expected_material_id,
        expected_material_code_snapshot=expected_code,
        material_change_confirmed=bool(material_change_confirmed),
        material_variance_approval_id=(approval.id if approval is not None else None),
        unit_price=normalized_price,
        currency=normalized_currency,
        price_unit=normalized_price_unit,
        tax_included=bool(tax_included),
        tax_rate=normalized_tax_rate,
    )
    request_hash = canonical_purchase_receipt_hash(request_payload)
    if submitted_request_hash is not None:
        submitted_hash = _normalize_hash(submitted_request_hash, "请求哈希")
        if not hmac.compare_digest(request_hash, submitted_hash):
            raise PurchaseReceiptFactStaleError("价格事实请求内容与请求哈希不一致")

    if existing is not None:
        assert_purchase_receipt_fact_replay(
            stored_request_hash=existing.request_hash,
            stored_actor_id=existing.created_by,
            submitted_request_hash=request_hash,
            submitted_actor_id=actor_id,
        )
        return existing

    fact = PurchaseReceiptFact(
        supplier_requisition_order_item_id=supplier_id,
        material_requisition_item_id=requisition_id,
        purchase_purpose_source_snapshot_id=int(snapshot.id),
        source_key=snapshot.source_key,
        receipt_fact_version=fact_version,
        expected_source_version=source_version,
        purpose_snapshot_version=snapshot_version,
        receipt_plan_fingerprint=plan_fingerprint,
        actual_material_id=material_id,
        actual_material_code_snapshot=actual_code,
        actual_material_version=int(actual_material.version),
        actual_material_layer_count_snapshot=actual_material.layer_count,
        actual_material_flute_type_snapshot=(
            str(actual_material.flute_type or "").strip() or None
        ),
        actual_material_is_active_snapshot=bool(actual_material.is_active),
        actual_material_fingerprint=actual_fingerprint,
        expected_material_id=expected_material_id,
        expected_material_code_snapshot=expected_code,
        material_change_confirmed=bool(material_change_confirmed),
        material_variance_approval_id=(approval.id if approval is not None else None),
        unit_price=normalized_price,
        currency=normalized_currency,
        price_unit=normalized_price_unit,
        tax_included=bool(tax_included),
        tax_rate=normalized_tax_rate,
        idempotency_key=key,
        request_hash=request_hash,
        created_by=actor_id,
    )
    try:
        with db.begin_nested():
            db.add(fact)
            db.flush()
    except IntegrityError as exc:
        replay = db.scalar(
            select(PurchaseReceiptFact).where(
                PurchaseReceiptFact.idempotency_key == key
            )
        )
        if replay is None:
            raise PurchaseReceiptFactStaleError(
                "最终价格版本已被其他操作更新，请刷新后重试"
            ) from exc
        assert_purchase_receipt_fact_replay(
            stored_request_hash=replay.request_hash,
            stored_actor_id=replay.created_by,
            submitted_request_hash=request_hash,
            submitted_actor_id=actor_id,
        )
        return replay
    return fact


def create_purchase_receipt_fact(db: Session, **kwargs: Any) -> PurchaseReceiptFact:
    """Compatibility name for callers that also accept an idempotent replay."""

    return create_or_replay_purchase_receipt_fact(db, **kwargs)


_SERIALIZED_FIELDS = (
    "source_key",
    "receipt_fact_version",
    "purpose_snapshot_version",
    "receipt_plan_fingerprint",
    "actual_material_id",
    "actual_material_version",
    "actual_material_fingerprint",
    "unit_price",
    "currency",
    "price_unit",
    "tax_included",
    "tax_rate",
)


def serialize_purchase_receipt_fact(fact: Any) -> dict[str, Any]:
    """Serialize only the stable API contract keys locked for P1-81."""

    getter = fact.get if isinstance(fact, Mapping) else lambda key: getattr(fact, key)
    result: dict[str, Any] = {}
    for field in _SERIALIZED_FIELDS:
        value = getter(field)
        if isinstance(value, Decimal):
            value = format(
                value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f"
            )
        result[field] = value
    return result
