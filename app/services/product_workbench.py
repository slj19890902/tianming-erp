"""Read-only, scope-aware projections for the product workbench."""
from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.customer import Customer
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.mold_tool import MoldTool
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.stock_replenishment import InventoryStockPolicy
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail, InventoryLot, SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
)
from app.services.product_specification import product_dimension_specification
from app.services.product_unit_labels import product_unit_label


def visible_products(db: Session, scope: set[int] | None):
    query = select(Product).join(Customer, Customer.id == Product.customer_id).where(
        Product.deleted_at.is_(None), Product.purged_at.is_(None))
    if scope is not None:
        query = query.where(Product.customer_id.in_(scope))
    return query.options(selectinload(Product.customer), selectinload(Product.material))


def find_products(db: Session, scope: set[int] | None, *, q: str,
                  customer_id: int | None, dimension_basis: str,
                  length: Decimal | None, width: Decimal | None,
                  height: Decimal | None, page: int, page_size: int):
    query = visible_products(db, scope).outerjoin(MoldTool, MoldTool.id == Product.mold_tool_id)
    if customer_id is not None:
        query = query.where(Product.customer_id == customer_id)
    if dimension_basis == "net":
        # Only these registered box rules define length/width as physical flat
        # pieces. A carton footprint is never silently treated as net sheet.
        from app.services.box_type_rules import BOX_TYPE_RULES
        net_aliases = [alias for rule in BOX_TYPE_RULES if rule.code in
                       {"liner", "divider", "die_cut_partition"}
                       for alias in (*rule.aliases, rule.display_name)]
        query = query.where(Product.box_style.in_(net_aliases))
    dims = {
        "finished": (Product.length_mm, Product.width_mm, Product.height_mm),
        "net": (Product.length_mm, Product.width_mm, Product.height_mm),
        "report": (Product.report_length_mm, Product.report_width_mm, None),
    }[dimension_basis]
    for value, column in zip((length, width, height), dims):
        if value is not None and column is not None:
            query = query.where(column == value)
        elif value is not None:
            query = query.where(False)
    tokens = q.strip().split()
    for token in tokens:
        compact = re.sub(r"[.\-\s]", "", token).lower()
        pattern = "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        matching = [
            Product.product_code.ilike(pattern, escape="\\"),
            Product.customer_material_code.ilike(pattern, escape="\\"),
            Product.legacy_customer_material_code.ilike(pattern, escape="\\"),
            Product.product_name.ilike(pattern, escape="\\"),
            Product.customer_drawing_number.ilike(pattern, escape="\\"),
            MoldTool.mold_code.ilike(pattern, escape="\\"),
            MoldTool.mold_name.ilike(pattern, escape="\\"),
            Customer.name.ilike(pattern, escape="\\"),
            Customer.chinese_short_name.ilike(pattern, escape="\\"),
            Customer.customer_code.ilike(pattern, escape="\\"),
            exists(select(1).select_from(OrderItem).join(Order, Order.id == OrderItem.order_id)
                   .where(OrderItem.product_id == Product.id,
                          Order.customer_id == Product.customer_id,
                          or_(Order.order_number.ilike(pattern, escape="\\"),
                              Order.customer_po.ilike(pattern, escape="\\")))) ,
        ]
        if compact and compact != token.lower():
            matching.extend([
                func.replace(func.lower(Product.product_code), ".", "").contains(compact),
                func.replace(func.lower(Product.customer_material_code), ".", "").contains(compact),
            ])
        dimension_tokens = re.split(r"[xX×*]", token.removesuffix("mm"))
        if len(dimension_tokens) in (2, 3):
            try:
                values = [Decimal(part) for part in dimension_tokens]
            except Exception:
                pass
            else:
                matching.append(and_(*(dims[i] == value for i, value in enumerate(values)
                                       if dims[i] is not None)))
        query = query.where(or_(*matching))
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    exact = q.strip().lower()
    rank = case((func.lower(Product.product_code) == exact, 0),
                (func.lower(Product.customer_material_code) == exact, 1), else_=2)
    rows = list(db.scalars(query.order_by(rank, Customer.name, Product.id)
                           .offset((page - 1) * page_size).limit(page_size)))
    return rows, total


