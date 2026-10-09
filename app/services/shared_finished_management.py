"""Versioned management of explicit shared finished-stock groups."""
from app.services.product_unit_labels import product_unit_label
import hashlib
import json
from sqlalchemy import select, update
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail
from app.models.shared_finished_stock import (
    SharedFinishedGroup, SharedFinishedMember, SharedFinishedLot,
    SharedFinishedPolicy, SharedFinishedMutation,
    SharedFinishedOrderBasis,
)
from app.services import shared_finished_stock as shared
from app.services.bom_transactions import atomic_bom
from app.services.audit_log import append_audit_event


def member_ids(db, group_id):
    group = db.get(SharedFinishedGroup, group_id)
    if group is None:
        raise shared._error("共用组不存在", 404)
    return sorted(db.scalars(select(SharedFinishedMember.product_id).where(
        SharedFinishedMember.group_id == group_id)).all())


def _policy(db, group_id):
    policy = db.get(SharedFinishedPolicy, group_id)
    return bool(policy and policy.auto_enroll)


def product_summary(product):
    basis = json.loads(shared.product_basis(product))
    return dict(product_id=product.id, customer_id=product.customer_id,
        customer_name=product.customer.name if product.customer else "",
        code=product.product_code, name=product.product_name, unit=product_unit_label(product),
        spec=basis["spec"], material=basis["material"], mold_id=product.mold_tool_id,
        mold_label=(product.mold_tool.label_name or product.mold_tool.mold_code) if product.mold_tool else "不使用模具",
        process=product.production_process, version=product.version,
        role_label='BOM整套' if product.is_composite else 'BOM零件' if product.is_internal_component else '普通成品')


def product_lots(db, product_ids, *, group_id=None):
    products = [db.get(Product, pid) for pid in product_ids]
    rows = db.scalars(select(InventoryLot).join(InventoryLot.finished_detail).where(
        InventoryLot.inventory_type == "finished", InventoryLot.status == "active",
        FinishedGoodsInventoryDetail.product_id.in_(product_ids)).order_by(InventoryLot.id)).all()
    from app.services.shared_bom_stock import lot_product, role
    if any(role(db,p) for p in products):
        rows += [lot for lot in db.scalars(select(InventoryLot).where(
            InventoryLot.inventory_type=='semi_finished',InventoryLot.status=='active').order_by(InventoryLot.id))
            if (identity:=lot_product(db,lot)) and identity[0] in product_ids]
    result = []
    from app.services.warehouse_inventory import WarehouseInventoryError
    for lot in rows:
        fact = db.get(SharedFinishedLot, lot.id)
        reason = None
        if fact:
            if fact.group_id != group_id:
                reason = "此批次已经属于另一共用组"
        else:
            try:
                shared._preview_lots(db, products, [lot.id])
            except WarehouseInventoryError as error:
                reason = str(error)
        detail = lot.finished_detail
        result.append(dict(lot_id=lot.id, number=lot.lot_number,
            product_id=lot_product(db,lot)[0], available=lot.quantity_available,
            reserved=lot.quantity_reserved, version=lot.version,
            location=lot.location.location_name if lot.location else "",
            shared=bool(fact and fact.group_id == group_id), reason=reason,
            eligible=fact is None and reason is None))
    return result


def detail(db, group_id):
    ids = member_ids(db, group_id)
    group = db.get(SharedFinishedGroup, group_id)
    from app.services.warehouse_inventory import WarehouseInventoryError
    issue = None
    try:
        shared.preview(db, product_ids=ids, lot_ids=[], group_id=group_id)
    except WarehouseInventoryError as error:
        issue = str(error)
    return dict(group_id=group.id, version=group.version, enabled=group.enabled,
        auto_enroll=_policy(db, group.id), evidence=group.evidence, identity_issue=issue,
        products=[product_summary(db.get(Product, pid)) for pid in ids],
        lots=product_lots(db, ids, group_id=group.id))


