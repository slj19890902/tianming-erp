from __future__ import annotations

import hashlib
import json
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.production import ProductionTask
from app.models.supplier_requisition_order import SupplierRequisitionOrder
from app.services.requisition_production_print import (
    build_supplier_requisition_production_package,
)


class ProductionPackagingLabelError(ValueError):
    pass


def _positive_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def build_supplier_requisition_packaging_label_package(
    db: Session,
    order: SupplierRequisitionOrder,
) -> dict:
    """Project immutable ProductionTask label snapshots into a read-only package.

    A supplier order can contain cover/base rows or repeated requisition sources
    that point to the same production task.  The task id is therefore the only
    deduplication key; procurement sheet quantities are deliberately ignored.
    """

    production_package = build_supplier_requisition_production_package(db, order)
    task_ids = {
        int(component["production_task_id"])
        for card in production_package["cards"]
        for component in card.get("components", [])
        if component.get("production_task_id") is not None
    }
    tasks = {
        int(task.id): task
        for task in (
            db.scalars(
                select(ProductionTask).where(ProductionTask.id.in_(task_ids))
            ).all()
            if task_ids
            else []
        )
    }

    task_sources: dict[int, tuple[dict, dict]] = {}
    for card in production_package["cards"]:
        for component in card.get("components", []):
            task_id = component.get("production_task_id")
            if task_id is None:
                continue
            task_sources.setdefault(int(task_id), (card, component))

    customer_ids = {
        int(card["customer_id"])
        for card, _component in task_sources.values()
        if card.get("customer_id") is not None
    }
    customer_codes = {
        int(customer.id): customer.customer_code
        for customer in (
            db.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
            if customer_ids
            else []
        )
    }

    plans: list[dict] = []
    package_review_messages = list(production_package.get("review_messages") or [])
    for task_id in sorted(task_sources):
        task = tasks.get(task_id)
        if task is None:
            package_review_messages.append(f"生产任务 #{task_id} 不存在，请核对")
            continue
        if not bool(task.production_label_enabled_snapshot):
            continue

        units_per_label = _positive_int(task.production_label_units_per_label_snapshot)
        total_quantity = _positive_int(task.production_label_total_quantity_snapshot)
        frozen_count = _positive_int(task.production_label_count_snapshot)
        expected_count = ceil(total_quantity / units_per_label) if units_per_label else 0
        if not units_per_label or not total_quantity or frozen_count != expected_count:
            package_review_messages.append(
                f"生产任务 #{task_id} 的包装标签快照不完整，请核对"
            )
            continue

        card, component = task_sources[task_id]
        quantities = [
            min(units_per_label, total_quantity - index * units_per_label)
            for index in range(frozen_count)
        ]
        if not quantities or any(quantity <= 0 for quantity in quantities):
            package_review_messages.append(
                f"生产任务 #{task_id} 的包装标签数量异常，请核对"
            )
            continue
        plans.append(
            {
                "production_task_id": task_id,
                "production_task_version": int(task.version or 1),
                "customer_id": card.get("customer_id"),
                "customer_name": card.get("customer_name"),
                "customer_code": customer_codes.get(int(card["customer_id"]))
                if card.get("customer_id") is not None
                else None,
                "product_code": component.get("product_code") or card.get("product_code"),
                "product_name": component.get("product_name") or card.get("product_name"),
                "specification": component.get("specification")
                or " / ".join(card.get("specifications") or []),
                "order_numbers": list(card.get("order_numbers") or []),
                "customer_pos": list(card.get("customer_pos") or []),
                "total_quantity": total_quantity,
                "units_per_label": units_per_label,
                "label_count": frozen_count,
                "label_quantities": quantities,
            }
        )

    labels: list[dict] = []
    for plan in plans:
        for index, quantity in enumerate(plan["label_quantities"], start=1):
            labels.append(
                {
                    "production_task_id": plan["production_task_id"],
                    "production_task_version": plan["production_task_version"],
                    "customer_id": plan["customer_id"],
                    "customer_name": plan["customer_name"],
                    "customer_code": plan["customer_code"],
                    "product_code": plan["product_code"],
                    "product_name": plan["product_name"],
                    "specification": plan["specification"],
                    "order_numbers": plan["order_numbers"],
                    "customer_pos": plan["customer_pos"],
                    "quantity": quantity,
                    "total_quantity": plan["total_quantity"],
                    "units_per_label": plan["units_per_label"],
                    "label_number": index,
                    "label_count": plan["label_count"],
                }
            )

    fingerprint_payload = {
        "supplier_order_id": int(order.id),
        "supplier_order_number": order.order_number,
        "plans": plans,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    review_messages = list(dict.fromkeys(str(value) for value in package_review_messages if value))
    return {
        "supplier_order_id": int(order.id),
        "supplier_order_number": order.order_number,
        "status": order.status,
        "status_label": "生产包装标签｜非库存标签",
        "plan_fingerprint": fingerprint,
        "production_task_count": len(plans),
        "label_count": len(labels),
        "review_required": bool(review_messages),
        "review_messages": review_messages,
        "printable": bool(labels) and not review_messages,
        "plans": plans,
        "labels": labels,
    }