def inventory_summary(db: Session, products: list[Product]):
    # The mobile projection already enforces product identity, owner and confirmed
    # semi-finished bindings. A common code alone never shares a lot.
    from app.api.mobile_erp import _product_inventory_summaries
    originals = _product_inventory_summaries(db, products)
    result = {pid: {kind: {
        "actual": row[kind]["quantity_total"],
        "available": row[kind]["quantity_available"],
        "reserved": row[kind]["quantity_reserved"],
        "unit": row[kind]["unit"],
    } for kind in ("finished", "semi_finished")} for pid, row in originals.items()}
    for product in products:
        for lot in _shared_finished_lots(db, product):
            detail = lot.finished_detail
            if (detail and detail.product_id == product.id and
                detail.owner_customer_id == product.customer_id and not detail.is_general and
                detail.inventory_code_snapshot == product.product_code):
                continue  # Already counted by the ordinary mobile identity query.
            group = result[product.id]["finished"]
            group["actual"] += int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0)
            group["available"] += int(lot.quantity_available or 0)
            group["reserved"] += int(lot.quantity_reserved or 0)
        for lot in _cross_customer_bound_semi_lots(db, product):
            group = result[product.id]["semi_finished"]
            group["actual"] += int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0)
            group["available"] += int(lot.quantity_available or 0)
            group["reserved"] += int(lot.quantity_reserved or 0)
    return result


def _shared_finished_lots(db: Session, product: Product) -> list[InventoryLot]:
    from app.services.shared_finished_stock import candidate_lot_ids
    ids = candidate_lot_ids(db, product_id=product.id, customer_id=product.customer_id)
    if not ids:
        return []
    from app.api.mobile_erp import _lot_load_options
    lots = list(db.scalars(select(InventoryLot).options(*_lot_load_options()).where(
        InventoryLot.id.in_(ids), InventoryLot.status == "active",
        InventoryLot.inventory_type == "finished",
        or_(InventoryLot.quantity_available > 0, InventoryLot.quantity_reserved > 0))))
    return [lot for lot in lots if lot.id in ids]


def _cross_customer_bound_semi_lots(db: Session, product: Product) -> list[InventoryLot]:
    """Only explicit lot/product facts, never a code or dimension guess."""
    from app.api.mobile_erp import _lot_load_options
    return list(db.scalars(select(InventoryLot).join(
        SemiFinishedLotAllowedProduct,
        SemiFinishedLotAllowedProduct.inventory_lot_id == InventoryLot.id,
    ).join(SemiFinishedInventoryDetail,
           SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
    ).options(*_lot_load_options()).where(
        SemiFinishedLotAllowedProduct.product_id == product.id,
        or_(SemiFinishedInventoryDetail.owner_customer_id != product.customer_id,
            SemiFinishedInventoryDetail.owner_customer_id.is_(None)),
        InventoryLot.inventory_type == "semi_finished", InventoryLot.status == "active",
        or_(InventoryLot.quantity_available > 0, InventoryLot.quantity_reserved > 0))))


def product_card(product: Product, summary: dict, drawings: dict) -> dict:
    if not product.is_active and drawings.get("status") == "none":
        drawings = {"status": "unavailable_inactive", "items": []}
    return {
        "id": product.id, "product_id": product.id,
        "customer_id": product.customer_id,
        "customer_name": product.customer.chinese_short_name or product.customer.name,
        "product_code": product.product_code,
        "customer_material_code": product.customer_material_code,
        "product_name": product.product_name,
        "label": f"{product.product_code} / {product.product_name}",
        "specification": product_dimension_specification(product) or "",
        "is_active": bool(product.is_active),
        "status_label": "在用" if product.is_active else "停用（仅查历史）",
        "is_internal_component": bool(product.is_internal_component),
        "is_composite": bool(product.is_composite),
        "inventory": summary, "inventory_summary": summary,
        "drawings": drawings,
    }


