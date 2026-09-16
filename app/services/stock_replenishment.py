from __future__ import annotations

from app.services.replenishment_receipt_progress import receipt_progress

from math import ceil
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import (
    beijing_now_naive,
    beijing_today,
    utc_naive_to_api,
    utc_now_naive,
)
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
from app.models.stock_replenishment import (
    InventoryStockPolicy,
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryReservation,
    SemiFinishedInventoryDetail,
    WarehouseLocation,
)
from app.services.flute_mapping import seven_layer_code_error
from app.services.location_candidates import (
    load_warehouse_location_projection_contexts,
    operational_location_issue,
)
from app.services.warehouse_location_address import employee_location_name
from app.services.requisition_quantities import (
    cutting_factor,
    normalize_cutting_mode,
)
from app.services.warehouse_inventory import (
    SEMI_FINISHED_FLUTES_BY_LAYER,
    WarehouseInventoryError,
    automatic_floor3_finished_turnover_location,
    automatic_raw_material_staging_location,
    manual_finished_in,
    manual_semi_finished_in,
    normalize_material_code,
    replace_semi_finished_lot_allowed_products,
)
from app.services.box_type_rules import box_type_code
from app.services.composite_bom_workflow import component_availability
from app.services.semi_finished_inventory import (
    safe_physical_board_facts_match,
)


class StockReplenishmentError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _lot_primary_replenishment_product_id(
    db: Session,
    lot: InventoryLot,
) -> int | None:
    """Resolve the product that owns a replenishment board lot.

    New replenishment receipts correctly point inventory lots at the incoming
    receipt fact.  Follow that fact back to the original replenishment line so
    the owning product still receives automatic low-stock coverage, while
    compatible sibling products remain available for manual allocation only.
    """

    replenishment_item_id: int | None = None
    if lot.source_ref_type == "stock_replenishment_item":
        replenishment_item_id = lot.source_ref_id
    elif (
        lot.source_ref_type
        in {"incoming_receipt_item", "stock_replenishment_receipt"}
        and lot.source_ref_id is not None
    ):
        receipt_item = db.get(IncomingReceiptItem, lot.source_ref_id)
        replenishment_item_id = (
            receipt_item.stock_replenishment_item_id
            if receipt_item is not None
            else None
        )
    if replenishment_item_id is None:
        return None
    source_item = db.get(StockReplenishmentOrderItem, replenishment_item_id)
    return source_item.product_id if source_item is not None else None


def product_replenishment_defaults(product: Product) -> dict:
    """Return common-box facts used to prepare a replenishment draft.

    The product record remains the source of truth for material and cutting
    facts.  A low-stock policy only owns the warning and target quantities.
    """
    material = product.material
    material_code = (
        material.code
        if material is not None
        else product.default_material_code or product.legacy_material_text
    )
    supplier_name = material.supplier_name if material is not None else None
    layer_count = (
        material.layer_count
        if material is not None and material.layer_count is not None
        else product.layer_count
    )
    flute_type = (
        product.flute_type
        or (material.flute_type if material is not None else None)
    )
    cutting_mode = normalize_cutting_mode(product.default_cutting_mode)
    crease_aliases = {"净": "净料", "毛": "毛片"}
    crease_type = crease_aliases.get(
        str(product.crease_type or "").strip(),
        str(product.crease_type or "").strip(),
    ) or None
    values = {
        "material_id": product.material_id,
        "material_code": material_code,
        "material_supplier_name": supplier_name,
        "layer_count": layer_count,
        "flute_type": str(flute_type or "").strip().upper() or None,
        "report_length_mm": product.report_length_mm,
        "report_width_mm": product.report_width_mm,
        "crease_type": crease_type,
        "crease_left_mm": product.crease_left_mm,
        "crease_middle_mm": product.crease_middle_mm,
        "crease_right_mm": product.crease_right_mm,
        "cutting_mode": cutting_mode,
        "output_per_sheet": cutting_factor(cutting_mode),
        "pieces_per_box": int(product.pieces_per_box or 1),
    }
    required = {
        "material_code": "材质",
        "layer_count": "层数",
        "flute_type": "楞型",
        "report_length_mm": "报料长",
        "report_width_mm": "报料宽",
    }
    missing = [
        label
        for key, label in required.items()
        if values.get(key) in (None, "")
    ]
    if crease_type == "压线" and any(
        values.get(key) is None
        for key in (
            "crease_left_mm",
            "crease_middle_mm",
            "crease_right_mm",
        )
    ):
        missing.append("压线尺寸")
    values["draft_ready"] = not missing
    values["missing_fields"] = missing
    return values


def is_set_replenishment_product(product: Product) -> bool:
    # Assembled sub-kits are real stock (not virtual) but still replenish by BOM.
    return bool(product.is_composite and (
        product.is_virtual_composite_parent or str(product.unit or "").strip() == "套"
    ))


def virtual_composite_replenishment_components(
    db: Session,
    *,
    product: Product,
) -> dict | None:
    """Resolve the physical products behind one virtual composite parent.

    A virtual parent represents a sellable set and intentionally has no board
    size or material of its own.  Stock-warning replenishment must therefore
    validate and order its required BOM components instead of treating the
    parent as one physical common box.
    """

    if not is_set_replenishment_product(product):
        return None
    relations = list(
        db.scalars(
            select(ProductBomComponent)
            .options(
                selectinload(ProductBomComponent.component_product).selectinload(
                    Product.material
                )
            )
            .where(
                ProductBomComponent.parent_product_id == product.id,
                ProductBomComponent.is_required.is_(True),
            )
            .order_by(
                ProductBomComponent.display_order,
                ProductBomComponent.id,
            )
        ).all()
    )
    missing: list[str] = []
    components: list[dict] = []
    if not relations:
        missing.append("必需BOM组件")
    for relation in relations:
        component = relation.component_product
        label = (
            component.product_name
            if component is not None
            else relation.internal_component_code
        )
        if component is None or component.deleted_at is not None or not component.is_active:
            missing.append(f"{label}：组件已停用或不存在")
            continue
        if component.customer_id != product.customer_id:
            missing.append(f"{label}：组件客户不一致")
            continue
        if is_set_replenishment_product(component):
            missing.append(f"{label}：不支持嵌套组合组件")
            continue
        quantity_per_set = int(relation.quantity_per_set or 0)
        if (
            quantity_per_set <= 0
            or relation.quantity_per_set != quantity_per_set
        ):
            missing.append(f"{label}：每套组件数量无效")
            continue
        defaults = product_replenishment_defaults(component)
        missing.extend(
            f"{label}：{field}" for field in defaults["missing_fields"]
        )
        output_per_sheet = max(int(defaults.get("output_per_sheet") or 1), 1)
        if relation.is_die_cut and relation.mold_max_yield_per_sheet is not None:
            mold_yield = int(relation.mold_max_yield_per_sheet)
            if output_per_sheet > mold_yield:
                missing.append(f"{label}：默认开料出数超过模具最大出数")
        components.append(
            {
                "relation": relation,
                "product": component,
                "quantity_per_set": quantity_per_set,
                "defaults": defaults,
                "output_per_sheet": output_per_sheet,
            }
        )
    supplier_names = sorted(
        {
            str(row["defaults"].get("material_supplier_name") or "").strip()
            for row in components
            if str(row["defaults"].get("material_supplier_name") or "").strip()
        }
    )
    if len(supplier_names) > 1:
        missing.append("BOM组件分属多个供应商，请分开报料")
    return {
        "draft_ready": not missing and len(components) == len(relations),
        "missing_fields": missing,
        "components": components,
        "supplier_names": supplier_names,
        "supplier_name": supplier_names[0] if len(supplier_names) == 1 else None,
    }


