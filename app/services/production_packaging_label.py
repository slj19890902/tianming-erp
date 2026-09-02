from __future__ import annotations

import hashlib
import json
from math import ceil
import re
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
from app.models.production import ProductionTask
from app.models.requisition import Requisition
from app.models.supplier_requisition_order import SupplierRequisitionOrder
from app.services.requisition_production_print import (
    build_supplier_requisition_production_package,
)
from app.services.production_label_strategy import (
    CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION,
)
from app.services.production_packaging_label_layout import effective_layout
from app.services.product_specification import (
    embedded_dimension_specification,
    resolved_product_specification,
)
from app.services.delivery_goods_projection import (
    actual_goods_lines,
    customer_document_fulfillment_mode,
    delivery_component_lines,
)


class ProductionPackagingLabelError(ValueError):
    pass


ALLOWED_TEMPLATE_VERSIONS = frozenset(
    {
        "legacy_65x45_v1",
        "current_40x30_v1",
        CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION,
    }
)
COMPOSITE_PRINTABLE_ITEM_STATUSES = frozenset({"有效", "已入库"})

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


def apply_packaging_label_print_counts(
    package: dict,
    requested_counts: Mapping[int, int],
) -> dict:
    """Freeze per-task print counts without changing the production plan."""

    plans = list(package.get("plans") or [])
    expected_task_ids = {int(plan["production_task_id"]) for plan in plans}
    actual_task_ids = {int(task_id) for task_id in requested_counts}
    if actual_task_ids != expected_task_ids:
        raise ProductionPackagingLabelError(
            "本次打印任务清单与当前标签计划不一致，请刷新后重试"
        )

    frozen = json.loads(json.dumps(package, ensure_ascii=False, default=str))
    selected_plans: list[dict] = []
    selection: list[dict] = []
    for plan in frozen.get("plans") or []:
        task_id = int(plan["production_task_id"])
        system_count = int(plan.get("label_count") or 0)
        requested = requested_counts[task_id]
        if isinstance(requested, bool) or not isinstance(requested, int):
            raise ProductionPackagingLabelError("本次打印标签张数必须为整数")
        if requested < 0 or requested > system_count:
            raise ProductionPackagingLabelError(
                f"生产任务 #{task_id} 本次打印张数必须在 0～{system_count} 之间"
            )
        selection.append(
            {
                "production_task_id": task_id,
                "print_label_count": requested,
                "system_label_count": system_count,
            }
        )
        if requested == 0:
            continue
        plan["system_label_count"] = system_count
        plan["print_label_count"] = requested
        selected_plans.append(plan)

    if not selected_plans:
        raise ProductionPackagingLabelError("本次未选择需要打印的标签")

    selected_by_task = {
        int(plan["production_task_id"]): int(plan["print_label_count"])
        for plan in selected_plans
    }
    selected_job_task_ids = {
        int(task_id)
        for plan in selected_plans
        for task_id in (
            plan.get("job_task_ids") or [plan["production_task_id"]]
        )
    }
    labels = [
        label
        for label in (frozen.get("labels") or [])
        if int(label["production_task_id"]) in selected_by_task
        and int(label["label_number"])
        <= selected_by_task[int(label["production_task_id"])]
    ]
    frozen["system_production_task_count"] = int(
        package.get("production_task_count") or len(plans)
    )
    frozen["system_label_count"] = int(
        package.get("label_count") or len(package.get("labels") or [])
    )
    if "job_tasks" in frozen:
        frozen["job_tasks"] = [
            row
            for row in (frozen.get("job_tasks") or [])
            if int(row["production_task_id"]) in selected_job_task_ids
        ]
        frozen["production_task_count"] = len(frozen["job_tasks"])
    else:
        frozen["production_task_count"] = len(selected_plans)
    frozen["label_count"] = len(labels)
    frozen["plans"] = selected_plans
    frozen["labels"] = labels
    frozen["print_selection"] = selection
    frozen["print_summary"] = {
        "printed_task_count": len(selected_plans),
        "print_label_count": len(labels),
        "system_task_count": len(plans),
        "system_label_count": int(package.get("label_count") or 0),
    }
    return frozen


