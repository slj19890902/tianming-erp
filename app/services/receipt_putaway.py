"""Real-map receipt routing, shared cells, and explicit cancellation semantics."""
from sqlalchemy import select, update, func
from app.models.receipt_putaway import ProductStoragePreference, ReceiptStagingArea
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor, WarehouseLocation, InventoryLot
from app.services.fixed_shelf import ShelfError, location_info
from app.services.location_candidates import operational_location_issue, claim_active_placed_location


def is_staging_area(db, area):
    return bool(area is not None and db.get(ReceiptStagingArea, area.id) is not None)


def is_staging_location(db, location):
    if location is None or location.warehouse_floor != 1:
        return False
    area = db.scalar(select(WarehouseArea).join(WarehouseFloor).where(
        WarehouseFloor.floor_number == 1, WarehouseArea.area_code == location.area_code))
    return is_staging_area(db, area)


def location_issue(db, location, inventory_type="finished", *, projection_context=None):
    if location is None:
        return "货位不存在"
    return operational_location_issue(db, location, warehouse_types={inventory_type, "shared"},
        require_published=True, require_map_geometry=True, required_inventory_type=inventory_type,
        projection_context=projection_context)


def info(db, product_id):
    row = db.get(ProductStoragePreference, product_id)
    location = db.get(WarehouseLocation, row.location_id) if row and row.location_id else None
    return {"product_id": product_id, "version": row.version if row else 0,
        "area_id": row.area_id if row else None, "location_id": row.location_id if row else None,
        "location": location_info(db, location) if location else None,
        "configured": row is not None, "issue": location_issue(db, location) if row and row.location_id else None}


def save(db, product_id, *, expected_version, area_id, location_id, address_version=None, layout_version=None):
    db.execute(update(Product).where(Product.id == product_id).values(product_code=Product.product_code))
    area = db.get(WarehouseArea, area_id) if area_id else None
    if area_id and (area is None or area.construction_status != "enabled"):
        raise ShelfError("区域未启用，请重新选择")
    if location_id:
        location = db.get(WarehouseLocation, location_id)
        issue = location_issue(db, location)
        if issue:
            raise ShelfError(issue)
        if not area or area.area_code != location.area_code or area.floor.floor_number != location.warehouse_floor:
            raise ShelfError("货位不属于所选区域")
        if address_version != location.address_version or not location.floor3_layout or layout_version != location.floor3_layout.version:
            raise ShelfError("货位身份已变化，请刷新后重新选择")
        if not claim_active_placed_location(db, location.id, expected_layout_version=layout_version):
            raise ShelfError("货位正在调整，请刷新后重试")
    elif area:
        rows = db.scalars(select(WarehouseLocation).where(WarehouseLocation.area_code == area.area_code,
            WarehouseLocation.warehouse_floor == area.floor.floor_number, WarehouseLocation.is_active.is_(True))).all()
        if not any(location_issue(db, loc) is None for loc in rows):
            raise ShelfError("该区域没有可用的正式成品货位")
    row = db.get(ProductStoragePreference, product_id)
    if row is None:
        if expected_version != 0:
            raise ShelfError("配置已变化，请刷新")
        db.add(ProductStoragePreference(product_id=product_id, area_id=area_id, location_id=location_id, version=1))
    else:
        changed = db.execute(update(ProductStoragePreference).where(
            ProductStoragePreference.product_id == product_id, ProductStoragePreference.version == expected_version)
            .values(area_id=area_id, location_id=location_id, version=expected_version + 1))
        if changed.rowcount != 1:
            raise ShelfError("配置已被其他人修改，请刷新")
        db.expire(row)
    db.flush()
    return info(db, product_id)


def remember_stocktake(db, lot, operator_id, *, follow_position=False):
    operator = db.get(User, operator_id) if operator_id else None
    if not operator or operator.role not in {"admin", "boss"}:
        return
    if lot.finished_detail and not lot.finished_detail.is_general:
        product_id = lot.finished_detail.product_id
        customer_id = lot.finished_detail.owner_customer_id
    elif lot.semi_finished_detail and len(lot.allowed_products) == 1:
        product_id = lot.allowed_products[0].product_id
        customer_id = lot.semi_finished_detail.owner_customer_id
    else:
        return
    product = db.get(Product, product_id)
    if not product or product.customer_id != customer_id or not product.is_active or product.deleted_at:
        return
    # Serialize against explicit edits and other stocktakes for this product.
    db.execute(update(Product).where(Product.id == product_id).values(product_code=Product.product_code))
    previous = db.get(ProductStoragePreference, product_id)
    if previous is not None and not follow_position:
        return
    from app.models.fixed_shelf import ShelfBinding
    if not follow_position and db.scalar(select(ShelfBinding.location_id).where(ShelfBinding.product_id == product_id).limit(1)):
        return
    location = db.get(WarehouseLocation, lot.warehouse_location_id)
    if not location or location_issue(db, location) or is_staging_location(db, location):
        return
    area = db.scalar(select(WarehouseArea).join(WarehouseFloor).where(
        WarehouseFloor.floor_number == location.warehouse_floor, WarehouseArea.area_code == location.area_code))
    if area:
        if previous and previous.location_id == location.id and previous.area_id == area.id:
            return
        before_location_id = previous.location_id if previous else None
        save(db, product_id, expected_version=previous.version if previous else 0,
             area_id=area.id, location_id=location.id, address_version=location.address_version,
             layout_version=location.floor3_layout.version)
        from app.services.audit_log import append_audit_event
        append_audit_event(db, event_category="business", result="success", source="web", module_code="warehouse",
            action_code="warehouse.receipt_storage.remember", resource="product", actor=operator,
            object_ref=str(product_id), details={"lot_id": lot.id, "location_id": location.id,
                "before_location_id": before_location_id, "follow_position": follow_position})
        db.flush()


