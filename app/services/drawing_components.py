"""Read-only BOM projection and frozen component placement validation for drawings.

This module does not change BOM quantities, stock, molds or any child drawing.
Callers must authorize the root product before requesting this projection.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select

from app.models.drawing_design import DrawingDesign, DrawingRelease
from app.models.multilevel_bom import ProductBomInventoryRelation, ProductBomProfile
from app.models.product import Product
from app.models.product_bom import ProductBomComponent


MAX_PRODUCTS = 128
MAX_PATHS = 256
MAX_INSTANCES = 500


def _fail(message: str, status: int = 422):
    raise HTTPException(status, message)


def _manifest(release: DrawingRelease) -> dict:
    try:
        value = json.loads(release.manifest_json)
    except (TypeError, ValueError):
        _fail("子件发布图纸记录损坏，请核对原发布版")
    if not isinstance(value, dict) or not isinstance(value.get("geometry"), dict):
        _fail("子件发布图纸缺少冻结结构")
    return value


def _release_view(release: DrawingRelease) -> dict:
    manifest = _manifest(release)
    geometry = manifest["geometry"]
    metadata = (manifest.get("parameters") or {}).get("__drawing_workbench_v1", {})
    editor = manifest.get("editor_state") or metadata.get("editor_state") or {}
    folding = manifest.get("fold_model", geometry.get("fold_model"))
    if folding is None and release.template_key != "assembly_v1":
        from app.services.drawing_workbench import fold_model
        folding = fold_model(release.template_key, geometry)
    result = {
        "id": release.id,
        "revision": release.revision,
        "number": release.external_number,
        "template_key": release.template_key,
        "geometry": geometry,
        "fold_model": folding,
        "assembly": editor.get("assembly"),
    }
    return result


def component_context(db, product: Product) -> dict:
    """Project actual product/edge identities, including separate repeated paths."""
    found = {product.id: product}
    edges: list[dict] = []
    pending = [product.id]
    profiles = {}
    while pending:
        ids = list(pending)
        pending = []
        profiles.update({p.product_id: p.source for p in db.scalars(
            select(ProductBomProfile).where(ProductBomProfile.product_id.in_(ids)))})
        rows = list(db.scalars(select(ProductBomComponent).where(
            ProductBomComponent.parent_product_id.in_(ids)).order_by(
                ProductBomComponent.parent_product_id, ProductBomComponent.display_order,
                ProductBomComponent.id)))
        relations = {r.bom_component_id: r.relation for r in db.scalars(
            select(ProductBomInventoryRelation).where(
                ProductBomInventoryRelation.bom_component_id.in_([row.id for row in rows])))} if rows else {}
        for row in rows:
            qty = Decimal(str(row.quantity_per_set))
            if not qty.is_finite() or qty <= 0 or qty != int(qty):
                _fail("组合图纸包含无效子件用量，请先核对 BOM")
            child = found.get(row.component_product_id)
            if child is None:
                child = db.get(Product, row.component_product_id)
                if child is None or child.customer_id != product.customer_id:
                    _fail("组合图纸包含无效或跨客户子件")
                if not child.is_active or child.deleted_at is not None or child.purged_at is not None:
                    _fail("组合图纸包含已停用或删除的子件")
                found[child.id] = child
                pending.append(child.id)
            if child.customer_id != product.customer_id:
                _fail("组合图纸包含跨客户子件")
            edges.append({"edge_id": row.id, "parent_product_id": row.parent_product_id,
                          "child_product_id": child.id, "quantity_per_set": int(qty),
                          "mold_tool_id": row.mold_tool_id,
                          "relation": relations.get(row.id, "unconfigured")})
        if len(found) > MAX_PRODUCTS or len(edges) > MAX_PATHS:
            _fail("组合图纸子件过多，请按子装配分开维护")

    children = {pid: [] for pid in found}
    for edge in edges:
        children[edge["parent_product_id"]].append(edge)
    instances: list[dict] = []

    def walk(pid: int, path: str, count: int, ancestry: frozenset[int]):
        if pid in ancestry:
            _fail("组合图纸的 BOM 存在循环引用")
        if len(ancestry) >= 24:
            _fail("组合图纸层级过深，请分开维护")
        for edge in children[pid]:
            child_path = f"{path}/{edge['edge_id']}".strip("/")
            child_count = count * edge["quantity_per_set"]
            if child_count > MAX_INSTANCES or len(instances) >= MAX_PATHS:
                _fail("组合预览超过 500 件，请按子装配分开维护")
            instances.append({"path": child_path, "parent_path": path,
                              "product_id": edge["child_product_id"],
                              "instance_count": child_count, "depth": len(ancestry) + 1,
                              "edge_id": edge["edge_id"]})
            walk(edge["child_product_id"], child_path, child_count, ancestry | {pid})

    walk(product.id, "", 1, frozenset())
    if sum(i["instance_count"] for i in instances) > MAX_INSTANCES:
        _fail("组合预览超过 500 件，请按子装配分开维护")
    designs = {d.product_id: d for d in db.scalars(select(DrawingDesign).where(
        DrawingDesign.product_id.in_(found)))}
    releases = {}
    for release in db.scalars(select(DrawingRelease).where(
            DrawingRelease.product_id.in_(found), DrawingRelease.customer_id == product.customer_id)
            .order_by(DrawingRelease.id.desc())):
        releases.setdefault(release.product_id, release)
    nodes, warnings = [], []
    for pid, item in found.items():
        design, release = designs.get(pid), releases.get(pid)
        has_body = item.box_style != "BOM组合" and profiles.get(pid) != "assembled"
        node = {"product_id": pid, "code": item.customer_material_code or item.product_code,
                "name": item.product_name, "unit": item.unit, "box_style": item.box_style,
                "product_version": item.version, "mold_tool_id": item.mold_tool_id,
                "design_version": design.version if design else None,
                "template_key": design.template_key if design else None,
                "has_body": has_body, "inventory_mode": profiles.get(pid),
                "latest_release": _release_view(release) if release else None}
        nodes.append(node)
        if pid != product.id and has_body and not release:
            warnings.append({"product_id": pid, "message": "子件尚未发布图纸"})
    basis = {"root_product_id": product.id,
             "products": [{"id": n["product_id"], "version": n["product_version"],
                           "has_body": n["has_body"]} for n in nodes], "edges": edges}
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":")).encode()).hexdigest()
    return {"root_product_id": product.id, "basis_hash": digest, "nodes": nodes,
            "edges": edges, "instances": instances, "warnings": warnings}


def _vector(values, label: str, limit: Decimal) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        _fail(f"{label}需要三个数值")
    result = []
    for value in values:
        try:
            if isinstance(value, bool):
                raise ValueError
            number = Decimal(str(value))
        except (ValueError, TypeError, InvalidOperation):
            _fail(f"{label}包含无效数值")
        if not number.is_finite() or abs(number) > limit or number.as_tuple().exponent < -4:
            _fail(f"{label}超出范围或精度")
        result.append(format(number, "f"))
    return result


def validate_component_placements(db, product: Product, placements: list,
                                  expected_basis_hash: str | None = None,
                                  for_publish: bool = False) -> list[dict]:
    """Bind geometry to immutable child releases; never infer placement or counts."""
    context = component_context(db, product)
    if expected_basis_hash is not None and expected_basis_hash != context["basis_hash"]:
        _fail("BOM 或子件资料已变化，请重新核对组合图纸", 409)
    if not isinstance(placements, list) or len(placements) > MAX_INSTANCES:
        _fail("组合图纸最多摆放 500 件")
    paths = {i["path"]: i for i in context["instances"]}
    nodes = {n["product_id"]: n for n in context["nodes"]}
    normalized = []
    selected = {}
    for placement in placements:
        if not isinstance(placement, dict):
            _fail("子件摆放资料格式不正确")
        path, index = placement.get("path"), placement.get("instance_index")
        entry = paths.get(path) if isinstance(path, str) else None
        if entry is None or type(index) is not int or not 0 <= index < entry["instance_count"]:
            _fail("摆放子件不属于当前 BOM 或超过实际用量")
        key = (path, index)
        if key in selected:
            _fail("同一子件实例不能重复摆放")
        release_id = placement.get("child_release_id")
        if type(release_id) is not int:
            _fail("请先选择子件已发布图纸")
        release = db.get(DrawingRelease, release_id)
        if (release is None or release.product_id != entry["product_id"]
                or release.customer_id != product.customer_id):
            _fail("子件图纸不属于当前产品或客户")
        frozen = _release_view(release)
        if release.template_key != "assembly_v1" and not frozen["geometry"].get("cut"):
            _fail("子件发布图纸缺少有效刀线")
        if release.template_key == "assembly_v1":
            assembly = frozen.get("assembly")
            if not isinstance(assembly, dict) or not assembly.get("placements"):
                _fail("子装配发布版缺少冻结摆放资料")
            child = db.get(Product, release.product_id)
            if for_publish and assembly.get("basis_hash") != component_context(db, child)["basis_hash"]:
                _fail("子装配发布版与当前 BOM 不一致，请先核对并发布子装配新版", 409)
        row = {"path": path, "instance_index": index, "product_id": release.product_id,
               "child_release_id": release.id, "child_revision": release.revision,
               "position_mm": _vector(placement.get("position_mm"), "摆放位置", Decimal(100000)),
               "rotation_deg": _vector(placement.get("rotation_deg"), "摆放角度", Decimal(360)),
               "geometry": frozen["geometry"], "fold_model": frozen["fold_model"],
               "assembly": frozen["assembly"], "template_key": release.template_key}
        selected[key] = row
        normalized.append(row)

    def ancestor_covers(path: str, index: int):
        entry = paths[path]
        parent = entry["parent_path"]
        while parent:
            parent_entry = paths[parent]
            ratio = entry["instance_count"] // parent_entry["instance_count"]
            match = selected.get((parent, index // ratio))
            if match and match["template_key"] == "assembly_v1":
                return True
            parent = parent_entry["parent_path"]
        return False

    for path, index in selected:
        if ancestor_covers(path, index):
            _fail("已摆放整套子装配，不能再次重复摆放其中零件")
    if for_publish:
        if not paths:
            _fail("组合图纸缺少真实 BOM 子件")
        covered_count = 0
        for entry in paths.values():
            if not nodes[entry["product_id"]]["has_body"]:
                continue
            for index in range(entry["instance_count"]):
                if (entry["path"], index) not in selected and not ancestor_covers(entry["path"], index):
                    _fail("组合图纸尚有子件未摆放或未发布图纸")
                covered_count += 1
        if not covered_count:
            _fail("组合图纸缺少有本体的真实子件")
    return normalized
