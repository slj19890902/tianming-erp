"""Fixed storage intent; all quantities remain derived from formal lots."""
import re
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from app.models.customer import Customer
from app.models.product import Product
from app.models.fixed_shelf import ShelfBinding, ShelfLotState, ShelfProfile
from app.models.stock_replenishment import InventoryStockPolicy
from app.models.warehouse_inventory import FinishedGoodsInventoryDetail, InventoryLot, InventoryPallet, InventoryPalletItem, WarehouseLocation
from app.services.location_candidates import operational_location_issue
from app.services.warehouse_location_address import employee_location_name


class ShelfError(ValueError):
    pass


def integer_mm(value):
    """Display dimensions in whole millimetres without changing source facts."""
    return str(Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def display_specification(value):
    if not value:
        return value
    dimensions = r'(?<![\w.])\d+(?:\.\d+)?(?:\s*[×xX*]\s*\d+(?:\.\d+)?){1,2}(?:\s*mm\b)?|(?<![\w.])\d+(?:\.\d+)?\s*mm\b'
    return re.sub(dimensions, lambda match: re.sub(r'\d+(?:\.\d+)?', lambda number: integer_mm(number.group()), match.group()), str(value), flags=re.I)


def guard_pick_task_overlap(db, order_item_ids, delivery_id):
    """Serialize fixed-shelf order rows until the preceding delivery is resolved.

    Existing order reservations are shared by drafts. Do not promise the same
    reservation twice while keeping the formal reservation/dispatch ledger.
    """
    from app.models.order import OrderItem
    from app.models.delivery import Delivery, DeliveryPickTask, DeliveryPickTaskItem
    fixed = db.execute(select(OrderItem.id, OrderItem.product_id).join(ShelfProfile,
        ShelfProfile.product_id == OrderItem.product_id).where(OrderItem.id.in_(order_item_ids))).all()
    if not fixed:
        return
    db.execute(update(ShelfProfile).where(ShelfProfile.product_id.in_({x.product_id for x in fixed})).values(version=ShelfProfile.version))
    conflict = db.scalar(select(DeliveryPickTaskItem.id).join(DeliveryPickTask,
        DeliveryPickTask.id == DeliveryPickTaskItem.task_id).join(Delivery,
        Delivery.id == DeliveryPickTask.delivery_id).where(
        DeliveryPickTaskItem.order_item_id.in_([x.id for x in fixed]),
        Delivery.status == 'pending', Delivery.id != delivery_id).limit(1))
    if conflict:
        raise ShelfError('该固定货架料号的同一订单行已有未完成拿货任务，请先发货或取消原任务，避免重复找同一批货')


def physical_quantity(lot):
    return int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0) + int(lot.quantity_damaged or 0)


def location_issue(db, location, projection_context=None):
    if location is None:
        return "货位已不存在，请重新选择"
    if location.storage_type != "rack" or location.is_temporary:
        return "固定货位必须是正式货架格"
    return operational_location_issue(db, location, warehouse_types={"finished", "shared"}, require_published=True, require_map_geometry=True, required_inventory_type="finished", projection_context=projection_context)


def incoming_primary_location(db, *, product_id, customer_id, claim=False):
    """Resolve a real product binding; never infer storage from a BOM name.

    An invalid configured destination must not silently become staging stock.
    Capacity is checked again by the inventory writer after adding the lot.
    Customer preferred-area precedence is owned by the receipt target resolver.
    """
    from app.services.location_candidates import claim_active_placed_location
    product = db.get(Product, product_id)
    if product is None or product.customer_id != customer_id:
        raise ShelfError("产品与收料客户不一致")
    if claim:
        # Same configuration row lock as save_profile; do not mutate its version.
        db.execute(update(ShelfProfile).where(ShelfProfile.product_id == product_id)
                   .values(version=ShelfProfile.version))
    binding = db.scalar(select(ShelfBinding).where(
        ShelfBinding.product_id == product_id, ShelfBinding.priority == 0)
        .execution_options(populate_existing=True))
    if binding is None:
        return None
    location = db.get(WarehouseLocation, binding.location_id, populate_existing=True)
    issue = location_issue(db, location)
    if issue:
        raise ShelfError(f"固定货位不可用：{issue}")
    layout = location.floor3_layout
    if claim and not claim_active_placed_location(db, location.id,
            expected_layout_version=layout.version if layout else None):
        raise ShelfError("固定货位已变化，请刷新后重试")
    issue = location_issue(db, location)
    if issue:
        raise ShelfError(f"固定货位不可用：{issue}")
    check_contents(db, location.id, product)
    return location


