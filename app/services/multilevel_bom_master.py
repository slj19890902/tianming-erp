"""Read real product/BOM identities; inventory semantics live on those edges."""
from sqlalchemy import select

from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomProfile, ProductBomInventoryRelation
from app.services.multilevel_bom_plan import BomPlanError


def load_master_structure(db, root_product_id):
    root = db.get(Product, root_product_id)
    if root is None:
        raise BomPlanError("父产品不存在")
    root_profile = db.get(ProductBomProfile, root_product_id)
    products, profiles, edges = {}, {}, []
    pending = {root_product_id}
    while pending:
        batch = sorted(pending - set(products))
        if not batch:
            break
        if len(products) + len(batch) > 1000:
            raise BomPlanError("BOM产品数量超过1000，请拆分维护")
        found = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(batch)))}
        for pid in batch:
            product = found.get(pid)
            if (product is None or product.customer_id != root.customer_id or not product.is_active
                    or product.deleted_at is not None or product.purged_at is not None):
                raise BomPlanError("BOM包含失效产品或跨客户关系")
        products.update(found)
        profiles.update({p.product_id: p.source for p in db.scalars(
            select(ProductBomProfile).where(ProductBomProfile.product_id.in_(batch)))})
        rows = list(db.scalars(select(ProductBomComponent).where(
            ProductBomComponent.parent_product_id.in_(batch)).order_by(ProductBomComponent.display_order)))
        meanings = {r.bom_component_id: r.relation for r in db.scalars(select(ProductBomInventoryRelation).where(
            ProductBomInventoryRelation.bom_component_id.in_([row.id for row in rows])))} if rows else {}
        pending = set()
        for row in rows:
            if row.parent_product_id not in profiles or meanings.get(row.id) not in {"assembly", "accompany"}:
                raise BomPlanError("多级BOM仍有未配置的旧旁路关系，请设置组装或配套")
            if not row.is_required:
                raise BomPlanError("多级BOM子件必须为必需项")
            source = profiles[row.parent_product_id]
            if meanings[row.id] == "assembly" and source not in {"assembled", "manufactured", "purchased"}:
                raise BomPlanError("组装子件的父产品必须为自制本体、外购本体或零件组装来源")
            if row.quantity_per_set <= 0 or row.quantity_per_set != int(row.quantity_per_set):
                raise BomPlanError("每父件子件用量必须为正整数")
            edges.append({"bom_component_id": row.id, "parent_id": row.parent_product_id,
                "child_id": row.component_product_id, "quantity": int(row.quantity_per_set),
                "relation": meanings[row.id]})
            pending.add(row.component_product_id)
        if len(edges) > 5000:
            raise BomPlanError("BOM关系数量超过5000")
    indegree = {pid: 0 for pid in products}
    children = {pid: [] for pid in products}
    for edge in edges:
        if edge["relation"] == "assembly" and profiles.get(edge["child_id"]) == "separate":
            raise BomPlanError("无实体库存的组合需求不能作为组装消耗子件")
        indegree[edge["child_id"]] += 1
        children[edge["parent_id"]].append(edge)
    ready = sorted(pid for pid, count in indegree.items() if not count)
    order = []
    while ready:
        pid = ready.pop(0)
        order.append(pid)
        if profiles.get(pid) == "assembled" and not any(e["relation"] == "assembly" for e in children[pid]):
            raise BomPlanError("组套成品缺少组装子件")
        if profiles.get(pid) == "separate" and not children[pid]:
            raise BomPlanError("子件分存组合必须有真实配套子件")
        for edge in children[pid]:
            indegree[edge["child_id"]] -= 1
            if not indegree[edge["child_id"]]:
                ready.append(edge["child_id"])
                ready.sort()
    if len(order) != len(products):
        raise BomPlanError("BOM关系会形成循环引用")
    return {"root_product_id": root.id, "customer_id": root.customer_id,
        "material_mode": root_profile.material_mode if root_profile else None,
        "delivery_mode": root_profile.delivery_mode if root_profile else None,
        "products": products, "profiles": profiles, "edges": edges, "order": order}


def preview_master_structure(db, root_product_id):
    structure = load_master_structure(db, root_product_id)
    return {"root_product_id": root_product_id, "customer_id": structure["customer_id"],
        "nodes": [{"product_id": pid, "version": structure["products"][pid].version,
            "name": structure["products"][pid].product_name,
            "code": structure["products"][pid].product_code,
            "unit": structure["products"][pid].unit,
            "inventory_mode": structure["profiles"].get(pid, "leaf")}
            for pid in structure["order"]], "edges": structure["edges"]}
