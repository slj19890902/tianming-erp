"""A narrow, explicit exception to customer-specific finished stock identity.

Never infer interchangeability from a code or mold. A confirmed lot keeps its
original owner, identity, costs and quantity ledger. Members and reservations
capture identities so a later master edit cannot reinterpret old allocations.
"""
import hashlib
import json
from sqlalchemy import select, update
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from app.models.shared_finished_stock import (
    SharedFinishedGroup, SharedFinishedMember, SharedFinishedLot, SharedFinishedReservation,
)
from app.services.finished_stock_identity import product_basis, matches_stock_identity, _value


EXTRA_FIELDS = (
    "supply_mode", "report_length_mm", "report_width_mm", "base_report_length_mm",
    "base_report_width_mm", "print_content", "printing_colors", "printing_plate_mode",
    "printing_plate_1_id", "printing_plate_2_id", "printing_plate_3_id", "surface_paper_type",
    "mold_count", "cutting_rows", "cutting_cols", "sheet_cutting_settings",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _error(message, status=409):
    from app.services.warehouse_inventory import WarehouseInventoryError
    return WarehouseInventoryError(message, status)


def member_identity(product):
    basis = json.loads(product_basis(product))
    basis.pop("product_id")
    return _json(dict(basis=basis, extra={
        key: getattr(product, key, None) if key == "sheet_cutting_settings"
        else _value(getattr(product, key, None)) for key in EXTRA_FIELDS}))


def lot_identity(lot):
    detail = lot.finished_detail
    return _json(dict(product_id=detail.product_id, customer_id=detail.owner_customer_id,
        code=detail.inventory_code_snapshot, general=detail.is_general, unit=lot.unit,
        physical_basis=detail.physical_basis_json))


def _ordinary(db, product):
    from app.models.multilevel_bom import ProductBomProfile
    return (product is not None and product.deleted_at is None and product.purged_at is None and product.is_active
        and product.supply_mode == "corrugated_production" and not product.is_composite
        and not product.is_virtual_composite_parent
        and db.get(ProductBomProfile, product.id) is None)


def preview(db, *, product_ids, lot_ids):
    pids, lids = sorted(set(product_ids)), sorted(set(lot_ids))
    if len(pids) < 2 or len(pids) > 20 or not lids or len(lids) > 100:
        raise _error("请指定2至20个可互换产品及1至100个已核实的成品批次")
    products = [db.get(Product, pid) for pid in pids]
    if not all(_ordinary(db, p) for p in products):
        raise _error("首期共用仅支持有效的普通自制成品，不支持组合品或外购换算")
    if len({p.customer_id for p in products}) != len(products):
        raise _error("每个客户在同一共用组只能指定一个产品")
    identities = [member_identity(p) for p in products]
    if len(set(identities)) != 1:
        raise _error("产品的单位、规格、材质、模具、印刷或工艺不同，不能直接共用")
    if any(db.get(SharedFinishedMember, pid) for pid in pids):
        raise _error("所选产品已有共用关联，请先核对原关联")
    lots = []
    for lid in lids:
        lot = db.get(InventoryLot, lid)
        if (lot is None or lot.inventory_type != "finished" or lot.status != "active"
                or lot.finished_detail is None or lot.finished_detail.is_general
                or lot.quantity_reserved or lot.quantity_available <= 0
                or lot.finished_detail.product_id not in pids or db.get(SharedFinishedLot, lid)):
            raise _error("仅可确认尚未预占、有可用余额且属于成员产品的正常专用成品批次")
        owner = products[pids.index(lot.finished_detail.product_id)]
        if lot.finished_detail.owner_customer_id != owner.customer_id:
            raise _error("批次客户和产品归属不一致")
        from app.services.bom_inventory_contract import is_body_lot
        from app.services.fixed_shelf_staging import staging_owner
        from app.services.bom_subkits import require_free_subkit_stock
        if is_body_lot(lot) or staging_owner(db, lot.id):
            raise _error("本体或已集货批次不能加入共用")
        require_free_subkit_stock(db, lot)
        # Old frozen mold IDs may differ after an explicit mold consolidation.
        # The preview retains that exact document; it never silently normalizes it.
        try:
            frozen = json.loads(lot.finished_detail.physical_basis_json)
        except (ValueError, TypeError):
            raise _error("批次缺少冻结实物身份，须先核实") from None
        if (frozen.get("product_id") != owner.id or frozen.get("unit") != owner.unit
                or frozen.get("assembly") or frozen.get("quantity_basis") or lot.unit != "boxes"):
            raise _error("批次单位或结构不适合普通成品共用")
        current = json.loads(product_basis(owner))
        if any(value != current.get(key) for key, value in frozen.items()
               if key not in {"mold_tool_id", "quantity_basis"}):
            raise _error("批次冻结规格与产品不符；历史模具归档之外的差异须单独核实")
        lots.append(dict(lot_id=lid, version=lot.version, available=lot.quantity_available,
                         identity_json=lot_identity(lot)))
    result = dict(schema=1, products=[dict(product_id=p.id, customer_id=p.customer_id,
        version=p.version, code=p.product_code, name=p.product_name,
        identity_json=identity, product_basis_json=product_basis(p))
        for p, identity in zip(products, identities)], lots=lots)
    result["preview_hash"] = hashlib.sha256(_json(result).encode()).hexdigest()
    return result


def confirm(db, *, product_ids, lot_ids, preview_hash, operation_key, evidence, actor):
    """Only an authenticated active administrator can create a sharing fact."""
    from app.services.bom_transactions import atomic_bom
    from app.services.audit_log import append_audit_event
    if actor is None or not actor.is_active or actor.role != "admin":
        raise _error("仅活动管理员可确认成品共用", 403)
    if not evidence or not evidence.strip() or len(evidence) > 1000:
        raise _error("请记录现场确认依据")
    request = _json(dict(product_ids=sorted(set(product_ids)), lot_ids=sorted(set(lot_ids)),
        preview_hash=preview_hash, evidence=evidence.strip(), actor_id=actor.id))
    with atomic_bom(db):
        previous = db.scalar(select(SharedFinishedGroup).where(SharedFinishedGroup.operation_key == operation_key))
        if previous is not None:
            if previous.request_json != request:
                raise _error("操作标识已用于另一组共用确认")
            return dict(group_id=previous.id, replayed=True)
        value = preview(db, product_ids=product_ids, lot_ids=lot_ids)
        if value["preview_hash"] != preview_hash:
            raise _error("产品或库存已变化，请重新核对共用预览")
        for row in value["products"]:
            locked = db.execute(update(Product).where(Product.id == row["product_id"],
                Product.version == row["version"]).values(version=Product.version, updated_at=Product.updated_at))
            if locked.rowcount != 1:
                raise _error("产品已变化，请重新预览")
        for row in value["lots"]:
            locked = db.execute(update(InventoryLot).where(InventoryLot.id == row["lot_id"],
                InventoryLot.version == row["version"], InventoryLot.quantity_reserved == 0,
                InventoryLot.status == "active").values(version=InventoryLot.version + 1))
            if locked.rowcount != 1:
                raise _error("库存已变化，请重新预览")
        group = SharedFinishedGroup(operation_key=operation_key, request_json=request,
            evidence=evidence.strip(), actor_id=actor.id)
        db.add(group)
        db.flush()
        for row in value["products"]:
            db.add(SharedFinishedMember(group_id=group.id, product_id=row["product_id"],
                customer_id=row["customer_id"], identity_json=row["identity_json"],
                product_basis_json=row["product_basis_json"]))
        for row in value["lots"]:
            db.add(SharedFinishedLot(group_id=group.id, lot_id=row["lot_id"], identity_json=row["identity_json"]))
        append_audit_event(db, event_category="business", result="success", source="web",
            module_code="warehouse", action_code="confirm_shared_finished_stock", resource="inventory_lot",
            actor=actor, entity_type="shared_finished_group", entity_id=group.id,
            details=dict(product_ids=sorted(set(product_ids)), lot_ids=sorted(set(lot_ids)),
                         preview_hash=preview_hash, evidence=evidence.strip()))
        db.flush()
        return dict(group_id=group.id, replayed=False)


def match(db, lot, *, product_id, customer_id, expected_basis=None, lock=False):
    """Return frozen member evidence only for this expressly approved live lot."""
    member = db.get(SharedFinishedMember, product_id)
    approved = db.get(SharedFinishedLot, lot.id)
    if member is None or approved is None or member.customer_id != customer_id or member.group_id != approved.group_id:
        return None
    group = db.get(SharedFinishedGroup, member.group_id)
    if group is None or not group.enabled:
        return None
    product = db.get(Product, product_id)
    if not _ordinary(db, product) or product.customer_id != customer_id or member_identity(product) != member.identity_json:
        return None
    if lot.finished_detail is None or lot_identity(lot) != approved.identity_json:
        return None
    if expected_basis is not None and not matches_stock_identity(member.product_basis_json, expected_basis):
        return None
    if lock:
        result = db.execute(update(SharedFinishedGroup).where(SharedFinishedGroup.id == group.id,
            SharedFinishedGroup.enabled.is_(True), SharedFinishedGroup.version == group.version)
            .values(version=SharedFinishedGroup.version))
        if result.rowcount != 1:
            raise _error("共用关联已变化，请刷新库存")
        result = db.execute(update(Product).where(Product.id == product.id, Product.version == product.version)
            .values(version=Product.version, updated_at=Product.updated_at))
        if result.rowcount != 1:
            raise _error("产品已变化，请刷新库存")
    return member


def candidate_lot_ids(db, *, product_id, customer_id, expected_basis=None):
    member = db.get(SharedFinishedMember, product_id)
    if member is None or member.customer_id != customer_id:
        return []
    lots = db.scalars(select(InventoryLot).join(SharedFinishedLot, SharedFinishedLot.lot_id == InventoryLot.id)
        .where(SharedFinishedLot.group_id == member.group_id, InventoryLot.status == "active",
               InventoryLot.inventory_type == "finished")).all()
    return [lot.id for lot in lots if match(db, lot, product_id=product_id,
        customer_id=customer_id, expected_basis=expected_basis)]


def freeze_reservation(db, reservation, member, lot):
    if member is not None:
        db.add(SharedFinishedReservation(reservation_id=reservation.id, group_id=member.group_id,
            product_id=member.product_id, customer_id=member.customer_id,
            lot_identity_json=lot_identity(lot), product_basis_json=member.product_basis_json))


def reserved_match(db, reservation, lot, *, product_id, customer_id):
    fact = db.get(SharedFinishedReservation, reservation.id)
    if fact is None:
        return False
    if not (fact.product_id == product_id and fact.customer_id == customer_id
            and lot.finished_detail is not None and fact.lot_identity_json == lot_identity(lot)):
        raise _error("共用预占与冻结批次身份不一致，请核实后再出库")
    return True


def inherit_lot(db, source, target):
    if source.id == target.id:
        return
    fact = db.get(SharedFinishedLot, source.id)
    if fact is not None:
        if lot_identity(source) != fact.identity_json or lot_identity(target) != fact.identity_json:
            raise _error("共用批次移库前后实物身份不一致")
        db.add(SharedFinishedLot(lot_id=target.id, group_id=fact.group_id,
            identity_json=fact.identity_json, source_lot_id=source.id))


def inherit_reservation(db, source, target):
    fact = db.get(SharedFinishedReservation, source.id)
    if fact is not None:
        db.add(SharedFinishedReservation(reservation_id=target.id, group_id=fact.group_id,
            product_id=fact.product_id, customer_id=fact.customer_id,
            lot_identity_json=fact.lot_identity_json, product_basis_json=fact.product_basis_json))


def candidate_projection(db, lot, *, product_id, customer_id):
    member = match(db, lot, product_id=product_id, customer_id=customer_id)
    if member is None:
        return {}
    product = db.get(Product, product_id)
    return dict(shared_stock=True, customer_name="已确认共用库存",
        inventory_code=product.product_code, product_name=product.product_name,
        shared_stock_notice="已确认共用成品；各客户使用同一库存余额。")