def production_card(db: Session, product: Product, scope: set[int] | None,
                    *, can_see_mold_location: bool) -> dict:
    from app.services.box_type_rules import get_box_type_rule
    from app.services.sheet_cutting_settings import component_settings, theoretical_product_yield
    from app.services.production_route_contract import production_route_contract
    code = product.default_material_code or (product.material.code if product.material else None) or product.legacy_material_text
    rule = get_box_type_rule(product.box_style)
    setting = component_settings(product.sheet_cutting_settings) if product.sheet_cutting_settings else None
    snapshot = setting.contract(product.report_length_mm, product.report_width_mm).to_snapshot() if (
        setting and product.report_length_mm and product.report_width_mm) else None
    cutting_components = {}
    if product.sheet_cutting_settings:
        for component in ("whole", "cover", "base"):
            if component not in product.sheet_cutting_settings:
                continue
            values = ((product.base_report_length_mm, product.base_report_width_mm)
                      if component == "base" else (product.report_length_mm, product.report_width_mm))
            component_setting = component_settings(product.sheet_cutting_settings, component)
            cutting_components[component] = (
                component_setting.contract(*values).to_snapshot()
                if all(value is not None and value > 0 for value in values) else None)
    def work_text(value):
        text = (value or "").strip()
        return None if re.search(r"成本|利润|采购价|供应商价|单价|报价|毛利", text) else text or None

    printing = bool((product.print_content or "").strip() not in {"", "无", "无印刷", "无需印刷", "不印刷"}
                    or (product.printing_colors or "").strip() not in {"", "无", "无印刷", "无需印刷", "不印刷"})
    process = work_text(product.production_process) or ""
    joining = next((value for value in ("打钉", "粘贴") if value in process), None)
    route = production_route_contract(snapshot, process=(process,), printing=printing, joining=joining)
    steps = []
    for row in route["steps"]:
        kind = row["code"]
        if kind == "sheet_cutting":
            detail = (f"供应商纸 {snapshot['supplier_length_mm']}×{snapshot['supplier_width_mm']}mm → "
                      f"长向{snapshot['length_parts']}份、宽向{snapshot['width_parts']}份 → "
                      f"每片 {snapshot['theoretical_length_mm']}×{snapshot['theoretical_width_mm']}mm")
        elif kind == "printing":
            detail = work_text(product.print_content) or work_text(product.printing_colors) or "印刷资料待完善"
        elif kind == "die_cutting":
            detail = product.mold_tool.mold_name if product.mold_tool else "模具关联待完善"
        elif kind in {"slotting", "creasing"}:
            if not rule or rule.code != "a1_0201":
                continue
            detail = " / ".join(str(x) if x is not None else "待完善" for x in
                              (product.crease_left_mm, product.crease_middle_mm, product.crease_right_mm))
        elif kind == "joining":
            detail = joining
        else:
            detail = row["label"]
        steps.append({"label": row["label"], "detail": detail})
    if ((rule and rule.code in {"die_cut_partition", "die_cut_inner_box", "irregular"})
            or product.box_category == "die_cut" or (setting and setting.is_die_cut)):
        if not any(row["label"] == "模切" for row in steps):
            steps.append({"label": "模切", "detail": product.mold_tool.mold_name if product.mold_tool else "模具关联待完善"})
    bom = []
    for edge in db.scalars(select(ProductBomComponent).where(
            ProductBomComponent.parent_product_id == product.id).order_by(ProductBomComponent.display_order)):
        child = db.get(Product, edge.component_product_id)
        if child is None or child.deleted_at is not None or child.customer_id != product.customer_id:
            continue
        if scope is not None and child.customer_id not in scope:
            continue
        bom.append({"product_id": child.id, "product_code": child.product_code,
                    "product_name": child.product_name, "quantity": float(edge.quantity_per_set),
                    "unit": product_unit_label(child) or child.unit,
                    "specification": product_dimension_specification(child) or "",
                    "mold_name": child.mold_tool.mold_name if child.mold_tool else None})
    molds = []
    if product.mold_tool:
        mold = product.mold_tool
        from app.services.mold_location import describe_mold_location
        location = describe_mold_location(mold.rack_location) if can_see_mold_location else {}
        molds.append({"id": mold.id, "name": mold.mold_name, "is_active": bool(mold.is_active),
                      "mold_id": mold.id,
                      "location": mold.rack_location if can_see_mold_location else None,
                      "mold_cell_id": location.get("location_id"), "floor": location.get("floor"),
                      "location_label": location.get("prompt"),
                      "map_url": f"/mobile/mold-lookup?mold_id={mold.id}&readonly=1" if can_see_mold_location else None})
    return {
        "report_length_mm": product.report_length_mm,
        "report_width_mm": product.report_width_mm,
        "base_report_length_mm": product.base_report_length_mm,
        "base_report_width_mm": product.base_report_width_mm,
        "material_code": code, "flute_type": product.flute_type or (product.material.flute_type if product.material else None),
        "layer_count": product.layer_count or (product.material.layer_count if product.material else None),
        "production_process": work_text(product.production_process),
        "production_notes": work_text(product.production_notes),
        "printing_colors": work_text(product.printing_colors),
        "print_content": work_text(product.print_content),
        "crease_type": product.crease_type,
        "crease_values_mm": [product.crease_left_mm, product.crease_middle_mm, product.crease_right_mm],
        "default_cutting_mode": product.default_cutting_mode,
        "sheet_cutting_settings": product.sheet_cutting_settings,
        "cutting": snapshot,
        "cutting_components": cutting_components,
        "mold_count": setting.mold_count if setting else theoretical_product_yield(product),
        "process_steps": steps, "molds": molds, "bom": bom,
    }