def apply_delivery_packaging_label_print_counts(
    package: dict,
    requested_counts: Mapping[str, int],
) -> dict:
    """Freeze delivery-label counts by opaque projected-line identity."""

    plans = list(package.get("plans") or [])
    expected_keys = {str(plan["selection_key"]) for plan in plans}
    actual_keys = {str(selection_key) for selection_key in requested_counts}
    if actual_keys != expected_keys:
        raise ProductionPackagingLabelError(
            "本次送货标签清单与当前送货单不一致，请刷新后重试"
        )

    frozen = json.loads(json.dumps(package, ensure_ascii=False, default=str))
    selected_plans: list[dict] = []
    selection: list[dict] = []
    for plan in frozen.get("plans") or []:
        selection_key = str(plan["selection_key"])
        system_count = int(plan.get("label_count") or 0)
        requested = requested_counts[selection_key]
        if isinstance(requested, bool) or not isinstance(requested, int):
            raise ProductionPackagingLabelError("本次打印标签张数必须为整数")
        if requested < 0 or requested > system_count:
            raise ProductionPackagingLabelError(
                f"送货标签 {selection_key} 本次打印张数必须在 0～{system_count} 之间"
            )
        selection.append(
            {
                "selection_key": selection_key,
                "print_label_count": requested,
                "system_label_count": system_count,
            }
        )
        if requested == 0:
            continue
        plan["system_label_count"] = system_count
        plan["print_label_count"] = requested
        selected_plans.append(plan)

    if not selected_plans:
        raise ProductionPackagingLabelError("本次未选择需要打印的送货标签")

    selected_by_key = {
        str(plan["selection_key"]): int(plan["print_label_count"])
        for plan in selected_plans
    }
    labels = [
        label
        for label in (frozen.get("labels") or [])
        if str(label["selection_key"]) in selected_by_key
        and int(label["label_number"])
        <= selected_by_key[str(label["selection_key"])]
    ]
    frozen["system_print_plan_count"] = int(
        package.get("print_plan_count") or len(plans)
    )
    frozen["system_label_count"] = int(
        package.get("label_count") or len(package.get("labels") or [])
    )
    frozen["print_plan_count"] = len(selected_plans)
    frozen["label_count"] = len(labels)
    frozen["plans"] = selected_plans
    frozen["labels"] = labels
    frozen["print_selection"] = selection
    frozen["print_summary"] = {
        "printed_plan_count": len(selected_plans),
        "print_label_count": len(labels),
        "system_plan_count": len(plans),
        "system_label_count": int(package.get("label_count") or 0),
    }
    return frozen


