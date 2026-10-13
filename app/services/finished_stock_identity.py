"""Physical stock matching uses captured facts, never a mutable product name."""
import json
import hashlib
from decimal import Decimal

from app.services.product_specification import product_dimension_specification, normalized_specification_text

FIELDS = ("box_category", "box_style", "production_process", "production_notes", "layer_count",
          "splice_mode", "pieces_per_box", "crease_type", "crease_left_mm",
          "crease_middle_mm", "crease_right_mm", "base_crease_type",
          "base_crease_left_mm", "base_crease_middle_mm", "base_crease_right_mm", "flap_mm")


def _value(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return format(Decimal(str(value)).normalize(), "f")
    return str(value).strip() or None


def _document(product_id, unit, spec, material, flute, values):
    return json.dumps(dict(schema=1, product_id=product_id, unit=unit,
        spec=normalized_specification_text(spec), material=_value(material), flute=_value(flute), assembly=[],
        **{key: _value(value) for key, value in values.items()}),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _product_basis(product, source=None):
    material = product.material
    values = {key: getattr(product, key) for key in FIELDS}
    values["layer_count"] = material.layer_count if material else product.layer_count
    source = source or ("purchased" if product.supply_mode == "external_purchase" else "manufactured")
    from app.services.sheet_cutting_settings import component_settings
    setting = component_settings(product.sheet_cutting_settings) if source == "manufactured" else None
    die_cut = source == "manufactured" and (setting.is_die_cut if setting else product.box_category == "die_cut")
    values.update(mold_tool_id=product.mold_tool_id if die_cut else None,
                  die_cut_path=product.die_cut_path if die_cut else None)
    return _document(product.id, product.unit, product_dimension_specification(product),
        material.code if material else product.legacy_material_text,
        product.flute_type or (material.flute_type if material else None), values)


def _with_assembly(document, children):
    value = json.loads(document)
    value["assembly"] = sorted(children, key=lambda child: child["product_id"])
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _child_basis(pid, quantity, document):
    return dict(product_id=pid, quantity=quantity,
                basis_hash=hashlib.sha256(document.encode()).hexdigest())


def product_basis(product):
    from sqlalchemy.orm import object_session
    from app.models.multilevel_bom import ProductBomProfile
    from app.services.multilevel_bom_master import load_master_structure
    db = object_session(product)
    if db is None or db.get(ProductBomProfile, product.id) is None:
        return _product_basis(product)
    structure = load_master_structure(db, product.id)
    documents = {}
    for pid in reversed(structure["order"]):
        documents[pid] = _with_assembly(_product_basis(structure["products"][pid], structure["profiles"].get(pid)), [
            _child_basis(e["child_id"], e["quantity"], documents[e["child_id"]])
            for e in structure["edges"] if e["parent_id"] == pid and e["relation"] == "assembly"])
    return documents[product.id]


def snapshot_basis(snapshot, unit):
    return _document(snapshot.component_product_id, unit, snapshot.snapshot_component_spec,
        snapshot.snapshot_component_material, snapshot.snapshot_component_flute_type,
        {**{key: getattr(snapshot, "snapshot_component_" + key) for key in FIELDS},
         "mold_tool_id": snapshot.snapshot_mold_tool_id, "die_cut_path": snapshot.snapshot_die_cut_path})


def matches_stock_identity(actual, expected):
    """Quantity ledger metadata is validated separately from the frozen SKU identity."""
    if expected is None:
        return True
    try:
        value = json.loads(actual or '{}')
        value.pop('quantity_basis', None)
        return value == json.loads(expected)
    except (ValueError, TypeError, AttributeError):
        return False


def order_product_basis(db, order_item_id, product_id):
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    compiled = read_compiled_order_bom(db, order_item_id)
    if compiled is None:
        from app.models.order import OrderItem
        item = db.get(OrderItem, order_item_id)
        if item is None or item.product_id != product_id:
            return None
        basis = item.stock_identity_snapshot_json
        if basis is not None:
            value = json.loads(basis)
            if not isinstance(value, dict) or value.get("schema") != 1 or value.get("product_id") != product_id or not value.get("unit"):
                raise ValueError("订单实物身份快照无效，请核对原下单记录")
        return basis
    return compiled_product_bases(compiled)[product_id]


def compiled_product_bases(compiled):
    """Physical identities for an already validated frozen or proposed graph.

    Also used before a rule revision exists in the database. No current master
    lookup can change the identity of either side of a version comparison.
    """
    from app.services.multilevel_bom_orders import validate_compiled_order_rows
    validate_compiled_order_rows(compiled.graph, compiled.snapshots)
    nodes, children, order = compiled.graph.validated()
    snapshots = {s.component_product_id: s for s in compiled.snapshots}
    documents = {}
    for pid in reversed(order):
        documents[pid] = _with_assembly(snapshot_basis(snapshots[pid], nodes[pid].unit), [
            _child_basis(e.child_id, e.quantity, documents[e.child_id])
            for e in children[pid] if e.relation == "assembly"])
    return documents


def matching_component_basis(db, snapshot, lot):
    from app.services.bom_inventory_contract import is_body_lot
    if is_body_lot(lot):
        return False
    expected = order_product_basis(db, snapshot.sales_order_item_id, snapshot.component_product_id)
    # Legacy orders retain their existing matching contract. New graph orders
    # require evidence captured on inbound; an unknown old lot is not a match.
    if expected is None:
        return True
    return matches_component_stock_identity(lot, expected)


def matches_lot_stock_identity(lot, expected):
    if matches_stock_identity(lot.finished_detail.physical_basis_json, expected):
        return True
    from app.services.frozen_assembly_mold_proof import matches_proven_assembly
    if matches_proven_assembly(lot, expected):
        return True
    from app.services.legacy_bom_mold_confirmation import confirmed_match
    return confirmed_match(lot, expected)


def matches_component_stock_identity(lot, expected):
    """Component reservations are physical 1:1 counts, with no ratio conversion."""
    actual = lot.finished_detail.physical_basis_json
    if not matches_lot_stock_identity(lot, expected):
        return False
    if expected is None:
        return True
    try:
        value, wanted = json.loads(actual), json.loads(expected)
        if "quantity_basis" not in value:
            return True
        from app.services.delivery_quantities import require_physical_stock
        require_physical_stock(lot, dict(customer_basis=1, physical_basis=1,
            customer_id=lot.finished_detail.owner_customer_id,
            product_id=wanted['product_id'], physical_unit=wanted['unit']), require_marker=True)
        return True
    except (ValueError, TypeError, AttributeError, KeyError):
        return False


def stock_identity_error(lot, expected):
    """Describe captured differences without substituting current master facts."""
    detail = lot.finished_detail
    reference = f"批次 {lot.lot_number}（存货编码 {detail.inventory_code_snapshot or detail.product_id}）"
    actual = detail.physical_basis_json
    if not actual:
        return f"{reference}未冻结规格工艺依据；请在批次实物身份确认中核对，不要重复盘点入库"
    try:
        value, wanted = json.loads(actual), json.loads(expected)
        if not isinstance(value, dict) or not isinstance(wanted, dict):
            raise ValueError("invalid identity document")
    except (ValueError, TypeError):
        return f"{reference}冻结依据格式无效；请管理员核对原入库记录，当前批次不能抵扣"
    labels = dict(product_id="产品身份", unit="单位", spec="规格", material="材质", flute="楞型",
        assembly="组装关系", box_category="箱型分类", box_style="箱式", production_process="工艺",
        production_notes="生产备注", layer_count="层数", splice_mode="接片方式", pieces_per_box="每箱片数",
        mold_tool_id="模具", die_cut_path="模切路径", crease_type="压线类型", flap_mm="封口尺寸")
    labels.update(base_crease_type="底片压线类型", crease_left_mm="左压线", crease_middle_mm="中压线",
        crease_right_mm="右压线", base_crease_left_mm="底片左压线", base_crease_middle_mm="底片中压线",
        base_crease_right_mm="底片右压线", schema="冻结版本")
    has_quantity_marker = "quantity_basis" in value
    value.pop("quantity_basis", None)
    fields = [key for key in sorted(set(value) | set(wanted)) if value.get(key) != wanted.get(key)]
    def text(val):
        if val is None or val == "":
            return "未填写"
        if isinstance(val, (list, dict)):
            return "已冻结关系" if val else "无关系"
        return str(val)[:80]
    differences = "；".join(f"{labels.get(key, key)}：库存 {text(value.get(key))} / 订单 {text(wanted.get(key))}"
        for key in fields[:8])
    reason = differences or ("实物数量单位资料未核实，组件库存必须按相同实物单位计数" if has_quantity_marker else "冻结字段结构不同")
    return f"{reference}已有冻结依据，但与订单不匹配（{reason}）；请核对原批次与订单规格工艺，勿重复入库或覆盖旧依据"


def identity_preview(db, lot_id):
    from app.models.warehouse_inventory import InventoryLot
    from app.models.product import Product
    from app.services.warehouse_inventory import WarehouseInventoryError
    lot = db.get(InventoryLot, lot_id)
    if lot is not None and lot.inventory_type == "semi_finished":
        from app.services.completed_stock_identity import preview as completed_preview
        return completed_preview(db, lot)
    if lot is None or lot.finished_detail is None or lot.inventory_type != "finished":
        raise WarehouseInventoryError("成品库存批次不存在", 404)
    if lot.finished_detail.physical_basis_json is not None:
        raise WarehouseInventoryError("该批次已有冻结规格工艺依据，不能重新覆盖", 409)
    if lot.status != "active" or lot.quantity_reserved or lot.quantity_consumed:
        raise WarehouseInventoryError("仅未预占、未消耗的正常批次可补充实物身份确认", 409)
    product = db.get(Product, lot.finished_detail.product_id)
    if product is None or product.deleted_at is not None or not product.is_active:
        raise WarehouseInventoryError("批次产品无效，请先核实产品身份", 409)
    basis = product_basis(product)
    result = dict(lot_id=lot.id, lot_number=lot.lot_number, lot_version=lot.version,
        product_id=product.id, product_version=product.version,
        location_id=lot.warehouse_location_id, quantity_available=lot.quantity_available,
        physical_basis=json.loads(basis))
    result["preview_hash"] = hashlib.sha256(json.dumps(result, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return result


def confirm_identity(db, *, lot_id, preview_hash, operation_key, actor):
    from sqlalchemy import select, update
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement
    from app.services.warehouse_inventory import WarehouseInventoryError, _movement, _balances
    from app.services.bom_transactions import atomic_bom
    from app.services.audit_log import append_audit_event
    from app.models.product import Product
    if actor is None or not actor.is_active or actor.role != "admin":
        raise WarehouseInventoryError("仅活动管理员可确认实物身份", 403)
    key = "physical-identity:" + operation_key
    request = dict(lot_id=lot_id, preview_hash=preview_hash, actor_id=actor.id)
    with atomic_bom(db):
        previous = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == key))
        if previous is not None:
            if (previous.inventory_lot_id != lot_id or previous.reason != "确认批次规格工艺"
                    or json.loads(previous.remarks or "{}") != request):
                raise WarehouseInventoryError("该操作标识已用于其他批次身份确认", 409)
            return {"lot_id": lot_id, "movement_id": previous.id, "replayed": True}
        preview = identity_preview(db, lot_id)
        if preview["preview_hash"] != preview_hash:
            raise WarehouseInventoryError("批次或产品已变化，请重新核对实物依据", 409)
        locked = db.execute(update(Product).where(Product.id == preview["product_id"],
            Product.version == preview["product_version"]).values(version=Product.version,
            updated_at=Product.updated_at))
        if locked.rowcount != 1:
            raise WarehouseInventoryError("产品已变化，请重新预览", 409)
        lot = db.get(InventoryLot, lot_id)
        before = _balances(lot)
        result = db.execute(update(InventoryLot).where(InventoryLot.id == lot_id,
            InventoryLot.version == preview["lot_version"], InventoryLot.quantity_reserved == 0,
            InventoryLot.quantity_consumed == 0).values(version=InventoryLot.version + 1))
        if result.rowcount != 1:
            raise WarehouseInventoryError("批次已变化，请重新预览", 409)
        if lot.inventory_type == "semi_finished":
            from app.services.completed_stock_identity import apply as apply_completed_identity
            apply_completed_identity(db, lot, preview)
        else:
            lot.finished_detail.physical_basis_json = json.dumps(preview["physical_basis"],
                ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        movement = _movement(db, lot=lot, movement_type="adjust", quantity=0,
            before=before, operator_id=actor.id, reason="确认批次规格工艺",
            remarks=json.dumps(request, sort_keys=True), idempotency_key=key)
        append_audit_event(db, event_category="business", result="success", source="web",
            module_code="warehouse", action_code=("confirm_completed_component_identity"
                if lot.inventory_type == "semi_finished" else "confirm_finished_physical_identity"),
            resource="inventory_lot", actor=actor, entity_type="inventory_lot", entity_id=lot.id,
            customer_id=(lot.finished_detail or lot.semi_finished_detail).owner_customer_id, details=preview)
        db.flush()
        return {"lot_id": lot_id, "movement_id": movement.id, "replayed": False}


ORDER_IDENTITY_FIELDS = {
    "snapshot_spec": "spec", "snapshot_material": "material", "flute_type": "flute",
    "layer_count": "layer_count", "snapshot_production_notes": "production_notes",
    **{"snapshot_" + key: key for key in FIELDS if key.startswith(("crease_", "base_crease_"))
       or key in ("splice_mode", "pieces_per_box", "flap_mm")},
}


def new_order_stock_basis(product, item):
    """Capture the selected physical product plus this order's explicit overrides."""
    value = json.loads(product_basis(product))
    # Box configuration defaults are operational projections, not a later
    # measurement of old physical stock. Capture the product's original box
    # facts unchanged; only explicit per-order identity overrides differ.
    for source in ("snapshot_spec", "snapshot_material", "flute_type", "layer_count", "snapshot_production_notes"):
        target = ORDER_IDENTITY_FIELDS[source]
        actual = getattr(item, source, None)
        if actual is not None:
            value[target] = normalized_specification_text(actual) if target == "spec" else _value(actual)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def revise_order_stock_basis(item, before, payload, product):
    """Explicit pre-production edits preserve every untouched frozen field.

    Never repair a historical NULL from the current product. The existing
    order mutation audits the before/after document and rejects active stock.
    """
    if not item.stock_identity_snapshot_json:
        return
    value = json.loads(item.stock_identity_snapshot_json)
    for source, target in ORDER_IDENTITY_FIELDS.items():
        actual = getattr(item, source, None)
        if actual != before[source]:
            value[target] = normalized_specification_text(actual) if target == "spec" else _value(actual)
    for key in ("production_process", "production_notes", "box_style"):
        if key in payload.model_fields_set and getattr(payload, key, None) is not None:
            value[key] = _value(getattr(payload, key))
    item.stock_identity_snapshot_json = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def draft_order_stock_basis(product, payload, *, pdf_matched_product=False):
    from types import SimpleNamespace
    from sqlalchemy.orm import object_session
    from app.models.material import Material
    from app.services.product_specification import resolved_product_specification
    from app.services.flute_mapping import normalize_flute_type
    material_id = None if pdf_matched_product else getattr(payload, "material_id", None)
    selected = object_session(product).get(Material, material_id) if material_id is not None else product.material
    material = (getattr(payload, "material", None) or "").strip() or (selected.code if selected else None)
    if pdf_matched_product and selected:
        material = selected.code
    return new_order_stock_basis(product, SimpleNamespace(
        snapshot_spec=resolved_product_specification(getattr(payload, "specification", None), product),
        snapshot_material=material,
        layer_count=selected.layer_count if selected else getattr(payload, "layer_count", None),
        flute_type=normalize_flute_type((getattr(payload, "flute_type", None) or "").strip() or product.flute_type),
        snapshot_production_notes=(getattr(payload, "production_notes", None) or "").strip() or None))
