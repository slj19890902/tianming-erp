from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.order import OrderItem
from app.services.composite_bom_workflow import (
    delivered_component_quantities,
    delivery_component_required_quantities,
    delivery_item_component_quantities,
    delivery_component_demands,
)


def customer_document_fulfillment_mode(
    *,
    frozen_order_mode: str | None,
    current_product_mode: str | None,
    component_lines: list[dict] | None = None,
) -> str:
    """Resolve the one-way customer-facing composite delivery mode."""

    frozen_modes = {row["bom_delivery_mode"] for row in component_lines or [] if row.get("bom_delivery_mode")}
    if frozen_modes:
        if len(frozen_modes) != 1 or not frozen_modes <= {"parent_delivery", "component_delivery"}:
            from app.services.multilevel_bom_plan import BomPlanError
            raise BomPlanError("送货明细冻结交付规则不一致")
        return next(iter(frozen_modes))
    if "parent_delivery" in {frozen_order_mode, current_product_mode}:
        return "parent_delivery"
    return "component_delivery"


def delivery_component_lines(
    db: Session,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int | None = None,
    dispatched: bool = False,
) -> list[dict]:
    """Project the BOM component quantities used by a delivery document."""

    if dispatched and delivery_item_id is not None:
        from app.services.multilevel_bom_delivery_history import historical_delivery_component_demands
        demands = historical_delivery_component_demands(db, delivery_item_id=delivery_item_id, order_item_id=order_item.id)
    else:
        demands = delivery_component_demands(db, order_item.id)
    if not demands:
        return []
    cumulative = delivered_component_quantities(db, order_item.id)
    document_quantities = (
        delivery_item_component_quantities(db, delivery_item_id)
        if dispatched and delivery_item_id is not None
        else delivery_component_required_quantities(
            db,
            order_item_id=order_item.id,
            delivery_sets=max(int(planned_delivery_quantity or 0), 0),
        )
    )
    return [
        {
            "line_type": "component",
            "component_snapshot_id": demand.snapshot_id,
            "component_product_id": demand.component_product_id,
            "product_code": demand.component_code,
            "product_name": demand.component_name,
            "specification": demand.specification,
            "unit": demand.unit,
            **({"is_graph_root": True} if demand.is_graph_root else {}),
            **({"bom_delivery_mode": demand.frozen_delivery_mode} if demand.frozen_delivery_mode else {}),
            "quantity_per_set": demand.quantity_per_set,
            "target_quantity": demand.required_piece_quantity,
            "delivered_quantity": cumulative.get(demand.snapshot_id, 0),
            "remaining_quantity": max(
                demand.required_piece_quantity
                - cumulative.get(demand.snapshot_id, 0),
                0,
            ),
            "planned_delivery_quantity": document_quantities.get(
                demand.snapshot_id,
                0,
            ),
            "pricing_included": False,
            "show_on_delivery": bool(demand.show_on_delivery),
            "pricing_note": "套内组件，不单独计价",
            "independent_return_receipt": False,
            "independent_statement": False,
        }
        for demand in demands
    ]


def actual_goods_lines(
    *,
    order_item_id: int,
    product_code: str | None,
    product_name: str | None,
    specification: str | None,
    parent_quantity: int,
    component_lines: list[dict],
    fulfillment_mode: str = "component_delivery",
    source_delivery_item_id: int | None = None,
    parent_product_id: int | None = None,
    include_internal_ids: bool = False,
) -> list[dict]:
    """Project one delivery item into the goods identities seen by the customer."""

    if fulfillment_mode == "component_delivery":
        rows = [
            {
                "line_type": "component",
                "order_item_id": order_item_id,
                "component_snapshot_id": component["component_snapshot_id"],
                "product_code": component["product_code"],
                "product_name": component["product_name"],
                "specification": component["specification"],
                "unit": component["unit"],
                "quantity": component["planned_delivery_quantity"],
                "pricing_included": False,
                "independent_return_receipt": False,
                "independent_statement": False,
            }
            for component in component_lines
            if int(component["planned_delivery_quantity"] or 0) > 0
        ]
        if include_internal_ids:
            by_snapshot = {
                int(component["component_snapshot_id"]): component
                for component in component_lines
            }
            for row in rows:
                component = by_snapshot[int(row["component_snapshot_id"])]
                row.update(
                    {
                        "source_delivery_item_id": source_delivery_item_id,
                        "projected_product_id": component.get(
                            "component_product_id"
                        ),
                        "fulfillment_mode": "component_delivery",
                    }
                )
        return rows

    row = {
        "line_type": "parent",
        "order_item_id": order_item_id,
        "component_snapshot_id": None,
        "product_code": product_code,
        "product_name": product_name,
        "specification": specification,
        "unit": next((component["unit"] for component in component_lines
                      if component.get("is_graph_root")), "PCS"),
        "quantity": max(int(parent_quantity or 0), 0),
        "pricing_included": True,
        "independent_return_receipt": True,
        "independent_statement": True,
    }
    if include_internal_ids:
        row.update(
            {
                "source_delivery_item_id": source_delivery_item_id,
                "projected_product_id": parent_product_id,
                "fulfillment_mode": "parent_delivery",
            }
        )
    return [row]