def product_replenishment_signature(
    product: Product,
    *,
    output_per_sheet: int | None = None,
) -> tuple | None:
    """Build the physical paperboard signature used for optional grouping."""
    defaults = product_replenishment_defaults(product)
    if not defaults["draft_ready"]:
        return None
    crease_segments = (
        defaults["crease_left_mm"],
        defaults["crease_middle_mm"],
        defaults["crease_right_mm"],
    ) if defaults["crease_type"] == "压线" else (None, None, None)
    return (
        product.customer_id,
        normalize_material_code(defaults["material_code"]),
        defaults["material_supplier_name"] or "",
        int(defaults["layer_count"]),
        defaults["flute_type"],
        int(defaults["report_length_mm"]),
        int(defaults["report_width_mm"]),
        defaults["crease_type"] or "",
        *crease_segments,
        int(output_per_sheet or defaults["output_per_sheet"]),
        int(defaults["pieces_per_box"]),
    )


def theoretical_requisition_quantity(
    finished_quantity: int,
    cutting_mode: str | None,
    pieces_per_box: int = 1,
) -> int:
    from app.services.requisition_quantities import required_piece_quantity, purchase_sheet_quantity
    return purchase_sheet_quantity(required_piece_quantity(finished_quantity, pieces_per_box), 0, cutting_mode)


def compatible_customer_product_ids(
    db: Session,
    item: StockReplenishmentOrderItem,
) -> list[int]:
    """Return products that may use one customer-dedicated board lot.

    A supplier-delivered board has not yet been printed, die-cut or glued.
    It therefore belongs to the customer and physical board signature, not to
    one finished product.  The existing lot-level hard bindings record every
    compatible product without increasing finished-goods inventory.
    """
    if item.customer_id is None or item.product_id is None:
        return []
    primary = db.scalar(
        select(Product)
        .options(selectinload(Product.material))
        .where(Product.id == item.product_id)
    )
    if primary is None or primary.deleted_at is not None or not primary.is_active:
        return []
    signature = product_replenishment_signature(primary)
    if signature is None:
        return [primary.id]
    products = db.scalars(
        select(Product)
        .options(selectinload(Product.material))
        .where(
            Product.customer_id == item.customer_id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
        )
        .order_by(Product.product_code, Product.id)
    ).all()
    return [
        product.id
        for product in products
        if product_replenishment_signature(product) == signature
    ]


