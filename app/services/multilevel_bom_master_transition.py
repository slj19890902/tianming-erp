"""Explicit, admin-only master transition; never converts orders or stock.

Caller supplies reviewed product versions and commits the outer transaction.
Replaying stale versions fails closed, rather than repeating side effects.
"""
from sqlalchemy import select, update

from app.models.bom_subkit import ProductSubkit
from app.models.product import Product
from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
from app.models.user import User
from app.services.audit_log import append_audit_event
from app.services.bom_subkits import recipe_rows, save_subkit
from app.services.bom_transactions import atomic_bom
from app.services.composite_bom import CompositeBOMError, replace_product_bom


def _positive(value):
    return type(value) is int and value > 0


def transition_master_boms(db, *, customer_id, changes, expected_versions,
                           disable_subkits, actor):
    """Apply ordered child-first BOM changes and retire specified old sidecars.

All existing/new reachable products must be in expected_versions. Disabling a
sidecar requires both its parent and real kit in the changes, so no anonymous
renamed product or half-converted recipe can be introduced by this operation.
No API/CLI exposure: formal execution still requires a reviewed release plan.
"""
    if (not _positive(customer_id) or not isinstance(changes, list)
            or not 1 <= len(changes) <= 30
            or not isinstance(expected_versions, dict) or not 1 <= len(expected_versions) <= 1000
            or not all(_positive(k) and _positive(v) for k, v in expected_versions.items())
            or not isinstance(disable_subkits, dict)
            or not all(_positive(k) and _positive(v) for k, v in disable_subkits.items())):
        raise CompositeBOMError("转换范围或版本无效")
    ids = [row.get("parent_product_id") for row in changes if isinstance(row, dict)]
    if (len(ids) != len(changes) or not all(_positive(pid) for pid in ids)
            or len(set(ids)) != len(ids)
            or any(not isinstance(row.get("components"), list)
                   or row.get("inventory_mode") not in {"manufactured", "assembled", "purchased"}
                   for row in changes)):
        raise CompositeBOMError("转换产品不能重复，且必须提供子件列表")
    with atomic_bom(db):
        current_actor = db.get(User, getattr(actor, "id", None), populate_existing=True)
        if current_actor is None or not current_actor.is_active or current_actor.role != "admin":
            raise CompositeBOMError("仅活动管理员可执行主档转换", 403)
        products = {p.id: p for p in db.scalars(select(Product).where(
            Product.id.in_(expected_versions)).execution_options(populate_existing=True))}
        for pid, version in expected_versions.items():
            product = products.get(pid)
            if (product is None or product.customer_id != customer_id or not product.is_active
                    or product.deleted_at is not None or product.purged_at is not None):
                raise CompositeBOMError("转换范围包含失效产品或其他客户", 409)
            if product.version != version:
                raise CompositeBOMError("转换产品版本已变化，请重新核对", 409)
        # Lock every reviewed identity, including unchanged leaves. A child edit
        # must not race between validation and the final parent recipe write.
        for pid in sorted(products):
            locked = db.execute(update(Product).where(
                Product.id == pid, Product.version == expected_versions[pid],
                Product.customer_id == customer_id, Product.is_active.is_(True),
                Product.deleted_at.is_(None), Product.purged_at.is_(None),
            ).values(version=Product.version, updated_at=Product.updated_at))
            if locked.rowcount != 1:
                raise CompositeBOMError("转换产品版本已变化，请重新核对", 409)

        required = set(ids)
        for change in changes:
            for component in change["components"]:
                if not isinstance(component, dict) or not _positive(component.get("component_product_id")):
                    raise CompositeBOMError("子件必须使用正式产品ID")
                required.add(component["component_product_id"])
        groups = {}
        for pid, version in disable_subkits.items():
            group = db.get(ProductSubkit, pid, populate_existing=True)
            if group is None or not group.enabled or group.version != version:
                raise CompositeBOMError("旧组套版本或状态已变化", 409)
            if pid not in ids or group.kit_product_id not in ids:
                raise CompositeBOMError("停用旧组套必须同时转换父件和真实套件")
            parent_change = next(row for row in changes if row["parent_product_id"] == pid)
            if group.kit_product_id not in {row["component_product_id"] for row in parent_change["components"]}:
                raise CompositeBOMError("新父件必须关联原真实套件产品")
            required.add(group.kit_product_id)
            required.update(row["product_id"] for row in recipe_rows(group))
            groups[pid] = group
        # Cover old descendants too: no omitted leaf can change unnoticed.
        visited = set()
        while required - visited:
            batch = required - visited
            if not batch <= products.keys():
                raise CompositeBOMError("缺少关联产品的核对版本", 409)
            visited.update(batch)
            required.update(db.scalars(select(ProductBomComponent.component_product_id).where(
                ProductBomComponent.parent_product_id.in_(batch))))
        # ON DELETE SET NULL is valid for ordinary master edits, but a controlled
        # transition must not silently alter even a historical snapshot FK.
        for change in changes:
            children = {row["component_product_id"] for row in change["components"]}
            removed = select(ProductBomComponent.id).where(
                ProductBomComponent.parent_product_id == change["parent_product_id"],
                ProductBomComponent.component_product_id.not_in(children))
            if db.scalar(select(SalesOrderItemBomComponent.id).where(
                    SalesOrderItemBomComponent.product_bom_component_id.in_(removed)).limit(1)):
                raise CompositeBOMError("待移除关系仍被历史订单引用，须先制定独立转换方案", 409)

        for pid, group in groups.items():
            kit = products[group.kit_product_id]
            save_subkit(db, parent_product_id=pid, name=kit.product_name,
                kits_per_parent=group.kits_per_parent, members=recipe_rows(group),
                expected_version=disable_subkits[pid], actor=current_actor, enabled=False)
        results = []
        for change in changes:
            pid = change["parent_product_id"]
            results.append(replace_product_bom(db, parent_product_id=pid,
                components=change["components"], inventory_mode=change.get("inventory_mode"),
                expected_version=products[pid].version, user=current_actor,
                change_reason="已核对的多级BOM主档转换；不改订单库存"))
        versions = {pid: products[pid].version for pid in sorted(products)}
        append_audit_event(db, event_category="business", result="success", source="web",
            module_code="products", action_code="transition_master_boms", resource="product_bom",
            actor=current_actor, customer_id=customer_id,
            details={"parent_ids": ids, "disabled_subkits": list(disable_subkits),
                     "order_inventory_conversion": False})
        return {"products": results, "versions": versions,
                "order_inventory_conversion": False}
