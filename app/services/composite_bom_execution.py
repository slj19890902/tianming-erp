"""Pure calculation helpers for composite-product BOM execution.

This module deliberately knows nothing about SQLAlchemy or HTTP.  Callers pass
immutable order/BOM snapshots, adjustment events, and inventory query results
as mappings, then persist any accepted plan in their own transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from math import ceil
from typing import Any


COMPLETION_DESTINATION_DIRECT_KIT = "direct_kit"
COMPLETION_DESTINATION_STOCK = "stock"
COMPLETION_DESTINATIONS = frozenset(
    {COMPLETION_DESTINATION_DIRECT_KIT, COMPLETION_DESTINATION_STOCK}
)


class CompositeBOMExecutionError(ValueError):
    """Raised when a pure composite-BOM execution input is invalid."""


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise CompositeBOMExecutionError(f"{label}必须是整数")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CompositeBOMExecutionError(f"{label}无效") from error
    if not result.is_finite():
        raise CompositeBOMExecutionError(f"{label}无效")
    return result


def as_integer(value: Any, *, label: str, minimum: int | None = None) -> int:
    """Return an integer, accepting Decimal input but rejecting fractions."""

    result = _decimal(value, label=label)
    if result != result.to_integral_value():
        raise CompositeBOMExecutionError(f"{label}必须是整数")
    integer = int(result)
    if minimum is not None and integer < minimum:
        operator = "大于0" if minimum == 1 else f"大于等于{minimum}"
        raise CompositeBOMExecutionError(f"{label}必须{operator}")
    return integer


def require_positive_integer(value: Any, *, label: str = "数量") -> int:
    """Validate a positive whole-number business quantity."""

    return as_integer(value, label=label, minimum=1)


def _component_value(component: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in component:
            return component[name]
    return default


def component_signature(component: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """Return the stable aggregation key for one immutable component snapshot.

    The signature intentionally includes product identity and snapshot
    specification/material fields, so components with different specifications
    cannot be merged accidentally even when their displayed names match.
    """

    component_id = _component_value(component, "component_product_id", "product_id", "id")
    product_code = _component_value(
        component,
        "component_product_code",
        "snapshot_component_product_code",
        "product_code",
    )
    internal_code = _component_value(component, "internal_component_code", "component_code")
    if component_id is None and not product_code and not internal_code:
        raise CompositeBOMExecutionError("组件必须提供组件ID、产品编码或内部组件编码")

    fields = (
        ("component_product_id", component_id),
        ("product_code", product_code),
        ("internal_component_code", internal_code),
        (
            "specification",
            _component_value(
                component,
                "specification",
                "snapshot_component_spec",
                "specification_snapshot",
            ),
        ),
        ("length_mm", component.get("length_mm")),
        ("width_mm", component.get("width_mm")),
        ("height_mm", component.get("height_mm")),
        (
            "material",
            _component_value(
                component,
                "material",
                "snapshot_component_material",
                "material_snapshot",
            ),
        ),
        ("is_die_cut", bool(component.get("is_die_cut", False))),
        (
            "mold_tool_id",
            _component_value(
                component,
                "mold_tool_id",
                "snapshot_mold_tool_id",
            ),
        ),
    )
    return tuple((name, None if value is None else str(value)) for name, value in fields)


def _component_name(component: Mapping[str, Any]) -> str:
    return str(
        _component_value(
            component,
            "component_name",
            "snapshot_component_product_name",
            "product_name",
            "component_product_name",
            "internal_component_code",
            "component_code",
            default="未命名组件",
        )
    )


def validate_completion_destination(destination: Any) -> str:
    """Validate the explicit completion destination required by Phase B."""

    if destination not in COMPLETION_DESTINATIONS:
        raise CompositeBOMExecutionError("完工去向必须是direct_kit或stock")
    return str(destination)


def calculate_effective_order_sets(
    snapshot_sets: Any,
    adjustments: Iterable[Mapping[str, Any]] = (),
    *,
    processed_sets: Any = 0,
) -> int:
    """Apply immutable adjustment events without mutating the source snapshot."""

    original = require_positive_integer(snapshot_sets, label="原订单套数")
    processed = as_integer(processed_sets, label="已处理套数", minimum=0)
    delta = 0
    for index, event in enumerate(adjustments, start=1):
        raw_delta = _component_value(
            event,
            "delta_sets",
            "delta_order_set_quantity",
            "quantity_delta",
            "adjustment_sets",
        )
        if raw_delta is None:
            raise CompositeBOMExecutionError(f"第{index}条订单调整缺少套数变更")
        delta += as_integer(raw_delta, label=f"第{index}条订单调整套数")
    effective = original + delta
    if effective < processed:
        raise CompositeBOMExecutionError("订单调整后的有效套数不能小于已处理套数")
    if effective < 0:
        raise CompositeBOMExecutionError("订单调整后的有效套数不能小于0")
    return effective


def validate_order_adjustment(
    snapshot_sets: Any,
    adjustment_event: Mapping[str, Any],
    *,
    prior_adjustments: Iterable[Mapping[str, Any]] = (),
    processed_sets: Any = 0,
) -> int:
    """Validate one proposed immutable adjustment and return its resulting sets."""

    return calculate_effective_order_sets(
        snapshot_sets,
        (*tuple(prior_adjustments), adjustment_event),
        processed_sets=processed_sets,
    )


def calculate_effective_component_demands(
    snapshot_sets: Any,
    components: Iterable[Mapping[str, Any]],
    adjustments: Iterable[Mapping[str, Any]] = (),
    *,
    processed_sets: Any = 0,
) -> dict[str, Any]:
    """Return effective order sets and component demand, grouped by signature."""

    effective_sets = calculate_effective_order_sets(
        snapshot_sets, adjustments, processed_sets=processed_sets
    )
    grouped: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
    for index, component in enumerate(components, start=1):
        quantity_per_set = require_positive_integer(
            _component_value(component, "quantity_per_set", "qty_per_set"),
            label=f"第{index}个组件每套用量",
        )
        signature = component_signature(component)
        row = grouped.get(signature)
        if row is None:
            row = {
                "signature": signature,
                "component": dict(component),
                "component_name": _component_name(component),
                "quantity_per_set": quantity_per_set,
                "is_required": bool(component.get("is_required", True)),
                "demand_quantity": 0,
            }
            grouped[signature] = row
        else:
            # Exact snapshot signatures represent the same physical component;
            # aggregate their per-set quantities before deriving kit capacity.
            row["quantity_per_set"] += quantity_per_set
            row["is_required"] = row["is_required"] or bool(
                component.get("is_required", True)
            )
        row["demand_quantity"] += effective_sets * quantity_per_set
    return {"effective_sets": effective_sets, "components": list(grouped.values())}


def calculate_requisition(
    component_demand: Mapping[str, Any],
    *,
    actual_yield_per_sheet: Any | None = None,
) -> dict[str, Any]:
    """Calculate purchase sheets using only a fixed spare-sheet quantity."""

    component = component_demand.get("component", component_demand)
    if not isinstance(component, Mapping):
        raise CompositeBOMExecutionError("组件报料输入无效")
    demand = as_integer(
        _component_value(component_demand, "demand_quantity", "component_demand"),
        label="组件需求",
        minimum=0,
    )
    is_die_cut = bool(component.get("is_die_cut", False))
    if is_die_cut:
        yield_value = actual_yield_per_sheet
        if yield_value is None:
            yield_value = _component_value(
                component,
                "snapshot_max_yield_per_sheet",
                "mold_max_yield_per_sheet",
                "max_yield_per_sheet",
            )
        yield_per_sheet = require_positive_integer(yield_value, label="模切出数")
    else:
        yield_per_sheet = 1
    spare_sheets = as_integer(
        _component_value(component, "spare_sheet_quantity", "fixed_spare_sheets", default=0),
        label="固定加放片数",
        minimum=0,
    )
    net_sheets = ceil(demand / yield_per_sheet)
    return {
        "signature": component_demand.get("signature", component_signature(component)),
        "component_name": component_demand.get("component_name", _component_name(component)),
        "demand_quantity": demand,
        "yield_per_sheet": yield_per_sheet,
        "net_sheets": net_sheets,
        "spare_sheets": spare_sheets,
        "purchase_sheets": net_sheets + spare_sheets,
    }


def calculate_requisitions(
    effective_demands: Mapping[str, Any],
    *,
    actual_yields_by_signature: Mapping[Any, Any] | None = None,
) -> list[dict[str, Any]]:
    """Calculate requisition rows; no percentage-loss estimate is introduced."""

    actual_yields_by_signature = actual_yields_by_signature or {}
    rows: list[dict[str, Any]] = []
    for demand in effective_demands.get("components", ()):
        signature = demand["signature"]
        actual = actual_yields_by_signature.get(signature)
        rows.append(calculate_requisition(demand, actual_yield_per_sheet=actual))
    return rows


def _available_for(component: Mapping[str, Any], signature: Any, available_quantities: Mapping[Any, Any]) -> int:
    candidates = (
        signature,
        component.get("component_product_id"),
        component.get("product_id"),
        component.get("id"),
        component.get("internal_component_code"),
        component.get("component_code"),
        component.get("product_code"),
    )
    for candidate in candidates:
        if candidate is not None and candidate in available_quantities:
            return as_integer(available_quantities[candidate], label="组件可用量", minimum=0)
    return 0


def calculate_kit_availability(
    effective_demands: Mapping[str, Any], available_quantities: Mapping[Any, Any]
) -> dict[str, Any]:
    """Calculate availability, shortages, and the maximum complete-kit count."""

    rows: list[dict[str, Any]] = []
    required_capacities: list[int] = []
    for demand in effective_demands.get("components", ()):
        component = demand["component"]
        available = _available_for(component, demand["signature"], available_quantities)
        demand_quantity = demand["demand_quantity"]
        shortage = max(demand_quantity - available, 0)
        quantity_per_set = demand["quantity_per_set"]
        required = bool(demand["is_required"])
        if required:
            required_capacities.append(available // quantity_per_set)
        rows.append(
            {
                **demand,
                "available_quantity": available,
                "shortage_quantity": shortage,
                "status": (
                    "sufficient" if shortage == 0 else "shortage"
                    if required else "optional_shortage"
                ),
            }
        )
    return {
        "components": rows,
        "max_complete_sets": min(required_capacities) if required_capacities else 0,
        "is_kit_complete": all(
            row["shortage_quantity"] == 0 for row in rows if row["is_required"]
        ),
    }


def format_missing_component_message(availability: Mapping[str, Any]) -> str:
    """Create an explicit Chinese shortage prompt for required components."""

    shortages = [
        row for row in availability.get("components", ())
        if row["is_required"] and row["shortage_quantity"] > 0
    ]
    if not shortages:
        return "必需组件齐套，可执行。"
    details = "；".join(
        f"【{row['component_name']}】缺{row['shortage_quantity']}个（需求{row['demand_quantity']}个，可用{row['available_quantity']}个）"
        for row in shortages
    )
    return f"必需组件缺件，无法齐套：{details}。"


def build_delivery_consumption_plan(
    delivery_sets: Any,
    components: Iterable[Mapping[str, Any]],
    available_quantities: Mapping[Any, Any],
) -> dict[str, Any]:
    """Build an all-or-nothing component consumption plan for delivery of N sets."""

    sets = require_positive_integer(delivery_sets, label="送货套数")
    effective_demands = calculate_effective_component_demands(sets, components)
    availability = calculate_kit_availability(effective_demands, available_quantities)
    executable = bool(availability["is_kit_complete"])
    plan: list[dict[str, Any]] = []
    for row in availability["components"]:
        consume = row["demand_quantity"]
        if not executable or (not row["is_required"] and row["shortage_quantity"] > 0):
            consume = 0
        plan.append(
            {
                "signature": row["signature"],
                "component_name": row["component_name"],
                "is_required": row["is_required"],
                "required_quantity": row["demand_quantity"],
                "planned_consumption_quantity": consume,
                "available_quantity": row["available_quantity"],
                "shortage_quantity": row["shortage_quantity"],
            }
        )
    return {
        "delivery_sets": sets,
        "executable": executable,
        "consumption_plan": plan,
        "missing_message": format_missing_component_message(availability),
        "availability": availability,
    }