def build_delivery_packaging_label_package(
    db: Session,
    delivery: Delivery,
) -> dict:
    """Build labels from the same customer-facing goods projection as delivery PDF.

    A new preview follows the current common-box label policy and the established
    one-way parent-delivery override.  Preparing a print job freezes the exact
    projected rows, product versions, quantities, and released layout.
    """

    if delivery.status == "voided":
        raise ProductionPackagingLabelError("已作废送货单不能创建产品标签")
    if delivery.status not in {"pending", "dispatched"}:
        raise ProductionPackagingLabelError("当前送货单状态不能创建产品标签")

    customer = db.get(Customer, delivery.customer_id)
    if customer is None:
        raise ProductionPackagingLabelError("送货单客户不存在，不能创建产品标签")
    template_version = CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
    customer_label_fields = _customer_label_fields(
        template_version,
        customer.chinese_short_name,
    )
    delivery_items = list(
        db.scalars(
            select(DeliveryItem)
            .where(
                DeliveryItem.delivery_id == delivery.id,
                DeliveryItem.is_current.is_(True),
            )
            .order_by(DeliveryItem.id)
        ).all()
    )
    if not delivery_items:
        raise ProductionPackagingLabelError("送货单没有可打印的产品明细")

    order_item_ids = {
        int(item.order_item_id)
        for item in delivery_items
        if item.order_item_id is not None
    }
    order_items = {
        int(item.id): item
        for item in (
            db.scalars(select(OrderItem).where(OrderItem.id.in_(order_item_ids))).all()
            if order_item_ids
            else []
        )
    }
    order_ids = {int(item.order_id) for item in order_items.values()}
    orders = {
        int(order.id): order
        for order in (
            db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
            if order_ids
            else []
        )
    }

    projected_rows: list[dict] = []
    review_messages: list[str] = []
    source_product_versions: dict[int, int] = {}
    for delivery_item in delivery_items:
        if delivery_item.source_type == "unordered_finished":
            direct_product = (
                db.get(Product, delivery_item.product_id)
                if delivery_item.product_id is not None
                else None
            )
            if direct_product is not None:
                source_product_versions[int(direct_product.id)] = int(
                    direct_product.version
                )
            projected_rows.append(
                {
                    "line_type": "product",
                    "order_item_id": None,
                    "component_snapshot_id": None,
                    "source_delivery_item_id": int(delivery_item.id),
                    "projected_product_id": delivery_item.product_id,
                    "fulfillment_mode": "single_product",
                    # Unordered-finished rows are already frozen delivery facts.
                    # Current Product data may control label enablement/counts, but
                    # must never silently rename the historical delivered goods.
                    "product_code": delivery_item.product_code_snapshot,
                    "product_name": delivery_item.product_name_snapshot,
                    "specification": resolved_product_specification(
                        delivery_item.specification_snapshot,
                        direct_product,
                    ),
                    "unit": delivery_item.unit_snapshot,
                    "quantity": int(delivery_item.delivered_quantity or 0),
                    "order_numbers": [],
                    "customer_pos": [],
                }
            )
            continue

        order_item = order_items.get(int(delivery_item.order_item_id or 0))
        if order_item is None:
            review_messages.append(
                f"送货明细 #{delivery_item.id} 的订单明细不存在，请核对"
            )
            continue
        parent_product = db.get(Product, order_item.product_id)
        if parent_product is not None:
            source_product_versions[int(parent_product.id)] = int(
                parent_product.version
            )
        current_mode = (
            parent_product.composite_fulfillment_mode
            if parent_product is not None
            else None
        )
        component_rows = delivery_component_lines(
            db,
            order_item=order_item,
            planned_delivery_quantity=int(delivery_item.delivered_quantity or 0),
            delivery_item_id=int(delivery_item.id),
            dispatched=delivery.status == "dispatched",
        )
        is_composite = bool(component_rows) or bool(
            order_item.is_virtual_composite_parent_snapshot
        )
        fulfillment_mode = (
            customer_document_fulfillment_mode(
                frozen_order_mode=order_item.composite_fulfillment_mode_snapshot,
                current_product_mode=current_mode,
            )
            if is_composite
            else "parent_delivery"
        )
        rows = actual_goods_lines(
            order_item_id=int(order_item.id),
            product_code=delivery_item.product_code_snapshot
            or order_item.snapshot_product_code,
            product_name=delivery_item.product_name_snapshot
            or order_item.snapshot_product_name,
            specification=(
                resolved_product_specification(
                    delivery_item.specification_snapshot,
                    parent_product,
                    fallback_snapshots=(order_item.snapshot_spec,),
                )
                or embedded_dimension_specification(
                    parent_product.product_name if parent_product else None
                )
            ),
            parent_quantity=int(delivery_item.delivered_quantity or 0),
            component_lines=component_rows,
            fulfillment_mode=fulfillment_mode,
            source_delivery_item_id=int(delivery_item.id),
            parent_product_id=order_item.product_id,
            include_internal_ids=True,
        )
        order = orders.get(int(order_item.order_id))
        for row in rows:
            row["fulfillment_mode"] = (
                fulfillment_mode if is_composite else "single_product"
            )
            row["order_numbers"] = [order.order_number] if order is not None else []
            row["customer_pos"] = (
                [order.customer_po]
                if order is not None and str(order.customer_po or "").strip()
                else []
            )
        projected_rows.extend(rows)

    plans: list[dict] = []
    labels: list[dict] = []
    excluded_items: list[dict] = []
    for row in projected_rows:
        delivery_item_id = int(row.get("source_delivery_item_id") or 0)
        component_snapshot_id = row.get("component_snapshot_id")
        line_identity = (
            f"component:{int(component_snapshot_id)}"
            if component_snapshot_id is not None
            else str(row.get("line_type") or "product")
        )
        selection_key = (
            f"delivery:{int(delivery.id)}:item:{delivery_item_id}:{line_identity}"
        )
        product_id = _positive_int(row.get("projected_product_id"))
        product = db.get(Product, product_id) if product_id else None
        if product is None:
            review_messages.append(
                f"{row.get('product_code') or selection_key} 没有可回读的常用箱产品，请核对"
            )
            continue
        source_product_versions[int(product.id)] = int(product.version)
        if not bool(product.production_label_enabled):
            excluded_items.append(
                {
                    "selection_key": selection_key,
                    "delivery_item_id": delivery_item_id,
                    "product_id": product_id,
                    "product_version": int(product.version),
                    "product_code": row.get("product_code"),
                    "product_name": row.get("product_name"),
                    "reason": "常用箱未勾选打印标签",
                }
            )
            continue
        missing_label_fields = [
            field_name
            for field_name, value in (
                ("存货编码", row.get("product_code")),
                ("产品名称", row.get("product_name")),
                ("规格", row.get("specification")),
            )
            if not str(value or "").strip()
        ]
        if missing_label_fields:
            review_messages.append(
                f"{row.get('product_code') or selection_key} 缺少"
                f"{'、'.join(missing_label_fields)}，请核对送货快照"
            )
            continue
        units_per_label = _positive_int(product.production_label_units_per_label)
        total_quantity = _positive_int(row.get("quantity"))
        if not units_per_label or not total_quantity:
            review_messages.append(
                f"{row.get('product_code') or selection_key} 的标签数量策略或送货数量不完整，请核对"
            )
            continue
        label_count = ceil(total_quantity / units_per_label)
        quantities = [
            min(units_per_label, total_quantity - index * units_per_label)
            for index in range(label_count)
        ]
        plan = {
            "selection_key": selection_key,
            "delivery_item_id": delivery_item_id,
            "order_item_id": row.get("order_item_id"),
            "component_snapshot_id": component_snapshot_id,
            "fulfillment_mode": row.get("fulfillment_mode"),
            "product_id": product_id,
            "product_version": int(product.version),
            "label_policy_source": "product_master_current",
            "template_version": template_version,
            "customer_id": int(customer.id),
            "customer_name": customer.name,
            "customer_code": customer.customer_code,
            "product_code": row.get("product_code"),
            "product_name": row.get("product_name"),
            "specification": row.get("specification"),
            "order_numbers": list(row.get("order_numbers") or []),
            "customer_pos": list(row.get("customer_pos") or []),
            "total_quantity": total_quantity,
            "units_per_label": units_per_label,
            "label_count": label_count,
            "label_quantities": quantities,
            **customer_label_fields,
        }
        plans.append(plan)
        for index, quantity in enumerate(quantities, start=1):
            labels.append(
                {
                    "selection_key": selection_key,
                    "delivery_item_id": delivery_item_id,
                    "order_item_id": row.get("order_item_id"),
                    "component_snapshot_id": component_snapshot_id,
                    "fulfillment_mode": row.get("fulfillment_mode"),
                    "template_version": template_version,
                    "customer_id": int(customer.id),
                    "customer_name": customer.name,
                    "customer_code": customer.customer_code,
                    "customer_short_name": customer_label_fields[
                        "customer_short_name"
                    ],
                    "product_code": row.get("product_code"),
                    "product_name": row.get("product_name"),
                    "specification": row.get("specification"),
                    "order_numbers": list(row.get("order_numbers") or []),
                    "customer_pos": list(row.get("customer_pos") or []),
                    "quantity": quantity,
                    "total_quantity": total_quantity,
                    "units_per_label": units_per_label,
                    "label_number": index,
                    "label_count": label_count,
                }
            )

    label_layout = effective_layout(db)
    fingerprint_payload = {
        "delivery_id": int(delivery.id),
        "delivery_number": delivery.delivery_number,
        "delivery_status": delivery.status,
        "plans": plans,
        "excluded_items": excluded_items,
        "customer_id": int(customer.id),
        "customer_version": int(customer.version),
        "source_product_versions": [
            {
                "product_id": product_id,
                "product_version": source_product_versions[product_id],
            }
            for product_id in sorted(source_product_versions)
        ],
        "label_layout": label_layout,
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
    messages = list(dict.fromkeys(str(value) for value in review_messages if value))
    return {
        "delivery_id": int(delivery.id),
        "delivery_number": delivery.delivery_number,
        "delivery_status": delivery.status,
        "status_label": "送货产品标签｜非库存标签",
        "label_policy_source": "product_master_current",
        "customer_id": int(customer.id),
        "customer_version": int(customer.version),
        "source_product_versions": fingerprint_payload[
            "source_product_versions"
        ],
        "template_version": template_version,
        "template_dimensions": template_dimensions(template_version),
        "plan_fingerprint": fingerprint,
        "print_plan_count": len(plans),
        "label_count": len(labels),
        "review_required": bool(messages),
        "review_messages": messages,
        "printable": bool(labels) and not messages,
        "plans": plans,
        "labels": labels,
        "excluded_items": excluded_items,
        "label_layout": label_layout,
    }


def build_supplier_requisition_packaging_label_package(
    db: Session,
    order: SupplierRequisitionOrder,
    *,
    selected_supplier_item_ids: set[int] | None = None,
    selected_task_ids: set[int] | None = None,
) -> dict:
    """Project a read-only package using the current common-box label policy.

    A supplier order can contain cover/base rows or repeated requisition sources
    that point to the same production task.  The task id is therefore the only
    deduplication key; procurement sheet quantities are deliberately ignored.
    Every selected requisition item is projected independently.  The enabled
    flag, units-per-label and template come from the current Product master, and
    the quantity comes from the current outstanding production demand.  A
    prepared print job freezes that projection; an actually printed job remains
    immutable.
    """

    if selected_supplier_item_ids is not None and selected_task_ids is not None:
        raise ProductionPackagingLabelError("产品标签明细与任务不能同时筛选")
    normalized_item_ids = (
        {int(value) for value in selected_supplier_item_ids}
        if selected_supplier_item_ids is not None
        else None
    )
    normalized_task_ids = (
        {int(value) for value in selected_task_ids}
        if selected_task_ids is not None
        else None
    )
    if normalized_item_ids is not None and not normalized_item_ids:
        raise ProductionPackagingLabelError("请选择需要打印标签的报料明细")
    if normalized_task_ids is not None and not normalized_task_ids:
        raise ProductionPackagingLabelError("请选择需要打印标签的生产任务")

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

    all_task_sources: dict[int, tuple[dict, dict]] = {}
    item_filtered_task_sources: dict[int, tuple[dict, dict]] = {}
    available_item_ids: set[int] = set()
    for card in production_package["cards"]:
        for component in card.get("components", []):
            supplier_item_id = component.get("supplier_order_item_id")
            if supplier_item_id is not None:
                available_item_ids.add(int(supplier_item_id))
            task_id = component.get("production_task_id")
            if task_id is None:
                continue
            all_task_sources.setdefault(int(task_id), (card, component))
            if (
                normalized_item_ids is None
                or int(supplier_item_id or 0) in normalized_item_ids
            ):
                item_filtered_task_sources.setdefault(
                    int(task_id), (card, component)
                )

    if normalized_item_ids is not None:
        missing_item_ids = sorted(normalized_item_ids - available_item_ids)
        if missing_item_ids:
            raise ProductionPackagingLabelError(
                "所选报料明细不存在、已作废或已变化，请刷新后重试"
            )
    if normalized_task_ids is not None:
        missing_task_ids = sorted(normalized_task_ids - set(all_task_sources))
        if missing_task_ids:
            raise ProductionPackagingLabelError(
                "所选生产任务不属于当前报料单或已变化，请刷新后重试"
            )

    task_sources = {
        task_id: source
        for task_id, source in item_filtered_task_sources.items()
        if normalized_task_ids is None or task_id in normalized_task_ids
    }

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
    excluded_items: list[dict] = []
    template_versions: set[str] = set()
    # An explicitly refreshed label snapshot is allowed to be newer than the
    # requisition card.  That production-card warning must not make the frozen
    # label plan unprintable; every other review reason remains fail-closed.
    package_review_messages = (
        [
            message
            for message in (production_package.get("review_messages") or [])
            if str(message) != "生产任务版本已变化，请核对并重打"
        ]
        if normalized_item_ids is None and normalized_task_ids is None
        else []
    )
    for task_id in sorted(task_sources):
        card, component = task_sources[task_id]
        product_code = str(
            component.get("product_code") or card.get("product_code") or ""
        ).strip()
        product_name = str(
            component.get("product_name") or card.get("product_name") or ""
        ).strip()

        def exclude(
            reason: str,
            *,
            product_id: int | None = None,
            blocks_single_order: bool = False,
        ) -> None:
            excluded_items.append(
                {
                    "production_task_id": task_id,
                    "product_id": product_id,
                    "product_code": product_code or None,
                    "product_name": product_name or None,
                    "reason": reason,
                }
            )
            if blocks_single_order:
                identity = product_code or product_name or f"生产任务 #{task_id}"
                package_review_messages.append(f"{identity}：{reason}")

        task = tasks.get(task_id)
        if task is None:
            exclude("对应生产任务不存在，请核对", blocks_single_order=True)
            continue
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
        if product is None:
            exclude(
                "没有可回读的常用箱产品，请核对",
                product_id=product_id,
                blocks_single_order=True,
            )
            continue
        if not bool(product.production_label_enabled):
            exclude(
                "常用箱未启用打印标签；请在常用箱勾选并保存后刷新来料页面",
                product_id=product_id,
                blocks_single_order=True,
            )
            continue
        template_version = CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
        if template_version not in ALLOWED_TEMPLATE_VERSIONS:
            package_review_messages.append(
                f"生产任务 #{task_id} 的包装标签模板版本不受支持，请核对"
            )
            continue
        template_versions.add(template_version)

        units_per_label = _positive_int(product.production_label_units_per_label)
        try:
            # Local import avoids the module cycle: label operations use this
            # projector when they freeze a prepared print job.
            from app.services.production_label_operations import (
                ProductionLabelOperationError,
                _task_product_and_total,
            )

            _current_product, total_quantity = _task_product_and_total(db, task)
        except ProductionLabelOperationError as error:
            package_review_messages.append(f"生产任务 #{task_id}：{error}")
            continue
        total_quantity = _positive_int(total_quantity)
        label_count = ceil(total_quantity / units_per_label) if units_per_label else 0
        if not units_per_label or not total_quantity or not label_count:
            package_review_messages.append(
                f"生产任务 #{task_id} 的常用箱标签数量或任务总量不完整，请核对"
            )
            continue

        quantities = [
            min(units_per_label, total_quantity - index * units_per_label)
            for index in range(label_count)
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
                "product_version": int(product.version),
                "label_policy_source": "product_master_current",
                "task_label_product_version_snapshot": (
                    task.production_label_product_version_snapshot
                ),
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
                "label_count": label_count,
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
        "label_policy_source": "product_master_current",
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
        "excluded_items": excluded_items,
    }
    if label_layout is not None:
        result["label_layout"] = label_layout
    return result


def combine_supplier_requisition_packaging_label_packages(
    packages: list[dict],
) -> dict:
    """Combine multiple supplier-order plans into one fail-closed print surface.

    Each source order keeps its own fingerprint and later its own immutable print
    job.  This combined projection exists only so one operator selection opens a
    single label page and a single printer dialog.
    """

    if not packages:
        raise ProductionPackagingLabelError("请选择需要打印标签的供应商报料单")
    ordered = sorted(packages, key=lambda value: int(value["supplier_order_id"]))
    order_ids = [int(package["supplier_order_id"]) for package in ordered]
    if len(order_ids) != len(set(order_ids)):
        raise ProductionPackagingLabelError("供应商报料单不能重复")

    templates = {
        str(package.get("template_version") or "").strip()
        for package in ordered
        if package.get("template_version")
    }
    review_messages: list[str] = []
    if len(templates) != 1:
        review_messages.append("所选报料单包含不同纸型的产品标签，请分别打印")
    template_version = next(iter(templates)) if len(templates) == 1 else None

    plans: list[dict] = []
    labels: list[dict] = []
    excluded_items: list[dict] = []
    print_selection: list[dict] = []
    source_fingerprints: list[dict] = []
    layout_payloads: list[dict] = []
    for package in ordered:
        order_id = int(package["supplier_order_id"])
        order_number = package.get("supplier_order_number")
        source_fingerprints.append(
            {
                "supplier_order_id": order_id,
                "supplier_order_number": order_number,
                "plan_fingerprint": package.get("plan_fingerprint"),
            }
        )
        for reason in package.get("review_messages") or []:
            review_messages.append(f"{order_number or order_id}｜{reason}")
        for item in package.get("excluded_items") or []:
            identity = item.get("product_code") or item.get("product_name") or "未识别产品"
            reason = item.get("reason") or "没有有效标签配置"
            review_messages.append(f"{order_number or order_id}｜{identity}：{reason}")
        for target, source in (
            (plans, package.get("plans") or []),
            (labels, package.get("labels") or []),
            (excluded_items, package.get("excluded_items") or []),
            (print_selection, package.get("print_selection") or []),
        ):
            for row in source:
                target.append(
                    {
                        **row,
                        "supplier_order_id": order_id,
                        "supplier_order_number": order_number,
                    }
                )
        if package.get("label_layout") is not None:
            layout_payloads.append(package["label_layout"])

    if layout_payloads and any(
        json.dumps(layout, ensure_ascii=False, sort_keys=True, default=str)
        != json.dumps(layout_payloads[0], ensure_ascii=False, sort_keys=True, default=str)
        for layout in layout_payloads[1:]
    ):
        review_messages.append("所选报料单读取到不同版本的标签布局，请刷新后重试")

    fingerprint = hashlib.sha256(
        json.dumps(
            source_fingerprints,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    messages = list(dict.fromkeys(review_messages))
    result = {
        "source_type": "supplier_order_batch",
        "supplier_order_ids": order_ids,
        "supplier_order_numbers": [
            package.get("supplier_order_number") for package in ordered
        ],
        "batch_source_fingerprints": source_fingerprints,
        "status_label": "生产包装标签｜非库存标签",
        "label_policy_source": "product_master_current",
        "template_version": template_version,
        "template_dimensions": (
            template_dimensions(template_version) if template_version else None
        ),
        "plan_fingerprint": fingerprint,
        "production_task_count": len(plans),
        "label_count": len(labels),
        "review_required": bool(messages),
        "review_messages": messages,
        "printable": bool(labels) and not messages,
        "plans": plans,
        "labels": labels,
        "excluded_items": excluded_items,
    }
    if print_selection:
        result["print_selection"] = print_selection
        result["system_label_count"] = sum(
            int(package.get("system_label_count") or package.get("label_count") or 0)
            for package in ordered
        )
        result["print_summary"] = {
            "printed_task_count": len(plans),
            "print_label_count": len(labels),
            "system_task_count": sum(
                int(package.get("print_summary", {}).get("system_task_count") or 0)
                for package in ordered
            ),
            "system_label_count": result["system_label_count"],
        }
    if layout_payloads:
        result["label_layout"] = layout_payloads[0]
    return result


def build_composite_requisition_packaging_label_package(
    db: Session,
    requisition: Requisition,
    *,
    selected_item_ids: set[int] | None = None,
) -> dict:
    """Build labels for a legacy composite requisition using frozen order/task facts.

    Component-delivery orders print the selected child task labels. Parent-delivery
    orders print one parent plan per order item and carry every child task version
    as evidence, so the automatic shortest-component kit rule remains auditable.
    """

    item_by_id = {int(row.id): row for row in requisition.items}
    selected_ids = (
        {int(value) for value in selected_item_ids}
        if selected_item_ids is not None
        else set(item_by_id)
    )
    if not selected_ids or not selected_ids.issubset(item_by_id):
        raise ProductionPackagingLabelError("所选组合报料明细不存在或已发生变化")
    inactive_item_ids = sorted(
        item_id
        for item_id in selected_ids
        if str(item_by_id[item_id].status or "").strip()
        not in COMPOSITE_PRINTABLE_ITEM_STATUSES
    )
    if inactive_item_ids:
        raise ProductionPackagingLabelError(
            f"所选组合报料明细已取消或失效：{inactive_item_ids}"
        )
    sources = {
        int(source.requisition_item_id): source
        for source in db.scalars(
            select(RequisitionItemBomSource).where(
                RequisitionItemBomSource.requisition_item_id.in_(set(item_by_id))
            )
        ).all()
    }
    if any(item_id not in sources for item_id in selected_ids):
        raise ProductionPackagingLabelError("所选报料明细不是完整的组合 BOM 子件")
    snapshot_ids = {
        int(sources[item_id].sales_order_item_bom_component_id)
        for item_id in selected_ids
    }
    snapshots = {
        int(row.id): row
        for row in db.scalars(
            select(SalesOrderItemBomComponent).where(
                SalesOrderItemBomComponent.id.in_(snapshot_ids)
            )
        ).all()
    }
    tasks = {
        int(task.sales_order_item_bom_component_id): task
        for task in db.scalars(
            select(ProductionTask).where(
                ProductionTask.sales_order_item_bom_component_id.in_(snapshot_ids)
            )
        ).all()
    }
    order_item_ids = {int(item_by_id[item_id].order_item_id) for item_id in selected_ids}
    order_items = {
        int(item.id): item
        for item in db.scalars(
            select(OrderItem).where(OrderItem.id.in_(order_item_ids))
        ).all()
    }
    customer_ids = {
        int(item.order.customer_id)
        for item in order_items.values()
        if item.order is not None
    }
    customers = {
        int(customer.id): customer
        for customer in db.scalars(
            select(Customer).where(Customer.id.in_(customer_ids))
        ).all()
    } if customer_ids else {}
    product_ids = {
        int(order_item.product_id) for order_item in order_items.values()
    } | {
        int(snapshot.component_product_id) for snapshot in snapshots.values()
    }
    products = {
        int(product.id): product
        for product in (
            db.scalars(select(Product).where(Product.id.in_(product_ids))).all()
            if product_ids
            else []
        )
    }

    plans: list[dict] = []
    job_tasks: list[dict] = []
    review_messages: list[str] = []
    template_versions: set[str] = set()
    seen_task_ids: set[int] = set()

    def append_job_task(task: ProductionTask, product_id: int | None) -> None:
        if int(task.id) in seen_task_ids:
            return
        seen_task_ids.add(int(task.id))
        job_tasks.append(
            {
                "production_task_id": int(task.id),
                "production_task_version": int(task.version or 1),
                "product_id": product_id,
                "product_version": task.production_label_product_version_snapshot,
                "template_version": str(
                    task.production_label_template_version_snapshot
                    or CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
                ),
            }
        )

    selected_by_order_item: dict[int, list[int]] = {}
    for item_id in sorted(selected_ids):
        selected_by_order_item.setdefault(
            int(item_by_id[item_id].order_item_id), []
        ).append(item_id)

    for order_item_id, group_item_ids in selected_by_order_item.items():
        order_item = order_items.get(order_item_id)
        if order_item is None or order_item.order is None:
            review_messages.append(f"订单明细 #{order_item_id} 不存在")
            continue
        mode = str(
            getattr(order_item, "composite_fulfillment_mode_snapshot", None)
            or "component_delivery"
        )
        customer = customers.get(int(order_item.order.customer_id))
        if customer is None:
            review_messages.append(f"订单明细 #{order_item_id} 的客户资料不存在")
            continue
        group_tasks: list[
            tuple[
                ProductionTask,
                SalesOrderItemBomComponent,
                RequisitionItemBomSource,
            ]
        ] = []
        for requisition_item_id in group_item_ids:
            source = sources[requisition_item_id]
            snapshot_id = int(source.sales_order_item_bom_component_id)
            snapshot = snapshots.get(snapshot_id)
            task = tasks.get(snapshot_id)
            if snapshot is None or task is None:
                review_messages.append(
                    f"报料明细 #{requisition_item_id} 缺少对应生产任务"
                )
                continue
            group_tasks.append((task, snapshot, source))

        if mode == "parent_delivery":
            all_group_ids = {
                int(row.id)
                for row in requisition.items
                if int(row.order_item_id) == order_item_id
                and int(row.id) in sources
                and str(row.status or "").strip()
                in COMPOSITE_PRINTABLE_ITEM_STATUSES
            }
            if set(group_item_ids) != all_group_ids:
                review_messages.append(
                    f"{order_item.snapshot_product_code or order_item_id} 为父件交付，必须整组选择所有子件"
                )
                continue
            parent_product = products.get(int(order_item.product_id))
            if parent_product is None:
                review_messages.append(
                    f"{order_item.snapshot_product_code or order_item_id} 没有可回读的常用箱产品"
                )
                continue
            if not bool(parent_product.production_label_enabled):
                review_messages.append(
                    f"{order_item.snapshot_product_code or order_item_id} 未启用父件产品标签"
                )
                continue
            units_per_label = _positive_int(
                parent_product.production_label_units_per_label
            )
            total_quantity = _positive_int(order_item.quantity)
            if not units_per_label or not total_quantity:
                review_messages.append(
                    f"{order_item.snapshot_product_code or order_item_id} 的父件标签数量策略不完整"
                )
                continue
            task_pairs = sorted(group_tasks, key=lambda pair: int(pair[0].id))
            if not task_pairs:
                continue
            for task, snapshot, _source in task_pairs:
                append_job_task(task, int(snapshot.component_product_id))
            template_version = str(
                order_item.parent_production_label_template_version_snapshot
                or CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
            )
            template_versions.add(template_version)
            label_count = ceil(total_quantity / units_per_label)
            plans.append(
                {
                    "production_task_id": int(task_pairs[0][0].id),
                    "production_task_version": int(task_pairs[0][0].version or 1),
                    "product_id": int(order_item.product_id),
                    "product_version": int(parent_product.version),
                    "label_policy_source": "product_master_current",
                    "task_label_product_version_snapshot": (
                        order_item.parent_production_label_product_version_snapshot
                    ),
                    "template_version": template_version,
                    "customer_id": int(customer.id),
                    "customer_name": customer.name,
                    "customer_code": customer.customer_code,
                    "product_code": order_item.snapshot_product_code,
                    "product_name": order_item.snapshot_product_name,
                    "specification": order_item.snapshot_spec,
                    "order_numbers": [order_item.order.order_number],
                    "customer_pos": [order_item.order.customer_po] if order_item.order.customer_po else [],
                    "total_quantity": total_quantity,
                    "units_per_label": units_per_label,
                    "label_count": label_count,
                    "label_quantities": [
                        min(units_per_label, total_quantity - index * units_per_label)
                        for index in range(label_count)
                    ],
                    "job_task_ids": [
                        int(task.id) for task, _snapshot, _source in task_pairs
                    ],
                    "fulfillment_mode": "parent_delivery",
                    **_customer_label_fields(template_version, customer.chinese_short_name),
                }
            )
            continue

        for task, snapshot, source in sorted(
            group_tasks, key=lambda pair: int(pair[0].id)
        ):
            append_job_task(task, int(snapshot.component_product_id))
            product = products.get(int(snapshot.component_product_id))
            if product is None:
                review_messages.append(
                    f"{snapshot.snapshot_component_product_code or snapshot.component_product_id} 没有可回读的常用箱产品"
                )
                continue
            if not bool(product.production_label_enabled):
                review_messages.append(
                    f"{snapshot.snapshot_component_product_code or snapshot.component_product_id} 未启用子件产品标签"
                )
                continue
            template_version = str(
                task.production_label_template_version_snapshot
                or CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
            )
            units_per_label = _positive_int(product.production_label_units_per_label)
            total_quantity = _positive_int(source.required_piece_quantity)
            label_count = ceil(total_quantity / units_per_label) if units_per_label else 0
            if (
                template_version not in ALLOWED_TEMPLATE_VERSIONS
                or not units_per_label
                or not total_quantity
                or not label_count
            ):
                review_messages.append(
                    f"生产任务 #{task.id} 的常用箱子件标签数量或任务总量不完整"
                )
                continue
            template_versions.add(template_version)
            plans.append(
                {
                    "production_task_id": int(task.id),
                    "production_task_version": int(task.version or 1),
                    "product_id": int(snapshot.component_product_id),
                    "product_version": int(product.version),
                    "label_policy_source": "product_master_current",
                    "task_label_product_version_snapshot": (
                        task.production_label_product_version_snapshot
                    ),
                    "template_version": template_version,
                    "customer_id": int(customer.id),
                    "customer_name": customer.name,
                    "customer_code": customer.customer_code,
                    "product_code": snapshot.snapshot_component_product_code,
                    "product_name": snapshot.snapshot_component_product_name,
                    "specification": snapshot.snapshot_component_spec,
                    "order_numbers": [order_item.order.order_number],
                    "customer_pos": [order_item.order.customer_po] if order_item.order.customer_po else [],
                    "total_quantity": total_quantity,
                    "units_per_label": units_per_label,
                    "label_count": label_count,
                    "label_quantities": [
                        min(units_per_label, total_quantity - index * units_per_label)
                        for index in range(label_count)
                    ],
                    "job_task_ids": [int(task.id)],
                    "fulfillment_mode": "component_delivery",
                    **_customer_label_fields(template_version, customer.chinese_short_name),
                }
            )

    if len(template_versions) > 1:
        review_messages.append("所选产品包含不同标签模板，请分开打印")
    template_version = next(iter(template_versions)) if len(template_versions) == 1 else None
    labels: list[dict] = []
    for plan in plans:
        for index, quantity in enumerate(plan["label_quantities"], start=1):
            label = {
                key: plan.get(key)
                for key in (
                    "production_task_id", "production_task_version", "template_version",
                    "customer_id", "customer_name", "customer_code", "product_code",
                    "product_name", "specification", "order_numbers", "customer_pos",
                    "total_quantity", "units_per_label",
                )
            }
            label.update(
                {
                    "quantity": quantity,
                    "label_number": index,
                    "label_count": plan["label_count"],
                }
            )
            if template_version == CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION:
                label["customer_short_name"] = plan["customer_short_name"]
            labels.append(label)

    label_layout = effective_layout(db) if template_version == CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION else None
    fingerprint_payload = {
        "material_requisition_id": int(requisition.id),
        "selected_item_ids": sorted(selected_ids),
        "plans": plans,
        "job_tasks": job_tasks,
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
    review_messages = list(dict.fromkeys(review_messages))
    result = {
        "material_requisition_id": int(requisition.id),
        "material_requisition_number": requisition.requisition_number,
        "selected_item_ids": sorted(selected_ids),
        "status": requisition.status,
        "status_label": "生产包装标签｜非库存标签",
        "label_policy_source": "product_master_current",
        "template_version": template_version,
        "template_dimensions": template_dimensions(template_version) if template_version else None,
        "plan_fingerprint": fingerprint,
        "production_task_count": len(job_tasks),
        "label_count": len(labels),
        "review_required": bool(review_messages),
        "review_messages": review_messages,
        "printable": bool(labels) and not review_messages,
        "plans": plans,
        "job_tasks": job_tasks,
        "labels": labels,
    }
    if label_layout is not None:
        result["label_layout"] = label_layout
    return result