def location_info(db, location, projection_context=None):
    return {"location_id": location.id, "name": employee_location_name(location),
            "address": location.location_code, "floor": location.warehouse_floor,
            "area": location.area_code, "rack": location.rack_code,
            "level": location.level_no, "slot": location.slot_no,
            "address_version": location.address_version,
            "layout_version": location.floor3_layout.version if location.floor3_layout else None,
            "sort_order": location.sort_order, "issue": location_issue(db, location, projection_context)}


def staging_issue(db, location, projection_context=None):
    if location is None or location.storage_type not in {'ground', 'temporary_aisle'}:
        return '请选择固定集货区内实际可用的地面货位'
    return operational_location_issue(db, location, warehouse_types={'finished', 'shared'},
        require_published=True, require_map_geometry=True, required_inventory_type='finished', projection_context=projection_context)


def staging_info(db, location, projection_context=None):
    return {**location_info(db, location, projection_context), 'issue': staging_issue(db, location, projection_context)}


def location_lots(db, location_id):
    return list(db.scalars(select(InventoryLot).where(InventoryLot.warehouse_location_id == location_id,
        InventoryLot.inventory_type == "finished", InventoryLot.status != "closed")).all())


def check_contents(db, location_id, product):
    total = 0
    for lot in location_lots(db, location_id):
        quantity = physical_quantity(lot)
        if not quantity:
            continue
        detail = lot.finished_detail
        if not detail or detail.product_id != product.id or detail.is_general or detail.owner_customer_id != product.customer_id:
            raise ShelfError("该格仍有其他客户、款号或通用库存，不能混放")
        total += quantity
    snapshots = db.scalars(select(InventoryPalletItem).join(InventoryPallet,
        InventoryPallet.id == InventoryPalletItem.pallet_id).where(
        InventoryPallet.location_id == location_id, InventoryPallet.is_current.is_(True),
        InventoryPalletItem.inventory_lot_id.is_(None), InventoryPalletItem.quantity > 0)).all()
    for row in snapshots:
        if row.product_id != product.id or row.customer_id != product.customer_id or row.item_type != "finished":
            raise ShelfError("该格还有未匹配或其他款的现场登记，请先核对")
        total += int(row.quantity)
    return total