def preview_change(db, *, group_id, action, expected_version, lot_ids=(),
                   enabled=None, auto_enroll=None):
    ids = member_ids(db, group_id)
    group = db.get(SharedFinishedGroup, group_id)
    if group.version != expected_version:
        raise shared._error("共用组已变化，请刷新后重新预览")
    if action == "add_lots":
        if enabled is not None or auto_enroll is not None:
            raise shared._error("追加批次不能同时改变共用设置", 422)
        if not lot_ids:
            raise shared._error("请选择要追加的成品批次")
        value = shared.preview(db, product_ids=ids, lot_ids=lot_ids, group_id=group_id)
    elif action == "configure":
        if lot_ids:
            raise shared._error("设置调整不能同时追加批次", 422)
        if type(enabled) is not bool or type(auto_enroll) is not bool:
            raise shared._error("请明确共用状态和新入库处理方式", 422)
        # Pausing must remain possible even when a member has drifted.
        value = shared.preview(db, product_ids=ids, lot_ids=[], group_id=group_id) if enabled else None
    else:
        raise shared._error("不支持的共用维护操作", 422)
    result = dict(group_id=group_id, action=action, version=group.version,
        current_enabled=group.enabled, current_auto_enroll=_policy(db, group_id),
        enabled=enabled, auto_enroll=auto_enroll, value=value)
    result["preview_hash"] = hashlib.sha256(shared._json(result).encode()).hexdigest()
    return result


def change(db, *, actor, source="web", group_id, action, expected_version,
           operation_key, preview_hash, evidence, lot_ids=(), enabled=None, auto_enroll=None):
    if source not in {"web", "script"} or (source == "web" and (
            actor is None or not actor.is_active or actor.role != "admin")):
        raise shared._error("仅活动管理员可维护成品共用", 403)
    if not evidence or not evidence.strip() or len(evidence) > 1000:
        raise shared._error("请填写本次实物核实或调整依据", 422)
    args = dict(group_id=group_id, action=action, expected_version=expected_version,
        lot_ids=sorted(set(lot_ids)), enabled=enabled, auto_enroll=auto_enroll)
    request = shared._json(dict(**args, preview_hash=preview_hash,
        evidence=evidence.strip(), actor_id=actor.id if actor else None, source=source))
    with atomic_bom(db):
        previous = db.scalar(select(SharedFinishedMutation).where(
            SharedFinishedMutation.operation_key == operation_key))
        if previous:
            if previous.request_json != request:
                raise shared._error("操作标识已用于另一项共用维护")
            return {**json.loads(previous.result_json), "replayed": True}
        value = preview_change(db, **args)
        if value["preview_hash"] != preview_hash:
            raise shared._error("产品或库存已变化，请重新核对共用预览")
        claimed = db.execute(update(SharedFinishedGroup).where(
            SharedFinishedGroup.id == group_id, SharedFinishedGroup.version == expected_version
        ).values(version=expected_version + 1, **({"enabled": enabled} if action == "configure" else {})))
        if claimed.rowcount != 1:
            raise shared._error("共用组已变化，请重新预览")
        if value["value"]:
            for product in value["value"]["products"]:
                claimed = db.execute(update(Product).where(Product.id == product["product_id"],
                    Product.version == product["version"]).values(version=Product.version, updated_at=Product.updated_at))
                if claimed.rowcount != 1:
                    raise shared._error("产品已变化，请重新预览")
        if action == "configure":
            policy = db.get(SharedFinishedPolicy, group_id)
            if policy is None:
                db.add(SharedFinishedPolicy(group_id=group_id, auto_enroll=auto_enroll))
            else:
                policy.auto_enroll = auto_enroll
        else:
            for row in value["value"]["lots"]:
                claimed = db.execute(update(InventoryLot).where(InventoryLot.id == row["lot_id"],
                    InventoryLot.version == row["version"], InventoryLot.quantity_reserved == 0,
                    InventoryLot.status == "active").values(version=InventoryLot.version + 1, updated_at=InventoryLot.updated_at))
                if claimed.rowcount != 1:
                    raise shared._error("库存已变化，请重新预览")
                db.add(SharedFinishedLot(group_id=group_id, lot_id=row["lot_id"], identity_json=row["identity_json"]))
        result = dict(group_id=group_id, version=expected_version + 1, replayed=False)
        db.add(SharedFinishedMutation(group_id=group_id, operation_key=operation_key,
            request_json=request, result_json=shared._json(result)))
        append_audit_event(db, event_category="business", result="success", source=source,
            module_code="warehouse", action_code="manage_shared_finished_stock", resource="inventory_lot",
            actor=actor, entity_type="shared_finished_group", entity_id=group_id,
            operator_name="老板明确授权的发布维护任务" if source == "script" else None,
            details=dict(**args, preview_hash=preview_hash, evidence=evidence.strip()))
        db.flush()
        return result


