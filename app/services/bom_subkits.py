"""Sub-kit recipe persistence. Callers own authorization and transaction commit."""
import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.bom_subkit import OrderSubkit, ProductSubkit
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
from app.models.user import User
from app.services.audit_log import append_audit_event
from app.services.bom_subkit_planning import SubkitMember, normalize_members
from app.services.composite_bom_execution import as_integer


class SubkitError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def recipe_rows(row: ProductSubkit | OrderSubkit) -> list[dict]:
    return json.loads(row.recipe_json)


def active_subkit_order(db: Session, lot) -> int | None:
    """Conversion stock belongs to its unfinished parent order, even after moving."""
    if lot.source_ref_type not in ("subkit_conversion", "bom_assembly"):
        return None
    from app.models.bom_subkit import SubkitConversion
    from app.models.multilevel_bom import BomAssembly
    from app.models.order import Order
    conversion = db.get(BomAssembly if lot.source_ref_type == "bom_assembly" else SubkitConversion, lot.source_ref_id)
    if conversion is None or conversion.status != "posted":
        raise SubkitError("内衬组套来源失效，请核对")
    item = db.get(OrderItem, conversion.order_item_id)
    order = db.get(Order, item.order_id)
    if (not item.is_force_closed and item.delivered_quantity < item.quantity
            and order.status not in ("cancelled", "closed", "已作废", "已结单")):
        return item.id
    return None


def require_free_subkit_stock(db: Session, lot) -> None:
    # The dedicated parent dispatch consumes both parent and liner atomically.
    # Never let an ordinary reservation or unordered dispatch steal that liner.
    if active_subkit_order(db, lot) is not None:
        raise SubkitError("该内衬已保留给原订单，随父件送货，不能另行预占或无单出库")


def read_subkit(db: Session, parent_product_id: int) -> dict | None:
    definition = db.get(ProductSubkit, parent_product_id)
    if definition is None:
        return None
    kit = db.get(Product, definition.kit_product_id)
    return {
        "kit_product_id": definition.kit_product_id,
        "name": kit.product_name,
        "kits_per_parent": definition.kits_per_parent,
        "members": recipe_rows(definition),
        "version": definition.version,
        "enabled": definition.enabled,
    }


