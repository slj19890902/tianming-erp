"""Current finished stock reference for planning; never reserves or dispatches."""
import hashlib
import json
from sqlalchemy import select
from app.models.order import OrderItem
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail, WarehouseLocation
from app.services.finished_stock_identity import product_basis, order_product_basis, matches_lot_stock_identity
from app.services.bom_inventory_contract import is_body_lot
from app.services.order_parent_stock import parent_inventory_eligible, physical_bom_parent
from app.services.warehouse_inventory import _frozen_product_has_stock
from app.services.customer_document_fields import decode_snapshot
from app.services.delivery_quantities import order_basis, require_physical_stock, available_customer_quantity, QuantityContractError


def enrich_stock_preview(db, batch, payload):
    cache = {}
    for row in payload['items']:
        row.update(stock_on_hand_qty=None, stock_locations=[], stock_pool_key=None, customer_drawing_number='')
        product = db.get(Product, row.get('product_id')) if row.get('product_id') else None
        if product is None or product.customer_id != batch.customer_id:
            continue
        item = db.get(OrderItem, row.get('order_item_id')) if row.get('order_item_id') else None
        if item is not None and (item.product_id != product.id or item.order.customer_id != batch.customer_id):
            continue
        # Preserve a captured drawing (including an explicitly blank one). NULL means not maintained.
        try:
            document = decode_snapshot(item.customer_document_snapshot_json) if item else None
            frozen_drawing = document.get('customer_drawing_number') if document is not None else None
            row['customer_drawing_number'] = frozen_drawing if frozen_drawing is not None else (product.customer_drawing_number or '')
        except ValueError:
            row['customer_drawing_number'] = ''
        expected = order_product_basis(db, item.id, product.id) if item else None
        # Ordinary historical orders retain the same identity contract as the
        # reservation service. Today's renamed mold description must not hide
        # already reserved physical stock. BOM sets still require assembly proof.
        if expected is None and (item is None or physical_bom_parent(db, product)):
            expected = product_basis(product)
        quantity_basis = None
        if item and item.supply_mode_snapshot == 'external_purchase':
            try:
                quantity_basis = order_basis(item, batch.customer_id)
            except QuantityContractError:
                continue  # Unknown units are not zero stock or a guessed conversion.
        ratio = (quantity_basis['customer_basis'], quantity_basis['physical_basis']) if quantity_basis else (1, 1)
        key = hashlib.sha256(json.dumps([batch.customer_id, product.id, json.loads(expected) if expected else None, ratio], sort_keys=True).encode()).hexdigest()
        row['stock_pool_key'] = key
        if key not in cache:
            locations, total = [], 0
            eligible = _frozen_product_has_stock(db, item.id, product.id) if item else parent_inventory_eligible(product, db=db)
            if eligible:
                lots = db.execute(select(InventoryLot, WarehouseLocation)
                    .join(FinishedGoodsInventoryDetail, FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id)
                    .outerjoin(WarehouseLocation, WarehouseLocation.id == InventoryLot.warehouse_location_id)
                    .where(InventoryLot.inventory_type == 'finished', InventoryLot.status == 'active',
                        InventoryLot.quantity_available + InventoryLot.quantity_reserved > 0,
                        FinishedGoodsInventoryDetail.product_id == product.id,
                        FinishedGoodsInventoryDetail.owner_customer_id == batch.customer_id,
                        FinishedGoodsInventoryDetail.is_general.is_(False))
                    .order_by(InventoryLot.stock_date, InventoryLot.id)).all()
                for lot, location in lots:
                    if is_body_lot(lot) or not matches_lot_stock_identity(lot, expected):
                        continue
                    if not quantity_basis and expected is None:
                        try:
                            captured = json.loads(lot.finished_detail.physical_basis_json or '{}')
                            unit = (item.sales_unit_snapshot if item else None) or product.unit
                            if captured.get('unit') and unit and captured['unit'] != unit:
                                continue
                        except (ValueError, AttributeError):
                            continue
                    quantity = lot.quantity_available + lot.quantity_reserved
                    if quantity_basis:
                        try:
                            require_physical_stock(lot, quantity_basis)
                            quantity = available_customer_quantity(quantity_basis, quantity)
                        except QuantityContractError:
                            continue
                    total += quantity
                    if quantity and location:
                        locations.append(dict(location_id=location.id, lot_id=lot.id,
                            location_code=location.location_code,
                            location_name=location.location_name or location.location_code,
                            warehouse_floor=location.warehouse_floor, quantity=quantity))
            cache[key] = (total, locations)
        row['stock_on_hand_qty'], row['stock_locations'] = cache[key]
    return payload