def enroll_new_lot(db, lot, *, operator_id, production_verified=False):
    """Only a newly created, qualified receipt in its owning transaction.

    Existing lots are never swept in by toggling the policy. Order/BOM/external
    receipts need their own frozen evidence; transfer uses the existing lineage.
    """
    from app.services.shared_bom_stock import lot_product
    identity=lot_product(db,lot)
    if db.get(SharedFinishedLot, lot.id) or not identity:
        return False
    if not production_verified and not (
            lot.source_type in {"manual", "stocktake"} and lot.source_ref_type is None):
        return False
    member = db.get(SharedFinishedMember, identity[0])
    if not member or not _policy(db, member.group_id):
        return False
    group = db.get(SharedFinishedGroup, member.group_id)
    if not group.enabled:
        return False
    from app.services.warehouse_inventory import WarehouseInventoryError
    try:
        ids = member_ids(db, group.id)
        preview = shared.preview(db, product_ids=ids, lot_ids=[], group_id=group.id)
        rows = shared._preview_lots(db, [db.get(Product, pid) for pid in ids], [lot.id], automatic=True)
    except WarehouseInventoryError:
        # Ordinary inbound still succeeds. The management page explains why a
        # batch awaits manual verification; never weaken its matching contract.
        return False
    claimed = db.execute(update(SharedFinishedGroup).where(
        SharedFinishedGroup.id == group.id, SharedFinishedGroup.version == group.version,
        SharedFinishedGroup.enabled.is_(True)).values(version=SharedFinishedGroup.version))
    if claimed.rowcount != 1:
        raise shared._error("共用设置在入库时变化，请重试入库")
    for product in preview["products"]:
        claimed = db.execute(update(Product).where(Product.id == product["product_id"],
            Product.version == product["version"]).values(version=Product.version, updated_at=Product.updated_at))
        if claimed.rowcount != 1:
            raise shared._error("共用产品资料在入库时变化，请重试入库")
    db.add(SharedFinishedLot(group_id=group.id, lot_id=lot.id, identity_json=rows[0]["identity_json"]))
    append_audit_event(db, event_category="business", result="success", source="system",
        module_code="warehouse", action_code="auto_share_finished_receipt", resource="inventory_lot",
        entity_type="inventory_lot", entity_id=lot.id, actor=None,
        details=dict(group_id=group.id, product_id=member.product_id, operator_id=operator_id,
            policy_version=group.version, identity_sha256=hashlib.sha256(rows[0]["identity_json"].encode()).hexdigest()))
    db.flush()
    return True


def enroll_production_receipt(db, lot, *, completion, item, operator_id):
    """Check actual frozen order/task facts after its own reservation is made."""
    from app.models.production import ProductionTask
    from app.services.shared_finished_receipts import order_identity
    from app.services.production_workflow import _new_task_printing_snapshot, ProductionWorkflowError
    product = db.get(Product, item.product_id)
    member = db.get(SharedFinishedMember, item.product_id)
    if not member or not _policy(db, member.group_id) or not shared._ordinary(db, product):
        return False
    task = db.get(ProductionTask, completion.task_id)
    if (task is None or task.sales_order_item_bom_component_id is not None
            or lot.source_ref_type != "production_completion" or lot.source_ref_id != completion.id
            or item.id != completion.order_item_id or task.order_item_id != item.id
            or lot.finished_detail.product_id != item.product_id):
        return False
    frozen = db.get(SharedFinishedOrderBasis, item.id)
    if (frozen is None or frozen.group_id != member.group_id or frozen.member_identity_json != member.identity_json
            or frozen.order_identity_json != order_identity(item)):
        return False
    expected = frozen.product_basis_json
    if expected != shared.product_basis(product) or expected != lot.finished_detail.physical_basis_json:
        return False
    for field in ("report_length_mm", "report_width_mm", "base_report_length_mm", "base_report_width_mm"):
        if shared._value(getattr(item, "snapshot_" + field)) != shared._value(getattr(product, field)):
            return False
    if item.sheet_cutting_settings_snapshot != product.sheet_cutting_settings:
        return False
    try:
        printing = _new_task_printing_snapshot(db, product)
    except ProductionWorkflowError:
        return False
    if any(shared._value(getattr(task, key)) != shared._value(value) for key, value in printing.items()):
        return False
    return enroll_new_lot(db, lot, operator_id=operator_id, production_verified=True)