def resolve(db, *, product_id=None, customer_id=None, claim=False, inventory_type="finished",
            excluded_location_ids=None):
    """Product destination first; defer customer areas and legacy maps to the caller.

    Missing/cleared preferences are optional, never inferred from historical stock.
    """
    excluded = excluded_location_ids or set()
    staging_ids = set(db.scalars(select(ReceiptStagingArea.area_id)).all())
    warning = None
    pref = None
    if product_id is not None:
        product = db.get(Product, product_id)
        if not product or product.customer_id != customer_id or product.deleted_at is not None:
            raise ShelfError("产品与收料客户不一致")
        if claim:
            db.execute(update(Product).where(Product.id == product_id).values(product_code=Product.product_code))
        pref = db.get(ProductStoragePreference, product_id, populate_existing=True)
    candidates = []
    if pref and pref.area_id:
        if pref.location_id:
            candidates = [db.get(WarehouseLocation, pref.location_id)]
        else:
            area = db.get(WarehouseArea, pref.area_id)
            if area:
                candidates = list(db.scalars(select(WarehouseLocation).where(WarehouseLocation.warehouse_floor == area.floor.floor_number,
                    WarehouseLocation.area_code == area.area_code, WarehouseLocation.is_active.is_(True)).order_by(WarehouseLocation.id)).all())
        warning = "产品默认货位不可用"
    elif pref is None and product_id:
        from app.services.fixed_shelf import incoming_primary_location
        if inventory_type == 'finished':
            try:
                old = incoming_primary_location(db, product_id=product_id, customer_id=customer_id, claim=claim)
                if old:
                    if old.id in excluded:
                        raise ShelfError("固定货位已被本批次占用，请核对分配")
                    return old, "fixed_shelf", None
            except ShelfError as exc:
                raise ShelfError(f"固定货位不可用：{exc}") from exc
    from app.services.location_candidates import load_warehouse_location_projection_contexts
    contexts = load_warehouse_location_projection_contexts(db, [loc for loc in candidates if loc is not None]) if candidates else {}
    for loc in candidates:
        if loc is None or loc.id in excluded or location_issue(db, loc, inventory_type, projection_context=contexts.get(loc.id)):
            continue
        if pref is not None and pref.location_id is None and operational_location_issue(
                db, loc, warehouse_types={inventory_type, 'shared'},
                require_published=True, require_map_geometry=True,
                required_inventory_type=inventory_type, require_empty=True, projection_context=contexts.get(loc.id)):
            continue
        # Existing exclusive picking bindings remain protected even in mixed cells.
        from app.models.fixed_shelf import ShelfBinding
        binding = db.get(ShelfBinding, loc.id)
        if binding and binding.product_id != product_id:
            continue
        if not claim or claim_active_placed_location(db, loc.id, expected_layout_version=loc.floor3_layout.version):
            return loc, "product_storage", None
    if pref and pref.location_id:
        issue=location_issue(db,db.get(WarehouseLocation,pref.location_id),inventory_type)
        raise ShelfError(f"产品默认货位不可用：{issue or '固定货位被其他产品占用'}；请重新选择正式货位")
    if pref and pref.area_id:
        raise ShelfError("产品指定区域当前没有可用的正式成品货位；请在常用箱中核对区域或选择具体货位")
    from app.services.customer_finished_storage import ordered_preferred_area_ids
    if customer_id is not None and ordered_preferred_area_ids(db, customer_id):
        # The workflow owns customer-area selection and its claim/revalidation.
        # Never let the generic first-floor fallback override a saved customer area.
        return None
    if not staging_ids:
        return None
    staging = []
    blocked_areas = []
    for area in db.scalars(select(WarehouseArea).where(WarehouseArea.id.in_(staging_ids))).all():
        if area.floor.floor_number != 1:
            continue
        area_locations = db.scalars(select(WarehouseLocation).where(WarehouseLocation.warehouse_floor == 1,
                WarehouseLocation.area_code == area.area_code, WarehouseLocation.is_active.is_(True))).all()
        # One bounded read per area, not a complete map/occupancy reload for
        # every candidate cell. These values live only within this resolution;
        # a receipt save still resolves again and claims the chosen version.
        contexts = load_warehouse_location_projection_contexts(db, area_locations)
        location_ids = [loc.id for loc in area_locations]
        from app.models.fixed_shelf import ShelfBinding
        bindings = {b.location_id: b for b in db.scalars(select(ShelfBinding).where(
            ShelfBinding.location_id.in_(location_ids)))} if location_ids else {}
        physical_by_location = dict(db.execute(select(InventoryLot.warehouse_location_id,
            func.sum(InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged))
            .where(InventoryLot.warehouse_location_id.in_(location_ids), InventoryLot.status.in_(["active", "frozen"]))
            .group_by(InventoryLot.warehouse_location_id)).all()) if location_ids else {}
        reasons = set()
        for loc in area_locations:
            if loc.id in excluded:
                continue
            binding = bindings.get(loc.id)
            if binding and binding.product_id != product_id:
                continue
            issue = location_issue(db, loc, inventory_type, projection_context=contexts.get(loc.id))
            if issue is None:
                physical = int(physical_by_location.get(loc.id) or 0)
                staging.append((physical, loc.id, loc))
            else:
                reasons.add(issue)
        if not area_locations:
            reasons.add("没有启用货位")
        if reasons:
            blocked_areas.append(f"{area.area_code}：{'、'.join(sorted(reasons))}")
    for _, _, loc in sorted(staging, key=lambda x: (x[0], x[1])):
        if not claim or claim_active_placed_location(db, loc.id, expected_layout_version=loc.floor3_layout.version):
            return loc, "receipt_staging", warning or "待入库区可混放，容量仅提醒；请按现场空间及时归位"
    detail = "；".join(blocked_areas[:4]) or "货位版本正在变化，请刷新后重试"
    raise ShelfError("一楼待入库区没有可用的已发布货位；" + detail
                     + "。请管理员进入仓库地图核对待入库区域的发布状态；本次未入库")