def save_subkit(
    db: Session, *, parent_product_id: int, name: str,
    kits_per_parent: int, members: list[dict], expected_version: int,
    actor: User, enabled: bool = True,
) -> dict:
    parent = db.get(Product, parent_product_id)
    if parent is None or not parent.is_active or parent.deleted_at is not None:
        raise SubkitError("父产品不存在或已停用", 404)
    if not parent.is_composite or parent.is_virtual_composite_parent or parent.composite_fulfillment_mode != "parent_delivery":
        raise SubkitError("子套件仅用于实体父件交付的组合产品")
    name = str(name or "").strip()
    if not name or len(name) > 250 or name == parent.product_name:
        raise SubkitError("请填写独立的子套件名称", 400)
    count = as_integer(kits_per_parent, label="每父件套数", minimum=1)
    definition = db.get(ProductSubkit, parent_product_id)
    current_version = definition.version if definition else 0
    if expected_version != current_version:
        raise SubkitError("组套设置已变化，请刷新")
    relations = list(db.scalars(select(ProductBomComponent).where(
        ProductBomComponent.parent_product_id == parent_product_id
    )))
    source = {r.component_product_id: r for r in relations}
    parsed = [SubkitMember(row["product_id"], row["pieces_per_kit"]) for row in members]
    # -1 is not a real product id; allocate a distinct positive validation-only id
    # before the actual product exists, without writing a placeholder identity.
    validation_kit_id = definition.kit_product_id if definition else max([parent_product_id, *source], default=0) + 1
    recipe = normalize_members(parsed, parent_product_id=parent_product_id, kit_product_id=validation_kit_id)
    if {r.product_id for r in recipe} != set(source):
        raise SubkitError("当前子套件需包含全部内部组件，不能遗漏或重复")
    for member in recipe:
        relation = source[member.product_id]
        component = db.get(Product, member.product_id)
        if (component is None or component.customer_id != parent.customer_id
                or not component.is_active or component.deleted_at is not None
                or component.is_composite or not relation.is_required):
            raise SubkitError("组件必须为同客户的有效必需组件，不能嵌套组套")
        if relation.quantity_per_set != member.pieces_per_kit * count:
            raise SubkitError("每套用量×每父件套数必须与BOM每父件用量一致")
    # Serialize writes against parent version too, including concurrent BOM edits.
    parent_version = int(parent.version)
    changed = db.execute(update(Product).where(
        Product.id == parent.id, Product.version == parent_version
    ).values(version=parent_version + 1))
    if changed.rowcount != 1:
        raise SubkitError("常用箱已变化，请刷新")
    payload = [{"product_id": r.product_id, "pieces_per_kit": r.pieces_per_kit} for r in recipe]
    if definition is None:
        kit = Product(
            customer_id=parent.customer_id,
            product_code=parent.product_code,
            customer_material_code=parent.customer_material_code,
            product_name=name,
            unit="套",
            is_internal_component=True,
        )
        db.add(kit)
        db.flush()
        definition = ProductSubkit(parent_product_id=parent.id, kit_product_id=kit.id,
            kits_per_parent=count, recipe_json=json.dumps(payload), version=1, enabled=enabled)
        db.add(definition)
    else:
        kit = db.get(Product, definition.kit_product_id)
        kit.product_name = name
        kit.version = int(kit.version) + 1
        definition.kits_per_parent = count
        definition.recipe_json = json.dumps(payload)
        definition.version += 1
        definition.enabled = enabled
    db.flush()
    append_audit_event(db, event_category="business", result="success", source="web",
        module_code="products", action_code="save_subkit", resource="product_subkit",
        actor=actor, entity_type="product", entity_id=parent.id,
        customer_id=parent.customer_id, details=read_subkit(db, parent.id))
    return {**read_subkit(db, parent.id), "product_version": parent.version}


def freeze_order_subkit(db: Session, *, order_item_id: int, actor_id: int | None = None) -> OrderSubkit | None:
    existing = db.get(OrderSubkit, order_item_id)
    if existing is not None:
        return existing
    item = db.get(OrderItem, order_item_id)
    if item is None:
        raise SubkitError("订单明细不存在", 404)
    definition = db.get(ProductSubkit, item.product_id)
    if definition is None or not definition.enabled:
        return None
    parent = db.get(Product, item.product_id)
    if parent.is_virtual_composite_parent or parent.composite_fulfillment_mode != "parent_delivery":
        raise SubkitError("请先停用子套件，再修改父件交付方式")
    snapshots = list(db.scalars(select(SalesOrderItemBomComponent).where(
        SalesOrderItemBomComponent.sales_order_item_id == item.id
    )))
    by_product = {s.component_product_id: s for s in snapshots}
    recipe = recipe_rows(definition)
    if set(by_product) != {r["product_id"] for r in recipe}:
        raise SubkitError("订单BOM与组套设置不一致，不能自动切换")
    frozen = []
    for row in recipe:
        component = by_product[row["product_id"]]
        if component.quantity_per_set != row["pieces_per_kit"] * definition.kits_per_parent:
            raise SubkitError("订单已冻结的组件用量与组套配方不一致")
        frozen.append({**row, "bom_snapshot_id": component.id})
    kit = db.get(Product, definition.kit_product_id)
    snapshot = OrderSubkit(order_item_id=item.id, kit_product_id=kit.id,
        kits_per_parent=definition.kits_per_parent, definition_version=definition.version,
        recipe_json=json.dumps(frozen), kit_name_snapshot=kit.product_name, created_by=actor_id)
    db.add(snapshot)
    db.flush()
    return snapshot
