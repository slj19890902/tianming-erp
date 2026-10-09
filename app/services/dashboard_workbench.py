"""Read-only homepage details, restricted to the existing eligible identities.

This does not decide what may be received, produced, delivered or approved.
Those decisions remain with the authoritative module projections/endpoints.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select

from app.api.deps import has_permission
from app.core.time_contract import utc_naive_to_beijing_date
from app.models.business_approval import BusinessApproval
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionTask
from app.models.user import User


STAGES = {
    "pending_material": ("待报料", "requisition", "去报料"),
    "pending_incoming": ("待收料", "incoming", "查看来料"),
    "pending_production": ("待生产", "production", "安排生产"),
    "pending_delivery": ("可送货", "deliveries", "安排送货"),
    "pending_receipt": ("待回单", "deliveries", "登记回单"),
    "pending_reconciliation": ("待对账", "finance", "查看对账"),
    "pending_invoice": ("待开票", "finance", "查看开票"),
    "pending_payment": ("待收款", "finance", "查看收款"),
}


def day(value):
    if isinstance(value, datetime):
        return utc_naive_to_beijing_date(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10] if value else None


def quantity(value):
    return f"{float(value):g}" if value is not None else "待核"


def _pending_requests(db, user, scope):
    if user.role not in {"admin", "boss"} and not has_permission(user, "business_requests.submit"):
        return []
    query = select(BusinessApproval).where(BusinessApproval.status == "pending")
    if user.role == "boss":
        query = query.where(BusinessApproval.action == "stock_replenishment")
    elif user.role != "admin":
        query = query.where(BusinessApproval.applicant_id == user.id)
    if scope is not None:
        query = query.where(BusinessApproval.customer_id.in_(scope))
    return list(db.scalars(query.order_by(BusinessApproval.created_at, BusinessApproval.id)))


def build_workbench(db, *, user, data, warnings, today, visible_customer_ids):
    sources = data["rows"]
    requests = _pending_requests(db, user, visible_customer_ids)
    customer_ids = {r["customer_id"] for rows in sources.values() for r in rows if r.get("customer_id")}
    customer_ids.update(cid for rows in sources.values() for r in rows for cid in r.get("_workbench_customer_ids", []))
    customer_ids.update(w["customer_id"] for w in warnings)
    customer_ids.update(r.customer_id for r in requests)
    if visible_customer_ids is not None:
        customer_ids.intersection_update(visible_customer_ids)
    if has_permission(user, "customers.view"):
        query = select(Customer.id).where(Customer.is_active.is_(True))
        if visible_customer_ids is not None:
            query = query.where(Customer.id.in_(visible_customer_ids))
        customer_ids.update(db.scalars(query))
    customers = {
        c.id: {"id": c.id, "name": c.name, "label": c.chinese_short_name or c.customer_code or c.name}
        for c in db.scalars(select(Customer).where(Customer.id.in_(customer_ids)))
    } if customer_ids else {}

    production_ids = [r["id"] for r in sources.get("pending_production", [])]
    production = {}
    if production_ids:
        query = (select(ProductionTask.id, ProductionTask.order_item_id,
                        ProductionTask.planned_quantity, ProductionTask.version, ProductionTask.sales_order_item_bom_component_id,
                        SalesOrderItemBomComponent.snapshot_component_product_name.label("component_name"))
                 .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
                 .join(Order, Order.id == OrderItem.order_id)
                 .outerjoin(SalesOrderItemBomComponent, SalesOrderItemBomComponent.id == ProductionTask.sales_order_item_bom_component_id)
                 .where(ProductionTask.id.in_(production_ids), Order.customer_id.in_(customer_ids)))
        production = {r.id: dict(r) for r in db.execute(query).mappings()}
    item_ids = {int(r.get("order_item_id") or r["item_id"])
                for key in ("pending_material", "pending_incoming")
                for r in sources.get(key, [])
                if r.get("order_item_id") or isinstance(r.get("item_id"), int)}
    item_ids.update(r["order_item_id"] for r in production.values())
    item_ids.update(i["item_id"] for r in sources.get("pending_delivery", []) for i in r.get("workbench_items", []))
    details = {}
    if item_ids:
        query = (select(OrderItem.id, OrderItem.order_id, Order.customer_id, Order.customer_po,
                        Order.order_number, Order.delivery_date, OrderItem.quantity,
                        OrderItem.delivered_quantity, Order.updated_at.label("item_updated_at"), OrderItem.snapshot_product_code.label("product_code"),
                        OrderItem.snapshot_product_name.label("product_name"), OrderItem.sales_unit_snapshot,
                        Product.unit, Product.production_process, Product.is_composite,
                        Product.is_internal_component, Product.box_style, Product.supply_mode,
                        OrderItem.combination_role,
                        OrderItem.external_packaging_purchase_unit_snapshot.label("purchase_unit"))
                 .join(Order, Order.id == OrderItem.order_id)
                 .join(Product, Product.id == OrderItem.product_id)
                 .where(OrderItem.id.in_(item_ids), Order.customer_id.in_(customer_ids)))
        details = {r.id: dict(r) for r in db.execute(query).mappings()}
        from app.services.product_unit_labels import order_unit_label
        for detail in details.values():
            detail["unit"] = order_unit_label(detail, detail) or "（单位待核）"

    tasks = []
    month = data["snapshot"]["statement_month"]

    def add(key, row, identity, *, detail=None, message="", code=None, name=None, **extra):
        cid = row.get("customer_id")
        members = [member for member in row.get("_workbench_customer_ids", []) if member in customers]
        if cid not in customers and not members:
            return
        customer = customers.get(cid) or {"name": " / ".join(customers[c]["name"] for c in members), "label": " / ".join(customers[c]["label"] for c in members)}
        detail = detail or {}
        title, target, action = STAGES[key]
        deadline = day(detail.get("delivery_date") or row.get("delivery_date")) if key in {
            "pending_material", "pending_incoming", "pending_production", "pending_delivery"} else None
        overdue = (today - date.fromisoformat(deadline)).days if deadline else None
        urgency = "overdue" if overdue is not None and overdue > 0 else "today" if overdue == 0 else "normal"
        task = {
            "id": f"{key}:{identity}", "key": key, "type": title, "target": target,
            "customer_id": cid, "customer_ids": members or [cid], "customer_name": customer["name"], "customer_label": customer["label"],
            "product_code": code if code is not None else row.get("product_code") or detail.get("product_code") or "",
            "product_name": name if name is not None else row.get("product_name") or detail.get("product_name") or "",
            "order_id": detail.get("order_id"), "customer_po": detail.get("customer_po") or row.get("customer_po") or "",
            "order_number": detail.get("order_number") or row.get("order_number") or "",
            "due_date": deadline, "urgency": urgency,
            "urgency_label": f"逾期{overdue}天" if urgency == "overdue" else "今日交期" if urgency == "today" else title,
            "message": message, "action_text": ("提交送货申请" if has_permission(user, "business_requests.submit") else "查看送货") if key == "pending_delivery" and not has_permission(user, "deliveries.execute") else action,
            "target_filter": {"customer_id": cid, "statement_month": month},
            **extra,
        }
        task["target_filter"].update({"keyword": task["customer_po"] or task["order_number"] or task["product_code"], "product_code": task["product_code"]})
        tasks.append(task)
        return task

    for key in ("pending_material", "pending_incoming"):
        for row in sources.get(key, []):
            item_id = row.get("order_item_id") or row.get("item_id")
            detail = details.get(item_id, {})
            unit = detail.get("sales_unit_snapshot") or detail.get("unit") or "（单位待核）"
            message = f"订单 {quantity(detail['quantity'])}{unit} · 待安排报料" if detail and key == "pending_material" else "合并报料 · 查看明细数量" if key == "pending_material" else "等待收料 · 核对实际到货"
            if key == "pending_incoming" and row.get("requisition_qty") is not None:
                unit = detail.get("purchase_unit") or "张"
                message = f"报料 {quantity(row['requisition_qty'])}{unit} · 核对实际到货"
            identity = f"merge:{row['merge_group_id']}" if row.get("merge_group_id") else f"source:{row.get('item_id') or item_id}"
            add(key, row, identity, detail=detail, message=message,
                source_item_id=str(row.get("item_id") or ""),
                source_basis={**{k:row.get(k) for k in ('version','requisition_item_id','supplier_order_item_id','requisition_qty','received_quantity','remaining_quantity','merge_group_id')},
                    'item_updated_at':str(detail.get('item_updated_at'))})
    for row in sources.get("pending_production", []):
        task = production.get(row["id"])
        if not task:
            continue
        detail = details.get(task["order_item_id"], {})
        component = bool(task["sales_order_item_bom_component_id"])
        # Component task plans are pieces, never the parent's delivery sets.
        unit = "片" if component else detail.get("sales_unit_snapshot") or detail.get("unit") or "（单位待核）"
        add("pending_production", row, row["id"], detail=detail,
            name=task["component_name"] if component else None,
            message=f"计划生产 {quantity(task['planned_quantity'])}{unit}", task_id=row["id"], source_basis=task["version"])
    for group in sources.get("pending_delivery", []):
        for row in group.get("workbench_items", []):
            detail = details.get(row["item_id"])
            if not detail:
                continue
            unit = detail.get("sales_unit_snapshot") or detail.get("unit") or "（单位待核）"
            remaining = max(int(detail["quantity"]) - int(detail["delivered_quantity"] or 0), 0)
            add("pending_delivery", group, row["item_id"], detail=detail,
                message=f"待交 {remaining}{unit} · 可送 {row['ready_quantity']}{unit}", order_item_id=row["item_id"], source_basis=str(detail.get('item_updated_at')))
    for row in sources.get("pending_receipt", []):
        task = add("pending_receipt", row, row["delivery_id"], code=row.get("delivery_number") or "送货单",
            message=f"{day(row.get('delivery_date')) or '日期待核'}送货 · 待回单", delivery_id=row["delivery_id"])
        if task:
            task["target_filter"]["keyword"] = row.get("delivery_number") or ""
    for key in ("pending_reconciliation", "pending_invoice", "pending_payment"):
        for row in sources.get(key, []):
            amount = row.get("amount")
            row_month = row.get("statement_month") or month
            identity = row.get("settlement_identity") or row.get("statement_id") or row["customer_id"]
            task = add(key, row, f"{row_month}:{identity}",
                code=row_month, name=STAGES[key][0], message=f"{row_month} · ¥{float(amount):,.2f}" if amount is not None else f"{row_month} · 核对明细",
                source_basis=row.get("source_basis"), amount=str(amount) if amount is not None else None,
                settlement_identity=row.get("settlement_identity"))
            if task:
                task["target_filter"]["balance_type"] = key
                task["target_filter"]["statement_month"] = row_month
                if row.get("settlement_identity", "").startswith("entity:"):
                    task["customer_label"] = task["customer_name"] = row["customer_name"]

    from app.services.business_approvals import ACTIONS
    applicant_ids = {r.applicant_id for r in requests}
    applicants = dict(db.execute(select(User.id, User.real_name).where(User.id.in_(applicant_ids))).all()) if applicant_ids else {}
    pending_policies = {}
    for request in requests:
        if request.customer_id not in customers:
            continue
        try:
            payload = json.loads(request.payload_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        for item in payload.get("items", []) if request.action == "stock_replenishment" else []:
            if item.get("stock_policy_id"):
                pending_policies.setdefault((request.customer_id, int(item["stock_policy_id"])), []).append(request.id)
        customer = customers[request.customer_id]
        tasks.append({
            "id": f"approval:{request.id}", "key": "approval", "type": "待审批" if user.role in {"admin", "boss"} else "我的申请",
            "customer_id": request.customer_id, "customer_name": customer["name"], "customer_label": customer["label"],
            "product_code": "", "product_name": ACTIONS.get(request.action, "业务申请"),
            "order_number": "", "customer_po": "", "due_date": None,
            "urgency": "approval", "urgency_label": "待审批", "request_id": request.id,
            "message": f"{applicants.get(request.applicant_id) or '申请人'}提交 · {day(request.created_at) or ''}",
            "target": "approvals", "action_text": "审核" if user.role in {"admin", "boss"} else "查看申请",
            "target_filter": {"customer_id": request.customer_id},
        })
    for warning in warnings:
        customer = customers.get(warning["customer_id"], {})
        warning["customer_label"] = customer.get("label") or warning.get("customer_name")
        warning["pending_request_ids"] = pending_policies.get((warning["customer_id"], warning["policy_id"]), [])
    tasks.sort(key=lambda r: ({"overdue": 0, "today": 1, "approval": 2}.get(r["urgency"], 3), r.get("due_date") or "9999-12-31", r["id"]))
    return {"tasks": tasks, "customers": sorted(customers.values(), key=lambda c: c["label"]),
            "can_review": user.role in {"admin", "boss"},
            "can_view_approvals": user.role in {"admin", "boss"} or has_permission(user, "business_requests.submit"),
            "today": today.isoformat()}