def save_profile(db, product, *, expected_version, units_per_bundle, bindings, staging_location_id=None):
    if not bindings or bindings[0]["priority"] != 0 or len({x["priority"] for x in bindings}) != len(bindings):
        raise ShelfError("必须配置一个主位，补充位顺序不得重复")
    if len({x["location_id"] for x in bindings}) != len(bindings):
        raise ShelfError("不能重复选择同一货位")
    profile = db.get(ShelfProfile, product.id)
    if profile is None:
        if expected_version != 0:
            raise ShelfError("货位配置已变化，请刷新")
        profile = ShelfProfile(product_id=product.id, units_per_bundle=units_per_bundle, version=1)
        db.add(profile)
        db.flush()
    else:
        result = db.execute(update(ShelfProfile).where(ShelfProfile.product_id == product.id,
            ShelfProfile.version == expected_version).values(units_per_bundle=units_per_bundle, version=ShelfProfile.version + 1))
        if result.rowcount != 1:
            raise ShelfError("货位配置已变化，请刷新")
    from app.models.order import OrderItem
    from app.models.delivery import Delivery, DeliveryPickTask, DeliveryPickTaskItem
    duplicate = db.scalar(select(OrderItem.id).join(DeliveryPickTaskItem,
        DeliveryPickTaskItem.order_item_id == OrderItem.id).join(DeliveryPickTask,
        DeliveryPickTask.id == DeliveryPickTaskItem.task_id).join(Delivery,
        Delivery.id == DeliveryPickTask.delivery_id).where(OrderItem.product_id == product.id,
        Delivery.status == 'pending').group_by(OrderItem.id).having(func.count(func.distinct(Delivery.id)) > 1).limit(1))
    if duplicate:
        raise ShelfError('该料号已有同一订单行的多张待发拿货任务，请先处理重复任务再启用固定货架')
    if staging_location_id:
        issue = staging_issue(db, db.get(WarehouseLocation, staging_location_id))
        if issue:
            raise ShelfError(issue)
    profile = db.get(ShelfProfile, product.id)
    if profile.staging_location_id != staging_location_id:
        staged = db.scalars(select(InventoryLot).join(ShelfLotState, ShelfLotState.lot_id == InventoryLot.id)
            .join(FinishedGoodsInventoryDetail, FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id)
            .where(FinishedGoodsInventoryDetail.product_id == product.id, ShelfLotState.staged_delivery_item_id.is_not(None))).all()
        if any(physical_quantity(lot) for lot in staged):
            raise ShelfError('该料号已有实际集货，请先处理已集货物，再更改固定集货区')
    profile.staging_location_id = staging_location_id
    old = list(db.scalars(select(ShelfBinding).where(ShelfBinding.product_id == product.id)).all())
    selected = {x["location_id"] for x in bindings}
    for row in old:
        if row.location_id in selected:
            continue
        if any(physical_quantity(lot) for lot in location_lots(db, row.location_id)):
            raise ShelfError("原货位还有库存，请先完成实物移库再解除绑定")
        if db.scalar(select(ShelfLotState.lot_id).join(InventoryLot, InventoryLot.id == ShelfLotState.lot_id).where(
            ShelfLotState.target_location_id == row.location_id, InventoryLot.status != "closed",
            InventoryLot.quantity_available + InventoryLot.quantity_reserved > 0).limit(1)):
            raise ShelfError("原货位还有待上架批次，请先处理")
    for row in bindings:
        location = db.get(WarehouseLocation, row["location_id"])
        issue = location_issue(db, location)
        if issue:
            raise ShelfError(issue)
        if location.address_version != row["address_version"]:
            raise ShelfError("货架地址已被调整，请刷新后重新选择")
        from app.services.warehouse_inventory import _claim_inventory_destination
        _claim_inventory_destination(db, location.id, expected_layout_version=row.get("layout_version"))
        existing = db.get(ShelfBinding, location.id)
        if existing and existing.product_id != product.id:
            raise ShelfError("该格已绑定其他客户料号")
        quantity = check_contents(db, location.id, product)
        if row.get("capacity") is not None and quantity > row["capacity"]:
            raise ShelfError("最大存放量不能小于当前实物数量")
    db.execute(delete(ShelfBinding).where(ShelfBinding.product_id == product.id))
    db.flush()
    for row in bindings:
        db.add(ShelfBinding(location_id=row["location_id"], product_id=product.id,
            priority=row["priority"], capacity=row.get("capacity")))
    db.flush()
    db.expire_all()
    return profile_info(db, product)


def profile_info(db, product):
    profile = db.get(ShelfProfile, product.id)
    customer = db.get(Customer, product.customer_id)
    policies = list(db.scalars(select(InventoryStockPolicy).where(InventoryStockPolicy.product_id == product.id,
        InventoryStockPolicy.customer_id == product.customer_id,
        InventoryStockPolicy.target_inventory_type == "finished", InventoryStockPolicy.active.is_(True))).all())
    warning = policies[0].warning_quantity if len(policies) == 1 else None
    rows = db.scalars(select(ShelfBinding).where(ShelfBinding.product_id == product.id).order_by(ShelfBinding.priority)).all()
    locations = []
    for binding in rows:
        loc = db.get(WarehouseLocation, binding.location_id)
        lots = location_lots(db, binding.location_id)
        locations.append({**location_info(db, loc), "priority": binding.priority, "capacity": binding.capacity,
            "physical_quantity": sum(physical_quantity(lot) for lot in lots),
            "available_quantity": sum(int(lot.quantity_available or 0) for lot in lots if lot.status == "active")})
    primary_available = next((x['available_quantity'] for x in locations if x['priority'] == 0 and not x['issue']), 0)
    overflow_available = sum(x['available_quantity'] for x in locations if x['priority'] > 0 and not x['issue'])
    refill = min(max((warning or 0) - primary_available, 0), overflow_available)
    primary = next((x for x in locations if x['priority'] == 0 and not x['issue']), None)
    if not primary:
        refill = 0
    elif primary['capacity'] is not None:
        refill = min(refill, max(primary['capacity'] - primary['physical_quantity'], 0))
    return {"product_id": product.id, "customer_id": product.customer_id,
        "customer_name": customer.name if customer else "",
        "customer_short_name": customer.chinese_short_name if customer else None,
        "inventory_code": product.customer_material_code, "product_name": product.product_name,
        "specification": " × ".join(integer_mm(x) for x in (product.length_mm, product.width_mm, product.height_mm) if x is not None) + " mm",
        "units_per_bundle": profile.units_per_bundle if profile else None,
        "staging_location_id": profile.staging_location_id if profile else None,
        "version": profile.version if profile else 0,
        "warning_quantity": warning, "warning_policy_count": len(policies), "locations": locations,
        "shelf_refill_reference_quantity": refill}