def inventory_details(db: Session, product: Product):
    from app.api.mobile_erp import _inventory_group, _lot_load_options, _pending_pick_by_lot
    from app.services.location_candidates import load_warehouse_location_projection_contexts
    finished = list(db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail,
        FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id).options(*_lot_load_options()).where(
        InventoryLot.status == "active", InventoryLot.inventory_type == "finished",
        or_(InventoryLot.quantity_available > 0, InventoryLot.quantity_reserved > 0),
        FinishedGoodsInventoryDetail.product_id == product.id,
        FinishedGoodsInventoryDetail.owner_customer_id == product.customer_id,
        FinishedGoodsInventoryDetail.is_general.is_(False),
        FinishedGoodsInventoryDetail.inventory_code_snapshot == product.product_code)))
    semi = list(db.scalars(select(InventoryLot).join(SemiFinishedInventoryDetail,
        SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id).join(SemiFinishedLotAllowedProduct,
        SemiFinishedLotAllowedProduct.inventory_lot_id == InventoryLot.id).options(*_lot_load_options()).where(
        InventoryLot.status == "active", InventoryLot.inventory_type == "semi_finished",
        or_(InventoryLot.quantity_available > 0, InventoryLot.quantity_reserved > 0),
        SemiFinishedLotAllowedProduct.product_id == product.id,
        SemiFinishedInventoryDetail.owner_customer_id == product.customer_id)))
    own_semi_ids = {lot.id for lot in semi}
    cross_semi = [lot for lot in _cross_customer_bound_semi_lots(db, product) if lot.id not in own_semi_ids]
    semi.extend(cross_semi)
    own_finished_ids = {lot.id for lot in finished}
    shared = [lot for lot in _shared_finished_lots(db, product) if lot.id not in own_finished_ids]
    finished.extend(shared)
    context = load_warehouse_location_projection_contexts(db, [x.location for x in [*finished, *semi] if x.location])
    groups = {
        "finished": _inventory_group(finished, unit=product_unit_label(product) or product.unit,
                                     pending_pick_by_lot=_pending_pick_by_lot(db, [x.id for x in finished]),
                                     projection_contexts=context),
        "semi_finished": _inventory_group(semi, unit="张", projection_contexts=context),
    }
    items = []
    shared_ids = {lot.id for lot in shared}
    cross_semi_ids = {lot.id for lot in cross_semi}
    for kind, group in groups.items():
        for row in group["positions"]:
            if row["lot_id"] in (shared_ids if kind == "finished" else cross_semi_ids):
                # The approved use may belong to another customer's original
                # lot. Show physical availability without exposing its code.
                row.pop("lot_number", None)
                row["shared_confirmed"] = True
                row["map_url"] = None
                row["map_issue"] = "共用批次按显示库位核对；原批次地图入口受归属权限限制"
            items.append({"inventory_type": kind, "actual": row["quantity_total"],
                          "available": row["quantity_available"], "reserved": row["quantity_reserved"],
                          "unit": row["unit"], "location_id": row["location_id"],
                          "location_label": row.get("employee_location_name"),
                          "lot_id": row["lot_id"], "floor": row.get("floor"),
                          "mapped": row.get("map_status") == "mapped", "url": row.get("map_url")})
    return {"summary": {k: {"actual": v["quantity_total"], "available": v["quantity_available"],
                              "reserved": v["quantity_reserved"], "unit": v["unit"]} for k, v in groups.items()},
            "groups": groups, "items": items}