def placement_state(db, lot):
    if not lot or lot.status not in {"active", "frozen"} or sum(int(x or 0) for x in
            [lot.quantity_available,lot.quantity_reserved,lot.quantity_damaged]) <= 0:
        return {"label":"", "color":""}
    location=db.get(WarehouseLocation,lot.warehouse_location_id)
    if is_staging_location(db,location):
        return {"label":"待归位", "color":"orange"}
    from app.models.warehouse_inventory import InventoryMovement, InventoryLotTransfer, InventoryLocationMovement
    moved=db.scalar(select(InventoryLotTransfer.id).where(InventoryLotTransfer.target_lot_id==lot.id).limit(1))
    if not moved:
        moved=db.scalar(select(InventoryMovement.id).where(InventoryMovement.inventory_lot_id==lot.id,
            InventoryMovement.movement_type=="location_transfer").limit(1))
    if not moved and lot.pallet_item:
        moved=db.scalar(select(InventoryLocationMovement.id).where(
            InventoryLocationMovement.pallet_id==lot.pallet_item.pallet_id,
            InventoryLocationMovement.movement_type=="move",InventoryLocationMovement.to_location_id==lot.warehouse_location_id).limit(1))
    if moved:
        return {"label":"人工归位", "color":"blue"}
    auto=db.scalar(select(InventoryMovement.id).where(InventoryMovement.inventory_lot_id==lot.id,
        InventoryMovement.movement_type=="manual_in",InventoryMovement.reason=="自动入位").limit(1))
    if auto:
        return {"label":"自动入位", "color":"green"}
    if lot.source_type in {"manual", "stocktake"}:
        return {"label":"人工入库", "color":"blue"}
    return {"label":"已入库", "color":"neutral"}


def pending_completion_filter(db, query):
    from app.models.production import ProductionCompletion
    codes=db.scalars(select(WarehouseArea.area_code).join(ReceiptStagingArea)).all()
    if not codes:
        return query
    pending_lots=select(InventoryLot.id).join(WarehouseLocation, WarehouseLocation.id==InventoryLot.warehouse_location_id).where(
        WarehouseLocation.warehouse_floor==1, WarehouseLocation.area_code.in_(codes),
        InventoryLot.status.in_(["active","frozen"]),InventoryLot.quantity_available+InventoryLot.quantity_reserved>0)
    return query.where(ProductionCompletion.inventory_lot_id.in_(pending_lots))
