"""Pure P1-80 purchase-purpose allocation and replay guards.

This module deliberately has no database or API dependency.  Callers must load
and lock the authoritative business rows, pass the freshly calculated physical
piece demand here, then persist the returned allocation and their business audit
in one transaction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import hmac
import json
from typing import Any, Mapping, Sequence


PURCHASE_PURPOSE_CALCULATION_RULE_VERSION = "p1-80-v1"


class PurchasePurposeError(ValueError):
    """Base error that API adapters may convert to a structured response."""

    code = "purchase_purpose_error"


class PurchasePurposeValidationError(PurchasePurposeError):
    code = "purchase_purpose_invalid"


class PurchasePurposeStaleError(PurchasePurposeError):
    code = "purchase_purpose_stale"


class PurchasePurposeIdempotencyConflict(PurchasePurposeError):
    code = "purchase_purpose_idempotency_conflict"


@dataclass(frozen=True, slots=True)
class PurchasePurposeSourceDemand:
    """One stable physical source contributing pieces to a purchase line."""

    source_key: str
    customer_id: int
    effective_required_piece_qty: int


@dataclass(frozen=True, slots=True)
class PurchasePurposeSourceAllocation:
    source_key: str
    customer_id: int
    effective_required_piece_qty: int
    purchase_sheet_qty: int
    order_purpose_sheet_qty: int
    reserve_purpose_sheet_qty: int


@dataclass(frozen=True, slots=True)
class PurchasePurposeAllocation:
    customer_id: int
    purchase_sheet_qty: int
    effective_required_piece_qty: int
    yield_per_sheet: int
    authoritative_order_sheet_qty: int
    order_purpose_sheet_qty: int
    reserve_purpose_sheet_qty: int
    calculation_rule_version: str
    source_allocations: tuple[PurchasePurposeSourceAllocation, ...]


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PurchasePurposeValidationError("用途载荷不能包含非有限数字")
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            return int(normalized)
        return format(normalized, "f")
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise PurchasePurposeValidationError("用途载荷不能包含非有限数字")
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
    raise PurchasePurposeValidationError(
        f"用途载荷包含不支持的类型：{type(value).__name__}"
    )


def canonical_purchase_purpose_json(payload: Any) -> str:
    """Return stable UTF-8 JSON for preview fingerprints and request hashes."""

    return json.dumps(
        _canonical_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_purchase_purpose_hash(payload: Any) -> str:
    canonical = canonical_purchase_purpose_json(payload).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def authoritative_order_sheet_quantity(
    effective_required_piece_qty: int,
    yield_per_sheet: int,
) -> int:
    pieces = _require_nonnegative_int(
        effective_required_piece_qty, "权威有效需求片数"
    )
    sheet_yield = _require_positive_int(yield_per_sheet, "一张产出数量")
    return (pieces + sheet_yield - 1) // sheet_yield


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise PurchasePurposeValidationError(f"{label}必须是非负整数")
    try:
        decimal_value = Decimal(str(value))
    except Exception as exc:
        raise PurchasePurposeValidationError(f"{label}必须是非负整数") from exc
    if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value():
        raise PurchasePurposeValidationError(f"{label}必须是非负整数")
    integer = int(decimal_value)
    if integer < 0:
        raise PurchasePurposeValidationError(f"{label}必须是非负整数")
    return integer


def _require_positive_int(value: Any, label: str) -> int:
    integer = _require_nonnegative_int(value, label)
    if integer <= 0:
        raise PurchasePurposeValidationError(f"{label}必须是正整数")
    return integer


def _allocate_integer_total(
    total: int,
    sources: Sequence[PurchasePurposeSourceDemand],
) -> list[int]:
    """Largest-remainder allocation with source_key as the stable tie breaker."""

    if not sources:
        return []
    if total == 0:
        return [0 for _ in sources]
    weights = [row.effective_required_piece_qty for row in sources]
    weight_sum = sum(weights)
    if weight_sum <= 0:
        result = [0 for _ in sources]
        result[0] = total
        return result
    numerators = [total * weight for weight in weights]
    floors = [value // weight_sum for value in numerators]
    remainder = total - sum(floors)
    priority = sorted(
        range(len(sources)),
        key=lambda index: (
            -(numerators[index] % weight_sum),
            sources[index].source_key,
        ),
    )
    for index in priority[:remainder]:
        floors[index] += 1
    return floors


def allocate_purchase_purpose(
    *,
    purchase_sheet_qty: int,
    yield_per_sheet: int,
    source_demands: Sequence[PurchasePurposeSourceDemand],
    order_purpose_sheet_qty: int | None = None,
    reserve_purpose_sheet_qty: int | None = None,
    authoritative_order_sheet_qty_override: int | None = None,
    allow_implicit_reserve: bool = True,
    calculation_rule_version: str = PURCHASE_PURPOSE_CALCULATION_RULE_VERSION,
) -> PurchasePurposeAllocation:
    """Validate and allocate one formal purchase line across physical sources.

    The authoritative sheet need is calculated once after summing every source's
    effective piece demand.  This prevents two 10-piece sources at one-sheet-out-3
    from being rounded independently to 4 + 4 instead of 7.

    ``allow_implicit_reserve=False`` is the old-client gate: a payload that omits
    both purpose quantities may only proceed when it has no explicit over-order.
    Preview callers normally keep the default and show the calculated reserve.
    """

    purchase = _require_positive_int(purchase_sheet_qty, "本次采购张数")
    sheet_yield = _require_positive_int(yield_per_sheet, "一张产出数量")
    rule_version = str(calculation_rule_version or "").strip()
    if not rule_version:
        raise PurchasePurposeValidationError("用途计算规则版本不能为空")
    if not source_demands:
        raise PurchasePurposeValidationError("采购用途至少需要一个物理来源")

    normalized_sources: list[PurchasePurposeSourceDemand] = []
    seen_source_keys: set[str] = set()
    for raw in source_demands:
        source_key = str(raw.source_key or "").strip()
        if not source_key:
            raise PurchasePurposeValidationError("物理来源键不能为空")
        if source_key in seen_source_keys:
            raise PurchasePurposeValidationError("同一物理来源不能重复分配采购用途")
        seen_source_keys.add(source_key)
        customer_id = _require_positive_int(raw.customer_id, "客户ID")
        effective = _require_nonnegative_int(
            raw.effective_required_piece_qty, "物理来源有效需求片数"
        )
        normalized_sources.append(
            PurchasePurposeSourceDemand(source_key, customer_id, effective)
        )
    normalized_sources.sort(key=lambda row: row.source_key)
    customer_ids = {row.customer_id for row in normalized_sources}
    if len(customer_ids) != 1:
        raise PurchasePurposeValidationError("一个采购用途分配组只能属于同一客户")

    effective_total = sum(
        row.effective_required_piece_qty for row in normalized_sources
    )
    if effective_total <= 0 and authoritative_order_sheet_qty_override is None:
        raise PurchasePurposeValidationError("权威有效需求片数必须大于0")
    minimum_authoritative = (
        authoritative_order_sheet_quantity(effective_total, sheet_yield)
        if effective_total > 0
        else 0
    )
    if authoritative_order_sheet_qty_override is None:
        authoritative = minimum_authoritative
    else:
        authoritative = _require_nonnegative_int(
            authoritative_order_sheet_qty_override,
            "权威订单用途张数",
        )
        if authoritative < minimum_authoritative:
            raise PurchasePurposeValidationError(
                "权威订单用途张数不能小于有效需求的最小换算张数"
            )

    omitted_both = (
        order_purpose_sheet_qty is None and reserve_purpose_sheet_qty is None
    )
    if omitted_both:
        if not allow_implicit_reserve and purchase > authoritative:
            raise PurchasePurposeValidationError(
                "旧客户端未提供采购用途字段，采购总量超过权威订单用途，禁止静默形成备库"
            )
        order_purpose = min(purchase, authoritative)
        reserve_purpose = purchase - order_purpose
    elif order_purpose_sheet_qty is None:
        reserve_purpose = _require_nonnegative_int(
            reserve_purpose_sheet_qty, "客户通用片料备库张数"
        )
        order_purpose = purchase - reserve_purpose
    elif reserve_purpose_sheet_qty is None:
        order_purpose = _require_nonnegative_int(
            order_purpose_sheet_qty, "订单生产用途张数"
        )
        reserve_purpose = purchase - order_purpose
    else:
        order_purpose = _require_nonnegative_int(
            order_purpose_sheet_qty, "订单生产用途张数"
        )
        reserve_purpose = _require_nonnegative_int(
            reserve_purpose_sheet_qty, "客户通用片料备库张数"
        )

    if order_purpose < 0 or reserve_purpose < 0:
        raise PurchasePurposeValidationError("采购用途数量不能为负数")
    if order_purpose + reserve_purpose != purchase:
        raise PurchasePurposeValidationError(
            "订单用途数量与备库用途数量合计必须等于本次采购张数"
        )
    if order_purpose > authoritative:
        raise PurchasePurposeValidationError(
            "订单生产用途张数不能超过权威订单所需纸板张数"
        )

    order_allocations = _allocate_integer_total(order_purpose, normalized_sources)
    reserve_allocations = _allocate_integer_total(reserve_purpose, normalized_sources)
    source_allocations = tuple(
        PurchasePurposeSourceAllocation(
            source_key=source.source_key,
            customer_id=source.customer_id,
            effective_required_piece_qty=source.effective_required_piece_qty,
            purchase_sheet_qty=order_allocations[index]
            + reserve_allocations[index],
            order_purpose_sheet_qty=order_allocations[index],
            reserve_purpose_sheet_qty=reserve_allocations[index],
        )
        for index, source in enumerate(normalized_sources)
    )
    return PurchasePurposeAllocation(
        customer_id=next(iter(customer_ids)),
        purchase_sheet_qty=purchase,
        effective_required_piece_qty=effective_total,
        yield_per_sheet=sheet_yield,
        authoritative_order_sheet_qty=authoritative,
        order_purpose_sheet_qty=order_purpose,
        reserve_purpose_sheet_qty=reserve_purpose,
        calculation_rule_version=rule_version,
        source_allocations=source_allocations,
    )


def assert_purchase_purpose_stale_token(
    submitted_fingerprint: str | None,
    current_fingerprint: str,
) -> None:
    submitted = str(submitted_fingerprint or "").strip().lower()
    current = str(current_fingerprint or "").strip().lower()
    if len(current) != 64:
        raise PurchasePurposeValidationError("当前采购用途指纹无效")
    if len(submitted) != 64 or not hmac.compare_digest(submitted, current):
        raise PurchasePurposeStaleError("报料需求已变化，请刷新采购用途草稿后重试")


def assert_purchase_purpose_replay(
    *,
    stored_request_hash: str | None,
    stored_actor_id: int | None,
    submitted_request_hash: str,
    submitted_actor_id: int,
) -> None:
    stored_hash = str(stored_request_hash or "").strip().lower()
    submitted_hash = str(submitted_request_hash or "").strip().lower()
    if stored_actor_id is None or int(stored_actor_id) != int(submitted_actor_id):
        raise PurchasePurposeIdempotencyConflict(
            "同一幂等键不能由不同操作者重放"
        )
    if (
        len(stored_hash) != 64
        or len(submitted_hash) != 64
        or not hmac.compare_digest(stored_hash, submitted_hash)
    ):
        raise PurchasePurposeIdempotencyConflict(
            "同一幂等键不能提交不同采购用途载荷"
        )


_SNAPSHOT_SERIALIZED_FIELDS = (
    "id",
    "snapshot_key",
    "allocation_group_key",
    "supplier_requisition_order_item_id",
    "material_requisition_item_id",
    "source_kind",
    "source_key",
    "source_order_item_id",
    "source_requisition_item_id",
    "source_bom_requisition_source_id",
    "customer_id",
    "customer_name_snapshot",
    "component_type",
    "source_finished_qty_snapshot",
    "pieces_per_finished_snapshot",
    "source_required_piece_qty_snapshot",
    "source_semi_reserved_piece_qty_snapshot",
    "source_effective_piece_qty_snapshot",
    "yield_per_sheet_snapshot",
    "group_effective_piece_qty_snapshot",
    "group_authoritative_order_sheet_qty_snapshot",
    "purchase_sheet_qty",
    "order_purpose_sheet_qty",
    "reserve_purpose_sheet_qty",
    "calculation_rule_version",
    "snapshot_version",
    "preview_fingerprint",
    "request_hash",
    "created_by",
    "created_at",
)


def serialize_purchase_purpose_snapshot(snapshot: Any | None) -> dict[str, Any]:
    """Serialize one immutable row; missing rows are explicit legacy/unset facts."""

    if snapshot is None:
        return {"purpose_status": "legacy_unset", "snapshot_version": None}
    if isinstance(snapshot, Mapping):
        getter = snapshot.get
    else:
        getter = lambda name: getattr(snapshot, name, None)
    result: dict[str, Any] = {"purpose_status": "frozen"}
    for field in _SNAPSHOT_SERIALIZED_FIELDS:
        value = getter(field)
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = int(value) if value == value.to_integral_value() else float(value)
        result[field] = value
    return result


def serialize_purchase_purpose_allocation(
    allocation: PurchasePurposeAllocation,
) -> dict[str, Any]:
    payload = asdict(allocation)
    payload["purpose_status"] = "preview"
    return payload