def order_card(db: Session, product: Product, *, include_history: bool = False):
    query = select(OrderItem, Order).join(Order, Order.id == OrderItem.order_id).where(
        OrderItem.product_id == product.id, Order.customer_id == product.customer_id)
    if not include_history:
        query = query.where(Order.status.notin_(("completed", "archived", "closed", "dead", "cancelled")))
    rows = list(db.execute(query.order_by(Order.order_date.desc(), OrderItem.id.desc()).limit(50)))
    return {"items": [{"id": item.id, "order_id": order.id, "product_id": product.id,
                       "customer_po": order.customer_po,
                       "order_number": order.order_number, "quantity": item.quantity,
                       "delivered_quantity": item.delivered_quantity, "status": order.status,
                       "deducted_quantity": None, "production_quantity": None,
                       "remaining_quantity": None,
                       "undelivered_quantity": max(item.quantity - item.delivered_quantity, 0),
                       "unit": item.sales_unit_snapshot,
                       "frozen_product": {"product_code": item.snapshot_product_code,
                                          "product_name": item.snapshot_product_name,
                                          "specification": item.snapshot_spec,
                                          "material": item.snapshot_material,
                                          "report_length_mm": item.snapshot_report_length_mm,
                                          "report_width_mm": item.snapshot_report_width_mm},
                       "delivery_date": order.delivery_date,
                       "snapshot_notice": "订单数量与资料按下单时冻结事实；当前生产资料不回写历史"}
                      for item, order in rows], "has_more": len(rows) == 50,
            "scope": "recent_50_including_history" if include_history else "recent_50_active",
            "history_included": include_history}


def action_card(db: Session, product: Product, *, can_edit_requisition: bool, can_request: bool):
    # No durable "inventory only" marker exists on Product. Dimensions and
    # price are only a warning signature; they cannot establish that status.
    suspected_placeholder = (product.length_mm == 100 and product.width_mm == 100 and
                             product.sale_unit_price == 1 and not product.production_process)
    stock_only = not product.is_active or product.deleted_at is not None
    missing_material = not (product.default_material_code or product.material_id)
    missing_report = not product.report_length_mm or not product.report_width_mm
    missing_production_data = (product.supply_mode == "corrugated_production" and
                               not product.is_composite and (missing_material or missing_report))
    policy_id = db.scalar(select(InventoryStockPolicy.id).where(
        InventoryStockPolicy.product_id == product.id,
        InventoryStockPolicy.active.is_(True),
        InventoryStockPolicy.target_inventory_type == "finished").order_by(InventoryStockPolicy.id).limit(1))
    composite_parent = bool(product.is_virtual_composite_parent or product.is_composite)
    eligible = not stock_only and not suspected_placeholder and not missing_production_data
    can_requisition = bool(can_edit_requisition and eligible and (not composite_parent or policy_id))
    return {"can_requisition": can_requisition,
            "can_request": bool(can_request and eligible and policy_id),
            "stock_only": bool(stock_only), "policy_id": policy_id,
            "is_virtual_composite_parent": bool(product.is_virtual_composite_parent),
            "action_reason": "停用品仅可查询历史" if stock_only else
                             "疑似临时占位档案，需核实生产资料" if suspected_placeholder else
                             "报料尺寸或材质待补齐" if missing_production_data else
                             "组合父件请从订单用途进入BOM报料" if composite_parent and not policy_id else None,
            "requisition_url": f"/requisition.html?reference_product_id={product.id}" if can_requisition and not composite_parent else None}