def _finished_inventory_quantity_summary(
    db: Session,
    *,
    customer_id: int,
    product_condition,
) -> dict[str, int]:
    from app.services.bom_inventory_contract import complete_stock_condition
    row = db.execute(
        select(
            func.coalesce(func.sum(InventoryLot.quantity_available), 0),
            func.coalesce(
                func.sum(
                    case(
                        (
                            FinishedGoodsInventoryDetail.is_general.is_(True),
                            InventoryLot.quantity_available,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(func.sum(InventoryLot.quantity_reserved), 0),
            func.coalesce(
                func.sum(
                    case(
                        (
                            FinishedGoodsInventoryDetail.is_general.is_(True),
                            InventoryLot.quantity_reserved,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .select_from(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(
            WarehouseLocation,
            WarehouseLocation.id == InventoryLot.warehouse_location_id,
        )
        .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .where(
            complete_stock_condition(),
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            Product.customer_id == customer_id,
            product_condition,
            or_(
                and_(
                    FinishedGoodsInventoryDetail.is_general.is_(False),
                    FinishedGoodsInventoryDetail.owner_customer_id == customer_id,
                ),
                FinishedGoodsInventoryDetail.is_general.is_(True),
            ),
            or_(
                WarehouseLocation.source_version.is_(None),
                WarehouseLocation.source_version != "V11",
                and_(
                    WarehouseLocation.source_version == "V11",
                    WarehouseLocation.warehouse_floor == 3,
                ),
            ),
        )
    ).one()
    allocatable = int(row[0] or 0)
    general_allocatable = int(row[1] or 0)
    reserved = int(row[2] or 0)
    general_reserved = int(row[3] or 0)
    physical_unconsumed = allocatable + reserved
    general_physical = general_allocatable + general_reserved
    return {
        "available_quantity": physical_unconsumed,
        "allocatable_available_quantity": allocatable,
        "dedicated_available_quantity": max(
            physical_unconsumed - general_physical,
            0,
        ),
        "general_available_quantity": general_physical,
        "reserved_quantity": reserved,
        "physical_unconsumed_quantity": physical_unconsumed,
    }


def _composite_parent_finished_quantity_summary(
    db: Session,
    *,
    customer_id: int,
    inventory_code: str,
    product_id: int,
) -> dict[str, int]:
    """Return complete parent sets without adding same-code component pieces.

    Composite components may deliberately share the parent's inventory code.
    Their physical ledgers remain in pieces, so adding those ledgers turns one
    finished set into the sum of all BOM pieces.  Only order-scoped active BOM
    reservations can establish which pieces belong together.  Convert each
    required component back to sets, take the shortest component and cap it by
    the parent order's undelivered set quantity.
    """

    parent_product_ids = list(
        db.scalars(
            select(Product.id).where(
                Product.customer_id == customer_id,
                func.lower(func.trim(Product.product_code)) == inventory_code,
                Product.id == product_id,
                or_(
                    Product.is_composite.is_(True),
                    Product.is_virtual_composite_parent.is_(True),
                ),
            )
        ).all()
    )
    if not parent_product_ids:
        return {
            "available_quantity": 0,
            "allocatable_available_quantity": 0,
            "dedicated_available_quantity": 0,
            "general_available_quantity": 0,
            "reserved_quantity": 0,
            "physical_unconsumed_quantity": 0,
        }

    direct = _finished_inventory_quantity_summary(
        db,
        customer_id=customer_id,
        product_condition=FinishedGoodsInventoryDetail.product_id.in_(
            parent_product_ids
        ),
    )
    reservation_remaining = (
        InventoryReservation.reserved_stock_quantity
        - InventoryReservation.consumed_stock_quantity
        - InventoryReservation.released_stock_quantity
    )
    order_item_ids = list(
        db.scalars(
            select(OrderItem.id)
            .join(
                SalesOrderItemBomComponent,
                SalesOrderItemBomComponent.sales_order_item_id == OrderItem.id,
            )
            .join(
                InventoryReservation,
                InventoryReservation.sales_order_item_bom_component_id
                == SalesOrderItemBomComponent.id,
            )
            .where(
                OrderItem.product_id.in_(parent_product_ids),
                OrderItem.is_force_closed.is_(False),
                OrderItem.quantity > func.coalesce(OrderItem.delivered_quantity, 0),
                InventoryReservation.reservation_type == "finished_order",
                InventoryReservation.status.in_(("active", "partial")),
                reservation_remaining > 0,
            )
            .distinct()
            .order_by(OrderItem.id)
        ).all()
    )

    component_reserved_sets = 0
    for order_item_id in order_item_ids:
        item = db.get(OrderItem, order_item_id)
        if item is None:
            continue
        required_component_sets: list[int] = []
        snapshots = db.scalars(
            select(SalesOrderItemBomComponent)
            .where(
                SalesOrderItemBomComponent.sales_order_item_id == order_item_id,
                SalesOrderItemBomComponent.is_required.is_(True),
                SalesOrderItemBomComponent.component_product_id.notin_(parent_product_ids),
            )
            .order_by(
                SalesOrderItemBomComponent.display_order,
                SalesOrderItemBomComponent.id,
            )
        ).all()
        for snapshot in snapshots:
            availability = component_availability(db, int(snapshot.id))
            required_component_sets.append(
                int(availability.stock_quantity)
                // max(int(availability.quantity_per_set), 1)
            )
        if not required_component_sets:
            continue
        remaining_order_sets = max(
            int(item.quantity or 0) - int(item.delivered_quantity or 0),
            0,
        )
        component_reserved_sets += min(
            min(required_component_sets),
            remaining_order_sets,
        )

    physical_unconsumed = (
        int(direct["physical_unconsumed_quantity"]) + component_reserved_sets
    )
    reserved = int(direct["reserved_quantity"]) + component_reserved_sets
    dedicated = (
        int(direct["dedicated_available_quantity"]) + component_reserved_sets
    )
    return {
        "available_quantity": physical_unconsumed,
        "allocatable_available_quantity": int(
            direct["allocatable_available_quantity"]
        ),
        "dedicated_available_quantity": dedicated,
        "general_available_quantity": int(direct["general_available_quantity"]),
        "reserved_quantity": reserved,
        "physical_unconsumed_quantity": physical_unconsumed,
        "assembled_quantity": int(direct["physical_unconsumed_quantity"]),
        "reserved_component_set_quantity": component_reserved_sets,
    }


def finished_product_quantity_summary(
    db: Session,
    *,
    product_id: int,
    customer_id: int,
) -> dict[str, int]:
    """Return warehouse stock for one customer inventory code.

    Stock warnings describe how many sound finished goods are still physically
    in the warehouse, so ``available_quantity`` includes both allocatable and
    already-reserved quantities.  ``allocatable_available_quantity`` keeps the
    narrower allocation fact explicit and prevents reserved stock from being
    allocated again.  Inventory is grouped by the frozen inventory-code
    snapshot instead of one product master id because historical and composite
    products may legitimately have multiple master rows for the same code.

    Valid third-floor V11 locations are formal inventory even while their
    placement status still needs attention.
    """
    product_fact = db.execute(
        select(
            Product.product_code,
            Product.customer_id,
            Product.is_composite,
            Product.is_virtual_composite_parent,
        ).where(
            Product.id == product_id
        )
    ).one_or_none()
    if product_fact is None or int(product_fact.customer_id) != int(customer_id):
        return {
            "available_quantity": 0,
            "allocatable_available_quantity": 0,
            "dedicated_available_quantity": 0,
            "general_available_quantity": 0,
            "reserved_quantity": 0,
            "physical_unconsumed_quantity": 0,
        }
    inventory_code = str(product_fact.product_code or "").strip().lower()
    if not inventory_code:
        return {
            "available_quantity": 0,
            "allocatable_available_quantity": 0,
            "dedicated_available_quantity": 0,
            "general_available_quantity": 0,
            "reserved_quantity": 0,
            "physical_unconsumed_quantity": 0,
        }
    if bool(product_fact.is_composite) or bool(
        product_fact.is_virtual_composite_parent
    ):
        return _composite_parent_finished_quantity_summary(
            db,
            customer_id=customer_id,
            inventory_code=inventory_code,
            product_id=product_id,
        )
    return _finished_inventory_quantity_summary(
        db,
        customer_id=customer_id,
        product_condition=(
            func.lower(
                func.trim(FinishedGoodsInventoryDetail.inventory_code_snapshot)
            )
            == inventory_code
        ),
    )


def current_policy_quantity(db: Session, policy: InventoryStockPolicy) -> int:
    if policy.target_inventory_type == "finished":
        if policy.product_id is None:
            return 0
        customer_id = policy.customer_id
        if customer_id is None:
            customer_id = db.scalar(
                select(Product.customer_id).where(Product.id == policy.product_id)
            )
        if customer_id is None:
            return 0
        return finished_product_quantity_summary(
            db,
            product_id=policy.product_id,
            customer_id=int(customer_id),
        )["available_quantity"]

    if policy.target_inventory_type != "semi_finished":
        return 0
    conditions = [
        InventoryLot.inventory_type == "semi_finished",
        InventoryLot.status == "active",
        SemiFinishedInventoryDetail.board_length_mm == policy.report_length_mm,
        SemiFinishedInventoryDetail.board_width_mm == policy.report_width_mm,
        SemiFinishedInventoryDetail.normalized_material_code
        == policy.normalized_material_code,
        SemiFinishedInventoryDetail.flute_type == policy.flute_type,
        SemiFinishedInventoryDetail.component_type == policy.component_type,
        SemiFinishedInventoryDetail.pieces_per_box == policy.pieces_per_box,
        SemiFinishedInventoryDetail.stock_yield_per_sheet
        == policy.stock_yield_per_sheet,
        SemiFinishedInventoryDetail.sheet_type == policy.sheet_type,
    ]
    if policy.customer_id is None:
        conditions.append(SemiFinishedInventoryDetail.owner_customer_id.is_(None))
    else:
        conditions.append(
            SemiFinishedInventoryDetail.owner_customer_id == policy.customer_id
        )
    value = db.scalar(
        select(func.coalesce(func.sum(InventoryLot.quantity_available), 0))
        .join(
            SemiFinishedInventoryDetail,
            SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(WarehouseLocation, WarehouseLocation.id == InventoryLot.warehouse_location_id)
        .where(
            *conditions,
            or_(
                WarehouseLocation.source_version.is_(None),
                WarehouseLocation.source_version != "V11",
            ),
        )
    )
    return int(value or 0)


def _replenishment_item_signature(
    item: StockReplenishmentOrderItem,
) -> tuple | None:
    if (
        item.customer_id is None
        or item.layer_count is None
        or not item.flute_type
        or item.report_length_mm is None
        or item.report_width_mm is None
        or not item.material_code_snapshot
    ):
        return None
    crease_type = str(item.crease_type or "").strip()
    crease_segments = (
        item.crease_left_mm,
        item.crease_middle_mm,
        item.crease_right_mm,
    ) if crease_type == "压线" else (None, None, None)
    return (
        item.customer_id,
        normalize_material_code(item.material_code_snapshot),
        str(item.order.supplier_name or "").strip(),
        int(item.layer_count),
        str(item.flute_type).strip().upper(),
        int(item.report_length_mm),
        int(item.report_width_mm),
        crease_type,
        *crease_segments,
        int(item.stock_yield_per_sheet or 1),
        int(item.pieces_per_box or 1),
    )


def customer_board_preparation_coverage(
    db: Session,
    *,
    product: Product,
    output_per_sheet: int | None = None,
) -> dict[str, int]:
    """Return available and already-ordered board sheets without calling them finished stock."""

    composite = virtual_composite_replenishment_components(
        db,
        product=product,
    )
    if composite is not None:
        empty = {
            "customer_board_preparation_available_sheet_quantity": 0,
            "customer_board_preparation_finished_capacity": 0,
            "customer_board_preparation_auto_cover_capacity": 0,
            "incoming_board_preparation_sheet_quantity": 0,
            "incoming_board_preparation_finished_capacity": 0,
            "incoming_board_preparation_auto_cover_capacity": 0,
        }
        if not composite["draft_ready"] or not composite["components"]:
            return empty
        component_coverages = [
            (
                row,
                customer_board_preparation_coverage(
                    db,
                    product=row["product"],
                    output_per_sheet=int(row["output_per_sheet"]),
                ),
            )
            for row in composite["components"]
        ]

        def complete_set_capacity(key: str) -> int:
            return min(
                int(coverage[key]) // int(row["quantity_per_set"])
                for row, coverage in component_coverages
            )

        return {
            "customer_board_preparation_available_sheet_quantity": sum(
                int(coverage["customer_board_preparation_available_sheet_quantity"])
                for _row, coverage in component_coverages
            ),
            "customer_board_preparation_finished_capacity": complete_set_capacity(
                "customer_board_preparation_finished_capacity"
            ),
            "customer_board_preparation_auto_cover_capacity": complete_set_capacity(
                "customer_board_preparation_auto_cover_capacity"
            ),
            "incoming_board_preparation_sheet_quantity": sum(
                int(coverage["incoming_board_preparation_sheet_quantity"])
                for _row, coverage in component_coverages
            ),
            "incoming_board_preparation_finished_capacity": complete_set_capacity(
                "incoming_board_preparation_finished_capacity"
            ),
            "incoming_board_preparation_auto_cover_capacity": complete_set_capacity(
                "incoming_board_preparation_auto_cover_capacity"
            ),
        }

    defaults = product_replenishment_defaults(product)
    effective_output_per_sheet = max(
        int(output_per_sheet or defaults["output_per_sheet"] or 1),
        1,
    )
    available_sheets = 0
    available_finished_capacity = 0
    available_auto_cover_capacity = 0
    if defaults["draft_ready"]:
        from app.services.semi_finished_inventory import (
            semi_finished_candidates_for_product,
        )

        candidates = semi_finished_candidates_for_product(
            db,
            product_id=product.id,
            customer_id=product.customer_id,
            board_length_mm=int(defaults["report_length_mm"]),
            board_width_mm=int(defaults["report_width_mm"]),
            material_code=str(defaults["material_code"]),
            flute_type=str(defaults["flute_type"]),
            component_type="whole",
            pieces_per_box=int(defaults["pieces_per_box"]),
            stock_yield_per_sheet=effective_output_per_sheet,
        )
        for row in candidates:
            detail = row.lot.semi_finished_detail
            customer_generic = bool(
                detail is not None and detail.customer_generic_eligible
            )
            if (
                detail is None
                or detail.owner_customer_id != product.customer_id
                or (
                    not customer_generic
                    and (
                        row.signature_differences
                        or row.source not in {"signature", "learned"}
                    )
                )
                or not safe_physical_board_facts_match(
                    detail,
                    supplier_name=defaults["material_supplier_name"],
                    layer_count=defaults["layer_count"],
                    crease_type=defaults["crease_type"],
                    crease_left_mm=defaults["crease_left_mm"],
                    crease_middle_mm=defaults["crease_middle_mm"],
                    crease_right_mm=defaults["crease_right_mm"],
                )
            ):
                continue
            available_sheets += int(row.available_stock_quantity or 0)
            capacity = int(row.deductible_requirement_quantity or 0)
            available_finished_capacity += capacity
            if customer_generic:
                continue
            primary_product_id = _lot_primary_replenishment_product_id(db, row.lot)
            allowed_product_ids = {
                int(binding.product_id)
                for binding in row.lot.allowed_products
            }
            if (
                primary_product_id == product.id
                or (
                    primary_product_id is None
                    and allowed_product_ids == {product.id}
                )
            ):
                available_auto_cover_capacity += capacity

    signature = product_replenishment_signature(
        product,
        output_per_sheet=effective_output_per_sheet,
    )
    incoming_sheets = 0
    incoming_finished_capacity = 0
    incoming_auto_cover_capacity = 0
    from app.services.replenishment_receipt_progress import short_closed_clause
    if signature is not None:
        incoming_items = db.scalars(
            select(StockReplenishmentOrderItem)
            .join(
                StockReplenishmentOrder,
                StockReplenishmentOrder.id
                == StockReplenishmentOrderItem.replenishment_order_id,
            )
            .where(
                StockReplenishmentOrder.source_type == "stock_warning",
                StockReplenishmentOrder.status.in_(
                    ("confirmed", "partially_stocked")
                ),
                StockReplenishmentOrderItem.target_inventory_type
                == "semi_finished",
                StockReplenishmentOrderItem.quantity
                > StockReplenishmentOrderItem.stocked_quantity,
                StockReplenishmentOrderItem.customer_id
                == product.customer_id,
                ~short_closed_clause(StockReplenishmentOrderItem.id),
            )
            .order_by(
                StockReplenishmentOrder.created_at,
                StockReplenishmentOrderItem.id,
            )
        ).all()
        for item in incoming_items:
            if _replenishment_item_signature(item) != signature:
                continue
            outstanding = max(
                int(item.quantity or 0) - int(item.stocked_quantity or 0),
                0,
            )
            incoming_sheets += outstanding
            capacity = outstanding * int(item.stock_yield_per_sheet or 1)
            incoming_finished_capacity += capacity
            # One incoming quantity has one owning replenishment line.  A
            # compatible-product binding permits future use, but must not make
            # the same physical sheets cover every product's warning at once.
            if item.reference_product_id == product.id or item.product_id == product.id:
                incoming_auto_cover_capacity += capacity
    return {
        "customer_board_preparation_available_sheet_quantity": available_sheets,
        "customer_board_preparation_finished_capacity": (
            available_finished_capacity // max(int(defaults["pieces_per_box"]), 1)
        ),
        "customer_board_preparation_auto_cover_capacity": (
            available_auto_cover_capacity // max(int(defaults["pieces_per_box"]), 1)
        ),
        "incoming_board_preparation_sheet_quantity": incoming_sheets,
        "incoming_board_preparation_finished_capacity": (
            incoming_finished_capacity // max(int(defaults["pieces_per_box"]), 1)
        ),
        "incoming_board_preparation_auto_cover_capacity": (
            incoming_auto_cover_capacity // max(int(defaults["pieces_per_box"]), 1)
        ),
        "available_auto_cover_piece_quantity": available_auto_cover_capacity,
        "incoming_auto_cover_piece_quantity": incoming_auto_cover_capacity,
    }


def free_bom_component_coverage(db: Session, product: Product) -> dict:
    """Read-only coverage, never assembly or a stock reservation.

    Match exact product and frozen physical identity; same-code long/short
    pieces are not interchangeable. Reserved stock cannot cover another order.
    """
    from app.services.finished_stock_identity import product_basis

    if not is_set_replenishment_product(product):
        return {"sets": 0, "pieces": {}}
    relations = list(db.scalars(select(ProductBomComponent).where(
        ProductBomComponent.parent_product_id == product.id,
        ProductBomComponent.is_required.is_(True),
    )))
    pieces, capacities = {}, []
    for relation in relations:
        component = relation.component_product
        ratio = relation.quantity_per_set
        if (component is None or not component.is_active or component.deleted_at
                or component.customer_id != product.customer_id or component.is_composite
                or not ratio or ratio <= 0 or ratio != int(ratio)):
            return {"sets": 0, "pieces": {}}
        quantity = int(db.scalar(select(func.coalesce(func.sum(InventoryLot.quantity_available), 0))
            .join(FinishedGoodsInventoryDetail, FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id)
            .join(WarehouseLocation, WarehouseLocation.id == InventoryLot.warehouse_location_id)
            .where(InventoryLot.inventory_type == "finished", InventoryLot.status == "active",
                InventoryLot.quantity_available > 0,
                FinishedGoodsInventoryDetail.product_id == component.id,
                FinishedGoodsInventoryDetail.physical_basis_json == product_basis(component),
                or_(FinishedGoodsInventoryDetail.owner_customer_id == product.customer_id,
                    FinishedGoodsInventoryDetail.is_general.is_(True)),
                or_(WarehouseLocation.source_version.is_(None), WarehouseLocation.source_version != "V11",
                    WarehouseLocation.warehouse_floor == 3))) or 0)
        pieces[component.id] = quantity
        capacities.append(quantity // int(ratio))
    return {"sets": min(capacities) if capacities else 0, "pieces": pieces}


def virtual_composite_replenishment_demand_plan(
    db: Session,
    *,
    product: Product,
    finished_quantity: int,
) -> dict | None:
    """Build component-level board demand for a virtual finished set."""

    resolved = virtual_composite_replenishment_components(
        db,
        product=product,
    )
    if resolved is None:
        return None
    parent_sets = max(int(finished_quantity or 0), 0)
    free_coverage = free_bom_component_coverage(db, product)
    component_demands: list[dict] = []
    total_sheets = 0
    parent_sets_still_needing_board = 0
    for row in resolved["components"]:
        relation = row["relation"]
        component = row["product"]
        defaults = row["defaults"]
        quantity_per_set = int(row["quantity_per_set"])
        coverage = customer_board_preparation_coverage(
            db,
            product=component,
            output_per_sheet=int(row["output_per_sheet"]),
        )
        required_pieces = parent_sets * quantity_per_set
        covered_pieces = (
            int(coverage["customer_board_preparation_auto_cover_capacity"])
            + int(coverage["incoming_board_preparation_auto_cover_capacity"])
            # Complete free sets already reduced the parent's shortfall. Only
            # unmatched surplus pieces may reduce this remaining demand again.
            + max(free_coverage["pieces"].get(component.id, 0)
                  - free_coverage["sets"] * quantity_per_set, 0)
        )
        outstanding_pieces = max(required_pieces - covered_pieces, 0)
        output_per_sheet = int(row["output_per_sheet"])
        pieces_per_unit = max(int(defaults["pieces_per_box"]), 1)
        free_surplus = max(free_coverage["pieces"].get(component.id, 0)
                           - free_coverage["sets"] * quantity_per_set, 0)
        required_board_pieces = max((required_pieces - free_surplus) * pieces_per_unit
            - int(coverage.get("available_auto_cover_piece_quantity", 0))
            - int(coverage.get("incoming_auto_cover_piece_quantity", 0)), 0)
        net_sheets = (required_board_pieces + output_per_sheet - 1) // output_per_sheet
        spare_sheets = (
            int(relation.spare_sheet_quantity or 0) if net_sheets > 0 else 0
        )
        sheet_quantity = net_sheets + spare_sheets
        total_sheets += sheet_quantity
        parent_sets_still_needing_board = max(
            parent_sets_still_needing_board,
            ceil(outstanding_pieces / quantity_per_set),
        )
        component_demands.append(
            {
                **row,
                "coverage": coverage,
                "required_piece_quantity": required_pieces,
                "covered_piece_quantity": covered_pieces,
                "suggested_component_piece_quantity": outstanding_pieces,
                "net_sheet_quantity": net_sheets,
                "spare_sheet_quantity": spare_sheets,
                "sheet_quantity": sheet_quantity,
            }
        )
    return {
        **resolved,
        "parent_set_quantity": parent_sets,
        "suggested_parent_set_quantity": parent_sets_still_needing_board,
        "suggested_sheet_quantity": total_sheets,
        "component_demands": component_demands,
    }


def stock_policy_dict(
    db: Session,
    policy: InventoryStockPolicy,
    *,
    projection_context: dict | None = None,
) -> dict:
    finished_summary: dict[str, int] = {}
    if (
        policy.target_inventory_type == "finished"
        and policy.product_id is not None
        and policy.customer_id is not None
    ):
        finished_summary = finished_product_quantity_summary(
            db,
            product_id=policy.product_id,
            customer_id=policy.customer_id,
        )
    available = (
        finished_summary["available_quantity"]
        if finished_summary
        else current_policy_quantity(db, policy)
    )
    free_sets = (free_bom_component_coverage(db, policy.product)["sets"]
                 if finished_summary and policy.product is not None else 0)
    available += free_sets
    target = int(policy.target_quantity or 0)
    warning = int(policy.warning_quantity or 0)
    location = policy.default_location
    location_context = projection_context or {}
    board_coverage = {
        "customer_board_preparation_available_sheet_quantity": 0,
        "customer_board_preparation_finished_capacity": 0,
        "customer_board_preparation_auto_cover_capacity": 0,
        "incoming_board_preparation_sheet_quantity": 0,
        "incoming_board_preparation_finished_capacity": 0,
        "incoming_board_preparation_auto_cover_capacity": 0,
    }
    if (
        policy.target_inventory_type == "finished"
        and policy.product is not None
    ):
        board_coverage = customer_board_preparation_coverage(
            db,
            product=policy.product,
        )
    external_purchase_incoming_quantity = 0
    if (
        policy.target_inventory_type == "finished"
        and policy.product is not None
        and policy.product.supply_mode == "external_purchase"
    ):
        external_purchase_incoming_quantity = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            StockReplenishmentOrderItem.quantity
                            - StockReplenishmentOrderItem.stocked_quantity
                        ),
                        0,
                    )
                )
                .join(
                    StockReplenishmentOrder,
                    StockReplenishmentOrder.id
                    == StockReplenishmentOrderItem.replenishment_order_id,
                )
                .where(
                    StockReplenishmentOrder.source_type == "stock_warning",
                    StockReplenishmentOrder.status.in_(
                        ("confirmed", "partially_stocked")
                    ),
                    StockReplenishmentOrderItem.stock_policy_id == policy.id,
                    StockReplenishmentOrderItem.target_inventory_type
                    == "finished",
                    StockReplenishmentOrderItem.quantity
                    > StockReplenishmentOrderItem.stocked_quantity,
                )
            )
            or 0
        )
    suggested_finished_quantity = max(target - available, 0)
    suggested_new_requisition_finished_quantity = max(
        suggested_finished_quantity
        - int(board_coverage["customer_board_preparation_auto_cover_capacity"])
        - int(board_coverage["incoming_board_preparation_auto_cover_capacity"])
        - external_purchase_incoming_quantity,
        0,
    )
    composite_plan = (
        virtual_composite_replenishment_demand_plan(
            db,
            product=policy.product,
            finished_quantity=suggested_finished_quantity,
        )
        if policy.product is not None
        else None
    )
    if composite_plan is not None:
        output_per_sheet = 1
        suggested_new_requisition_finished_quantity = int(
            composite_plan["suggested_parent_set_quantity"]
        )
        suggested_new_requisition_sheet_quantity = int(
            composite_plan["suggested_sheet_quantity"]
        )
    else:
        output_per_sheet = (
            int(product_replenishment_defaults(policy.product)["output_per_sheet"])
            if policy.product is not None
            else 1
        )
        pieces_per_box = max(int(product_replenishment_defaults(policy.product)["pieces_per_box"]), 1) if policy.product is not None else 1
        if policy.target_inventory_type != "finished":
            # Material warning quantities are already physical sheets.
            pieces_per_box = output_per_sheet = 1
        required_pieces = max((suggested_finished_quantity - external_purchase_incoming_quantity) * pieces_per_box
            - int(board_coverage.get("available_auto_cover_piece_quantity", 0))
            - int(board_coverage.get("incoming_auto_cover_piece_quantity", 0)), 0)
        suggested_new_requisition_sheet_quantity = (required_pieces + output_per_sheet - 1) // output_per_sheet
        suggested_new_requisition_finished_quantity = (required_pieces + pieces_per_box - 1) // pieces_per_box
    replenishment_state = (
        "purchase_needed"
        if suggested_new_requisition_sheet_quantity > 0
        else "already_ordered"
        if external_purchase_incoming_quantity > 0
        else "board_preparation_ready"
        if board_coverage[
            "customer_board_preparation_available_sheet_quantity"
        ]
        > 0
        else "already_ordered"
        if board_coverage["incoming_board_preparation_sheet_quantity"] > 0
        else "target_covered"
    )
    return {
        "id": policy.id,
        "policy_name": policy.policy_name,
        "target_inventory_type": policy.target_inventory_type,
        "product_id": policy.product_id,
        "customer_id": policy.customer_id,
        "customer_name": policy.customer.name if policy.customer else None,
        "product_code": policy.product.product_code if policy.product else None,
        "product_name": policy.product.product_name if policy.product else None,
        "material_code": policy.material_code_snapshot,
        "layer_count": policy.layer_count,
        "flute_type": policy.flute_type,
        "report_length_mm": policy.report_length_mm,
        "report_width_mm": policy.report_width_mm,
        "sheet_type": policy.sheet_type,
        "component_type": policy.component_type,
        "pieces_per_box": policy.pieces_per_box,
        "stock_yield_per_sheet": policy.stock_yield_per_sheet,
        "warning_quantity": warning,
        "target_quantity": target,
        "available_quantity": available,
        "unassembled_available_set_quantity": free_sets,
        "assembled_quantity": finished_summary.get("assembled_quantity"),
        "reserved_component_set_quantity": finished_summary.get("reserved_component_set_quantity", 0),
        "dedicated_available_quantity": finished_summary.get(
            "dedicated_available_quantity"
        ),
        "general_available_quantity": finished_summary.get(
            "general_available_quantity"
        ),
        "allocatable_available_quantity": finished_summary.get(
            "allocatable_available_quantity"
        ),
        "reserved_quantity": finished_summary.get("reserved_quantity"),
        "physical_unconsumed_quantity": finished_summary.get(
            "physical_unconsumed_quantity"
        ),
        "warning_triggered": bool(policy.active and available < warning),
        "suggested_replenishment_quantity": suggested_finished_quantity,
        **board_coverage,
        "suggested_new_requisition_finished_quantity": (
            suggested_new_requisition_finished_quantity
        ),
        "suggested_new_requisition_sheet_quantity": (
            suggested_new_requisition_sheet_quantity
        ),
        "external_purchase_incoming_quantity": (
            external_purchase_incoming_quantity
        ),
        "is_virtual_composite_parent": composite_plan is not None,
        "bom_component_count": (
            len(composite_plan["components"])
            if composite_plan is not None
            else 0
        ),
        "replenishment_state": replenishment_state,
        "default_location": (
            {
                "id": location.id,
                "location_code": location.location_code,
                "location_name": employee_location_name(
                    location,
                    area=location_context.get("area"),
                    floor=location_context.get("floor"),
                    area_sequence=location_context.get("area_sequence"),
                ),
                "location_master_name": location.location_name,
                "warehouse_type": location.warehouse_type,
            }
            if location
            else None
        ),
        "supplier_name": policy.supplier_name,
        "remark": policy.remark,
        "active": policy.active,
        "updated_at": utc_naive_to_api(policy.updated_at) if policy.updated_at else None,
    }


def validate_stock_policy(db: Session, policy: InventoryStockPolicy) -> None:
    if policy.target_quantity <= 0 or policy.warning_quantity < 0:
        raise StockReplenishmentError("预警数量不能小于0，目标数量必须大于0。")
    if policy.target_quantity < policy.warning_quantity:
        raise StockReplenishmentError("目标库存不能小于预警库存。")
    if policy.target_inventory_type == "finished":
        product = db.get(Product, policy.product_id) if policy.product_id else None
        if product is None or product.deleted_at is not None:
            raise StockReplenishmentError("成品库存预警必须选择有效产品。")
        policy.customer_id = product.customer_id
    elif policy.target_inventory_type == "semi_finished":
        required = (
            policy.material_code_snapshot,
            policy.normalized_material_code,
            policy.layer_count,
            policy.flute_type,
            policy.report_length_mm,
            policy.report_width_mm,
        )
        if not all(value not in (None, "") for value in required):
            raise StockReplenishmentError(
                "半成品库存预警必须填写材质、层数、楞型和报料长宽。"
            )
        code_error = seven_layer_code_error(
            policy.material_code_snapshot,
            policy.layer_count,
        )
        if code_error:
            raise StockReplenishmentError(code_error)
        flute_type = str(policy.flute_type or "").strip().upper()
        if (
            policy.layer_count not in SEMI_FINISHED_FLUTES_BY_LAYER
            or flute_type
            not in SEMI_FINISHED_FLUTES_BY_LAYER[policy.layer_count]
        ):
            raise StockReplenishmentError(
                "三层只允许A/B/E楞，五层只允许AB/BE楞，七层只允许AAA/ABC楞。"
            )
        policy.flute_type = flute_type
    else:
        raise StockReplenishmentError("库存预警类型无效。")

    if policy.default_location_id:
        location = db.get(WarehouseLocation, policy.default_location_id)
        if location is None or not location.is_active:
            raise StockReplenishmentError("默认库位不存在或已停用。")
        if location.source_version == "V11":
            raise StockReplenishmentError(
                "V11 三楼 Phase A 货位不能用于正式库存预警策略。", 409
            )
        allowed = {
            "finished": {"finished", "shared"},
            "semi_finished": {"semi_finished", "shared"},
        }[policy.target_inventory_type]
        if location.warehouse_type not in allowed:
            raise StockReplenishmentError("默认库位类型与目标库存类型不一致。")
        location_issue = operational_location_issue(
            db,
            location,
            warehouse_types=allowed,
        )
        if location_issue:
            raise StockReplenishmentError(
                f"默认库位不可使用：{location_issue}", 409
            )


def next_replenishment_order_number() -> str:
    return f"SR-{beijing_now_naive():%Y%m%d}-{uuid4().hex[:8].upper()}"


def replenishment_item_dict(
    item: StockReplenishmentOrderItem,
    *,
    projection_context: dict | None = None,
    db: Session | None = None,
) -> dict:
    location = item.location
    lot = item.inventory_lot
    location_context = projection_context or {}
    allowed_products = (
        [
            {
                "id": binding.product_id,
                "product_code": binding.product.product_code,
                "product_name": binding.product.product_name,
            }
            for binding in lot.allowed_products
            if binding.product is not None
        ]
        if lot and lot.inventory_type == "semi_finished"
        else []
    )
    return {
        "id": item.id,
        "stock_policy_id": item.stock_policy_id,
        "target_inventory_type": item.target_inventory_type,
        "product_id": item.product_id,
        "reference_product_id": item.reference_product_id,
        "customer_id": item.customer_id,
        "product_code": item.product_code_snapshot,
        "product_name": item.product_name_snapshot,
        "internal_name": item.internal_name,
        "material_id": item.material_id,
        "material_code": item.material_code_snapshot,
        "layer_count": item.layer_count,
        "flute_type": item.flute_type,
        "report_length_mm": item.report_length_mm,
        "report_width_mm": item.report_width_mm,
        "crease_type": item.crease_type,
        "crease_left_mm": item.crease_left_mm,
        "crease_middle_mm": item.crease_middle_mm,
        "crease_right_mm": item.crease_right_mm,
        "sheet_type": item.sheet_type,
        "component_type": item.component_type,
        "pieces_per_box": item.pieces_per_box,
        "stock_yield_per_sheet": item.stock_yield_per_sheet,
        "quantity": item.quantity,
        "stocked_quantity": receipt_progress(db, item)['received_quantity'] if db is not None else item.stocked_quantity,
        "receipt_progress": receipt_progress(db, item) if db is not None else None,
        "location": (
            {
                "id": location.id,
                "location_code": location.location_code,
                "location_name": employee_location_name(
                    location,
                    area=location_context.get("area"),
                    floor=location_context.get("floor"),
                    area_sequence=location_context.get("area_sequence"),
                ),
                "location_master_name": location.location_name,
            }
            if location
            else None
        ),
        "inventory_lot": (
            {
                "id": lot.id,
                "lot_number": lot.lot_number,
                "quantity_available": lot.quantity_available,
                "customer_generic_eligible": bool(
                    lot.semi_finished_detail
                    and lot.semi_finished_detail.customer_generic_eligible
                ),
                "internal_name": (
                    lot.semi_finished_detail.internal_name
                    if lot.semi_finished_detail is not None
                    else None
                ),
                "display_name": (
                    "客户通用纸板备料"
                    if lot.inventory_type == "semi_finished"
                    and lot.semi_finished_detail is not None
                    and lot.semi_finished_detail.customer_generic_eligible
                    else "客户专用纸板备料"
                    if lot.inventory_type == "semi_finished" and item.customer_id
                    else "半成品片料"
                    if lot.inventory_type == "semi_finished"
                    else "成品库存"
                ),
                "allowed_products": allowed_products,
            }
            if lot
            else None
        ),
        "historical_source": (
            {
                "workbook": item.historical_workbook,
                "sheet": item.historical_sheet,
                "row": item.historical_row,
                "search_text": item.historical_search_text,
            }
            if item.historical_row
            else None
        ),
        "remark": item.remark,
        "stocked_at": utc_naive_to_api(item.stocked_at) if item.stocked_at else None,
    }


def replenishment_order_dict(
    order: StockReplenishmentOrder,
    *,
    db: Session | None = None,
    projection_contexts: dict[int, dict] | None = None,
) -> dict:
    contexts = projection_contexts if projection_contexts is not None else (
        load_warehouse_location_projection_contexts(
            db,
            [item.location for item in order.items if item.location is not None],
        )
        if db is not None
        else {}
    )
    return {
        "id": order.id,
        "order_number": order.order_number,
        "supplier_name": order.supplier_name,
        "customer_id": order.customer_id,
        "customer_name": order.customer.name if order.customer else None,
        "source_type": order.source_type,
        "status": order.status,
        "remark": order.remark,
        "created_at": utc_naive_to_api(order.created_at) if order.created_at else None,
        "confirmed_at": (
            utc_naive_to_api(order.confirmed_at) if order.confirmed_at else None
        ),
        "stocked_at": utc_naive_to_api(order.stocked_at) if order.stocked_at else None,
        "total_quantity": sum(item.quantity for item in order.items),
        "stocked_quantity": sum(receipt_progress(db, item)['received_quantity'] if db is not None else item.stocked_quantity for item in order.items),
        "items": [
            replenishment_item_dict(
                item,
                db=db,
                projection_context=(
                    contexts.get(int(item.location.id))
                    if item.location is not None
                    else None
                ),
            )
            for item in order.items
        ],
    }


def receive_replenishment_item(
    db: Session,
    *,
    order: StockReplenishmentOrder,
    item: StockReplenishmentOrderItem,
    quantity: int,
    operator_id: int | None,
    receipt_item_id: int,
    source_ref_type: str = "stock_replenishment_receipt",
    actual_inventory_quantity: int | None = None,
) -> InventoryLot:
    """Put one actually received replenishment line into inventory.

    Saving the replenishment document never calls this function.  The incoming
    receipt workflow calls it only after the operator confirms an actual
    receipt, so the inventory movement and the receipt fact share one
    idempotent source. ``quantity`` advances the frozen replenishment plan;
    ``actual_inventory_quantity`` may be higher for an audited supplier
    over-receipt while the original plan remains unchanged.
    """
    if order.status == "voided":
        raise StockReplenishmentError("已作废补库单不能收货。", 409)
    remaining = int(item.quantity or 0) - int(item.stocked_quantity or 0)
    inventory_quantity = (
        quantity
        if actual_inventory_quantity is None
        else int(actual_inventory_quantity)
    )
    if inventory_quantity <= 0 or quantity < 0:
        raise StockReplenishmentError("本次实收数量必须大于0。")
    if inventory_quantity < quantity:
        raise StockReplenishmentError("实际入库数量不能小于计划到货数量。")
    if quantity > remaining:
        raise StockReplenishmentError(
            "本次实收超过补库单剩余待收数量；请核对后另建补库单处理超收。",
            409,
        )
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.services.supplier_receipt_price_facts import (
        SupplierReceiptPriceFactError,
        assert_receipt_settlement_price,
        stock_replenishment_uses_paperboard_price,
    )

    if stock_replenishment_uses_paperboard_price(db, item):
        receipt_item = db.get(IncomingReceiptItem, receipt_item_id)
        if (
            source_ref_type != "stock_replenishment_receipt"
            or receipt_item is None
            or receipt_item.stock_replenishment_item_id != item.id
            or receipt_item.received_quantity != inventory_quantity
        ):
            raise StockReplenishmentError(
                "纸板补库必须从来料实收入口冻结供应商结算价后入库，不能直接入库。", 409
            )
        try:
            assert_receipt_settlement_price(db, receipt_item=receipt_item)
        except SupplierReceiptPriceFactError as error:
            raise StockReplenishmentError(str(error), error.status_code) from error
    finished_product = (
        db.get(Product, item.product_id)
        if item.target_inventory_type == "finished" and item.product_id is not None
        else None
    )
    has_external_purchase_source = bool(
        db.scalar(
            select(ExternalPackagingPurchaseItem.id)
            .where(
                ExternalPackagingPurchaseItem.stock_replenishment_item_id == item.id
            )
            .limit(1)
        )
    )
    external_finished = bool(
        item.target_inventory_type == "finished"
        and (
            item.procurement_route_snapshot == "external_packaging"
            or has_external_purchase_source
        )
    )
    paperboard_finished = bool(
        item.target_inventory_type == "finished"
        and not external_finished
        and (
            item.procurement_route_snapshot == "paperboard"
            or (
                item.procurement_route_snapshot is None
                and finished_product is not None
                and box_type_code(finished_product.box_style) == "liner"
            )
        )
    )
    if (
        order.source_type == "stock_warning"
        and item.target_inventory_type != "semi_finished"
    ):
        if finished_product is None or not (
            paperboard_finished or external_finished
        ):
            raise StockReplenishmentError(
                "库存预警到料只能进入客户通用纸板备料；"
                "只有衬板或正式外购包材可按直接成品入库。"
            )
    if item.target_inventory_type == "semi_finished":
        try:
            destination = automatic_raw_material_staging_location(
                db,
                repair_operator_id=operator_id,
                repair_idempotency_key=(
                    f"stock-replenishment-receipt:{int(receipt_item_id)}"
                ),
                require_floor3_left=True,
            )
        except WarehouseInventoryError as error:
            raise StockReplenishmentError(str(error), error.status_code) from error
    else:
        target = None
        from app.services.receipt_putaway import resolve
        from app.services.fixed_shelf import ShelfError
        try:
            receipt_target = resolve(db, product_id=finished_product.id if finished_product else None,
                customer_id=item.customer_id or (finished_product.customer_id if finished_product else None), claim=True)
        except ShelfError as exc:
            raise StockReplenishmentError(str(exc), 409) from exc
        if receipt_target is not None:
            destination = receipt_target[0]
        elif paperboard_finished:
            try:
                destination = automatic_floor3_finished_turnover_location(db)
            except WarehouseInventoryError as error:
                raise StockReplenishmentError(str(error), error.status_code) from error
        elif external_finished and finished_product is not None:
            try:
                from app.services.production_workflow import (
                    ProductionWorkflowError,
                    _production_direct_finished_target,
                )

                destination, target = _production_direct_finished_target(
                    db,
                    customer_id=(item.customer_id or finished_product.customer_id),
                )
            except ProductionWorkflowError as error:
                raise StockReplenishmentError(
                    str(error), error.status_code
                ) from error
        else:
            # Preserve receivability of historical non-liner finished rows only
            # through the explicit destination that was frozen on the row.
            destination = (
                db.get(WarehouseLocation, item.location_id)
                if item.location_id
                else None
            )
        if destination is None or not destination.is_active:
            raise StockReplenishmentError("历史成品补库明细缺少可用入库库位。", 409)
        if destination.source_version == "V11":
            raise StockReplenishmentError(
                "V11 三楼 Phase A 货位不能用于正式库存补库。", 409
            )
        location_issue = operational_location_issue(
            db,
            destination,
            warehouse_types={"finished", "shared"},
        )
        if location_issue:
            raise StockReplenishmentError(
                f"历史成品补库库位不可使用：{location_issue}", 409
            )
    destination_location_id = destination.id

    customer_board_preparation = (
        item.target_inventory_type == "semi_finished"
        and item.customer_id is not None
    )
    customer_generic_preparation = (
        customer_board_preparation
        and (item.reference_product_id is not None or item.product_id is None)
    )
    common = {
        "location_id": destination_location_id,
        "quantity": inventory_quantity,
        "stock_date": beijing_today(),
        "source_type": "replenishment",
        "remarks": (
            f"补库单 {order.order_number}；"
            f"{'客户通用纸板备料；' if customer_generic_preparation else '客户专用纸板备料；' if customer_board_preparation else ''}"
            f"{item.remark or ''}"
        ).strip("；"),
        "operator_id": operator_id,
        "idempotency_key": (
            f"incoming-replenishment-item-{receipt_item_id}"
            if source_ref_type == "stock_replenishment_receipt"
            else (
                f"stock-replenishment-item-{item.id}"
                if source_ref_type == "stock_replenishment_item"
                else f"{source_ref_type}-{receipt_item_id}"
            )
        ),
        "source_ref_type": source_ref_type,
        "source_ref_id": receipt_item_id,
        "expected_layout_version": (
            int(target.layout_version)
            if item.target_inventory_type == "finished"
            and external_finished
            and target is not None
            else (
                int(destination.floor3_layout.version)
                if destination.floor3_layout is not None
                else None
            )
        ),
    }
    try:
        if item.target_inventory_type == "finished":
            product = db.get(Product, item.product_id) if item.product_id else None
            if product is None:
                raise StockReplenishmentError("成品补库明细缺少有效产品。")
            lot = manual_finished_in(
                db,
                customer_id=item.customer_id or product.customer_id,
                product_id=product.id,
                movement_reason=("待归位" if receipt_target and receipt_target[1]=="receipt_staging"
                    else "自动入位" if receipt_target else "补库收料入库"),
                **common,
            )
        elif item.target_inventory_type == "semi_finished":
            required = (
                item.material_code_snapshot,
                item.layer_count,
                item.flute_type,
                item.report_length_mm,
                item.report_width_mm,
            )
            if not all(value not in (None, "") for value in required):
                raise StockReplenishmentError(
                    "半成品补库明细缺少材质、层数、楞型或报料长宽。"
                )
            lot = manual_semi_finished_in(
                db,
                material_code=item.material_code_snapshot or "",
                layer_count=int(item.layer_count or 0),
                flute_type=item.flute_type or "",
                board_length_mm=int(item.report_length_mm or 0),
                board_width_mm=int(item.report_width_mm or 0),
                sheet_type=item.sheet_type,
                component_type=item.component_type,
                pieces_per_box=item.pieces_per_box,
                stock_yield_per_sheet=item.stock_yield_per_sheet,
                supplier_name=order.supplier_name,
                customer_id=item.customer_id,
                material_id=item.material_id,
                crease_type=item.crease_type,
                crease_left_mm=item.crease_left_mm,
                crease_middle_mm=item.crease_middle_mm,
                crease_right_mm=item.crease_right_mm,
                cutting_note=item.remark,
                movement_reason=(
                    "库存预警到料转客户通用纸板备料"
                    if customer_board_preparation
                    else "补库来料转半成品库存"
                ),
                allow_raw_material_staging=True,
                customer_generic_eligible=customer_generic_preparation,
                internal_name=item.internal_name,
                **common,
            )
            if customer_board_preparation and not customer_generic_preparation:
                allowed_product_ids = compatible_customer_product_ids(db, item)
                if not allowed_product_ids:
                    raise StockReplenishmentError(
                        "客户专用纸板备料没有可绑定的同规格成品款号。"
                    )
                lot = replace_semi_finished_lot_allowed_products(
                    db,
                    inventory_lot_id=lot.id,
                    product_ids=allowed_product_ids,
                    expected_version=lot.version,
                    operator_id=operator_id,
                )
        else:
            raise StockReplenishmentError("补库目标类型无效。")
    except WarehouseInventoryError as error:
        raise StockReplenishmentError(str(error), error.status_code) from error

    now = utc_now_naive()
    item.inventory_lot_id = lot.id
    item.inventory_lot = lot
    item.stocked_quantity = int(item.stocked_quantity or 0) + quantity
    item.stocked_at = now
    remaining_items = [
        row for row in order.items if int(row.stocked_quantity or 0) < int(row.quantity or 0)
    ]
    order.status = "partially_stocked" if remaining_items else "stocked"
    order.stocked_by = operator_id
    order.stocked_at = now if not remaining_items else None
    if order.confirmed_at is None:
        order.confirmed_at = now
        order.confirmed_by = operator_id
    db.flush()
    return lot


def stock_replenishment_order(
    db: Session,
    *,
    order: StockReplenishmentOrder,
    operator_id: int | None,
) -> StockReplenishmentOrder:
    if order.status == "voided":
        raise StockReplenishmentError("已作废补库单不能入库。", 409)
    if order.status == "stocked":
        return order

    for item in order.items:
        if item.stocked_quantity >= item.quantity or receipt_progress(db, item)['short_closed']:
            continue
        quantity = item.quantity - item.stocked_quantity
        receive_replenishment_item(
            db,
            order=order,
            item=item,
            quantity=quantity,
            operator_id=operator_id,
            receipt_item_id=item.id,
            source_ref_type="stock_replenishment_item",
        )
    db.flush()
    return order
