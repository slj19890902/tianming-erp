from __future__ import annotations

import hashlib
import json
from math import ceil
import re
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.order import OrderItem
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


def build_supplier_requisition_packaging_label_package(
    db: Session,
    order: SupplierRequisitionOrder,
) -> dict:
    """Project a read-only package using the current common-box label policy.

    A supplier order can contain cover/base rows or repeated requisition sources
    that point to the same production task.  The task id is therefore the only
    deduplication key; procurement sheet quantities are deliberately ignored.
    Production quantity and template stay frozen on the task, while the enabled
    flag and units-per-label come from the current Product master.  A prepared
    print job freezes that projection; an actually printed job remains immutable.
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
            package_review_messages.append(
                f"生产任务 #{task_id} 没有可回读的常用箱产品，请核对"
            )
            continue
        if not bool(product.production_label_enabled):
            continue
        if not bool(task.production_label_enabled_snapshot):
            if (
                task.status in {"waiting_material", "pending"}
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

        units_per_label = _positive_int(product.production_label_units_per_label)
        total_quantity = _positive_int(task.production_label_total_quantity_snapshot)
        label_count = ceil(total_quantity / units_per_label) if units_per_label else 0
        if not units_per_label or not total_quantity or not label_count:
            package_review_messages.append(
                f"生产任务 #{task_id} 的常用箱标签数量或任务总量不完整，请核对"
            )
            continue

        card, component = task_sources[task_id]
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
    }
    if label_layout is not None:
        result["label_layout"] = label_layout
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