def on_finished_in(db, lot):
    detail = lot.finished_detail
    if not detail or detail.is_general:
        return
    profile = db.get(ShelfProfile, detail.product_id)
    if not profile:
        return
    own = db.get(ShelfBinding, lot.warehouse_location_id)
    primary = db.scalar(select(ShelfBinding).where(ShelfBinding.product_id == detail.product_id, ShelfBinding.priority == 0))
    db.add(ShelfLotState(lot_id=lot.id, units_per_bundle=profile.units_per_bundle,
        target_location_id=primary.location_id if primary and (not own or own.product_id != detail.product_id) else None))


def assert_destination(db, lot):
    binding = db.get(ShelfBinding, lot.warehouse_location_id)
    if not binding:
        return
    product = db.get(Product, binding.product_id)
    quantity = check_contents(db, binding.location_id, product)
    if binding.capacity is not None and quantity > binding.capacity:
        raise ShelfError("固定货位放不下，请选择同款补充位或保留待上架")


def copy_lot_state(db, source_lot, target_lot):
    state = db.get(ShelfLotState, source_lot.id)
    if state is None:
        return
    target = db.get(ShelfLotState, target_lot.id)
    if state.staged_delivery_item_id:
        from app.services.fixed_shelf_staging import follow_staged_allocation_transfer
        follow_staged_allocation_transfer(db, source_lot, target_lot, state.staged_delivery_item_id)
    binding = db.get(ShelfBinding, target_lot.warehouse_location_id)
    target_id = None if binding and binding.product_id == target_lot.finished_detail.product_id else state.target_location_id
    staged_item_id = None if binding and binding.product_id == target_lot.finished_detail.product_id else state.staged_delivery_item_id
    if target is None:
        db.add(ShelfLotState(lot_id=target_lot.id, units_per_bundle=state.units_per_bundle, target_location_id=target_id,
            staged_delivery_item_id=staged_item_id))
    else:
        target.target_location_id = target_id
        target.staged_delivery_item_id = staged_item_id


def bundle_breakdown(quantity, units):
    if not units:
        return {"units_per_bundle": None, "bundle_count": None, "loose_quantity": None, "bundle_text": "每捆数量待核"}
    bundles, loose = divmod(int(quantity), int(units))
    return {"units_per_bundle": units, "bundle_count": bundles, "loose_quantity": loose,
        "bundle_text": f"{bundles}捆" + (f" + {loose}散只" if loose else "") + f"（{units}只/捆）"}


def enrich_pick_groups(db, groups):
    ids = {int(line["lot_id"]) for group in groups for line in group.get("lines", []) if line.get("lot_id")}
    states = {row.lot_id: row for row in db.scalars(select(ShelfLotState).where(ShelfLotState.lot_id.in_(ids))).all()} if ids else {}
    codes = dict(db.execute(select(FinishedGoodsInventoryDetail.inventory_lot_id, Product.customer_material_code)
        .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .where(FinishedGoodsInventoryDetail.inventory_lot_id.in_(ids))).all()) if ids else {}
    for group in groups:
        for line in group.get("lines", []):
            line['specification_display'] = display_specification(line.get('specification'))
            state = states.get(line.get("lot_id"))
            line['customer_inventory_code'] = codes.get(line.get('lot_id'))
            line.update(bundle_breakdown(line.get("pick_quantity", 0), state.units_per_bundle if state else None))
            if state and state.target_location_id:
                line["putaway_pending"] = True
                line["requires_attention"] = True
                group["requires_attention"] = True
