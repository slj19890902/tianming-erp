from __future__ import annotations

import hashlib
import json
from math import ceil
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionTask
from app.models.supplier_requisition_order import SupplierRequisitionOrder
from app.services.requisition_production_print import (
    build_supplier_requisition_production_package,
)
from app.services.production_label_strategy import (
    CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION,
)
from app.services.production_packaging_label_layout import effective_layout


class ProductionPackagingLabelError(ValueError):
    pass


ALLOWED_TEMPLATE_VERSIONS = frozenset(
    {
        "legacy_65x45_v1",
        "current_40x30_v1",
        CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION,
    }
)

def _customer_label_fields(
    template_version: str,
    customer_short_name: object,
) -> dict[str, str]:
    # Adding fields to a legacy plan would change the frozen fingerprint and
    # break immutable historical reprints.  Only v2 carries the manually
    # maintained customer short name.
    if template_version != CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION:
        return {}
    short_name = str(customer_short_name or "").strip()
    if (
        not short_name
        or len(short_name) > 30
        or not re.search(r"[\u3400-\u9fff]", short_name)
    ):
        raise ProductionPackagingLabelError(
            "客户资料未填写有效的标签中文简称，请先到客户资料补录"
        )
    return {"customer_short_name": short_name}


def template_dimensions(template_version: str) -> dict[str, int]:
    if template_version == "legacy_65x45_v1":
        return {"width_mm": 65, "height_mm": 45}
    if template_version in {
        "current_40x30_v1",
        CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION,
    }:
        return {"width_mm": 40, "height_mm": 30}
    raise ProductionPackagingLabelError("生产包装标签模板版本不受支持")


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
    customers_by_id = {
        int(customer.id): customer
        for customer in (
            db.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
            if customer_ids
            else []
        )
    }

    plans: list[dict] = []
    template_versions: set[str] = set()
    # An explicitly refreshed label snapshot is allowed to be newer than the
    # requisition card.  That production-card warning must not make the frozen
    # label plan unprintable; every other review reason remains fail-closed.
    package_review_messages = [
        message
        for message in (production_package.get("review_messages") or [])
        if str(message) != "生产任务版本已变化，请核对并重打"
    ]
    for task_id in sorted(task_sources):
        task = tasks.get(task_id)
        if task is None:
            package_review_messages.append(f"生产任务 #{task_id} 不存在，请核对")
            continue
        if not bool(task.production_label_enabled_snapshot):
            item = db.get(OrderItem, task.order_item_id)
            component_snapshot = (
                db.get(
                    SalesOrderItemBomComponent,
                    task.sales_order_item_bom_component_id,
                )
                if task.sales_order_item_bom_component_id is not None
                else None
            )
            product_id = (
                int(component_snapshot.component_product_id)
                if component_snapshot is not None
                else int(item.product_id)
                if item is not None
                else None
            )
            product = db.get(Product, product_id) if product_id is not None else None
            if (
                product is not None
                and bool(product.production_label_enabled)
                and task.status in {"waiting_material", "pending"}
                and task.production_label_template_version_snapshot
                == CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
                and task.production_label_product_version_snapshot
                == int(product.version)
            ):
                package_review_messages.append(
                    f"生产任务 #{task_id} 的当前产品已启用标签策略，"
                    "但任务快照未启用；请停止打印并核对任务创建链路"
                )
            continue

        template_version = str(
            task.production_label_template_version_snapshot or ""
        ).strip()
        if template_version not in ALLOWED_TEMPLATE_VERSIONS:
            package_review_messages.append(
                f"生产任务 #{task_id} 的包装标签模板版本不受支持，请核对"
            )
            continue
        template_versions.add(template_version)

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
        item = db.get(OrderItem, task.order_item_id)
        component_snapshot = (
            db.get(
                SalesOrderItemBomComponent,
                task.sales_order_item_bom_component_id,
            )
            if task.sales_order_item_bom_component_id is not None
            else None
        )
        product_id = (
            int(component_snapshot.component_product_id)
            if component_snapshot is not None
            else int(item.product_id)
            if item is not None
            else None
        )
        quantities = [
            min(units_per_label, total_quantity - index * units_per_label)
            for index in range(frozen_count)
        ]
        if not quantities or any(quantity <= 0 for quantity in quantities):
            package_review_messages.append(
                f"生产任务 #{task_id} 的包装标签数量异常，请核对"
            )
            continue
        customer_name = card.get("customer_name")
        customer = (
            customers_by_id.get(int(card["customer_id"]))
            if card.get("customer_id") is not None
            else None
        )
        try:
            customer_label_fields = _customer_label_fields(
                template_version,
                customer.chinese_short_name if customer is not None else None,
            )
        except ProductionPackagingLabelError as exc:
            package_review_messages.append(
                f"生产任务 #{task_id} 的{exc}"
            )
            continue
        plans.append(
            {
                "production_task_id": task_id,
                "production_task_version": int(task.version or 1),
                "product_id": product_id,
                "product_version": task.production_label_product_version_snapshot,
                "template_version": template_version,
                "customer_id": card.get("customer_id"),
                "customer_name": customer_name,
                "customer_code": customer.customer_code if customer is not None else None,
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
                **customer_label_fields,
            }
        )

    if len(template_versions) > 1:
        package_review_messages.append(
            "同一报料单包含不同尺寸的包装标签模板，请分别创建打印作业"
        )
    template_version = (
        next(iter(template_versions)) if len(template_versions) == 1 else None
    )

    labels: list[dict] = []
    for plan in plans:
        for index, quantity in enumerate(plan["label_quantities"], start=1):
            label = {
                "production_task_id": plan["production_task_id"],
                "production_task_version": plan["production_task_version"],
                "template_version": plan["template_version"],
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
            if plan["template_version"] == CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION:
                label["customer_short_name"] = plan["customer_short_name"]
            labels.append(label)

    label_layout = (
        effective_layout(db)
        if template_version == CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
        else None
    )
    fingerprint_payload = {
        "supplier_order_id": int(order.id),
        "supplier_order_number": order.order_number,
        "plans": plans,
    }
    if label_layout is not None:
        fingerprint_payload["label_layout"] = label_layout
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
    result = {
        "supplier_order_id": int(order.id),
        "supplier_order_number": order.order_number,
        "status": order.status,
        "status_label": "生产包装标签｜非库存标签",
        "template_version": template_version,
        "template_dimensions": (
            template_dimensions(template_version) if template_version else None
        ),
        "plan_fingerprint": fingerprint,
        "production_task_count": len(plans),
        "label_count": len(labels),
        "review_required": bool(review_messages),
        "review_messages": review_messages,
        "printable": bool(labels) and not review_messages,
        "plans": plans,
        "labels": labels,
    }
    if label_layout is not None:
        result["label_layout"] = label_layout
    return result
