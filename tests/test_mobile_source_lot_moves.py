"""A purchase source is traceable after a split, without granting sheet usage."""
from datetime import date, datetime

from sqlalchemy import select

from app.models.product import Product
from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.warehouse_inventory import (
    InventoryLot, InventoryLotTransfer, SemiFinishedInventoryDetail, WarehouseLocation,
)
from app.services.product_activity import source_lot_descendant_ids, source_lots
from test_p1_21b_mobile_admin_product_search import mobile_erp_app


def test_stock_source_follows_only_verified_same_customer_transfer(mobile_erp_app):
    _app, ids, factory = mobile_erp_app
    now = datetime(2026, 10, 10)
    with factory() as db:
        product = db.get(Product, ids['product'])
        other = db.get(Product, ids['other_product'])
        location = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == 'SF-TEMP'))
        order = StockReplenishmentOrder(order_number='CBW-LOT-MOVE', customer_id=product.customer_id,
                                        source_type='stock_warning', status='stocked')
        db.add(order)
        db.flush()
        item = StockReplenishmentOrderItem(replenishment_order_id=order.id,
            target_inventory_type='semi_finished', customer_id=product.customer_id,
            reference_product_id=product.id, product_code_snapshot=product.product_code,
            product_name_snapshot=product.product_name, material_code_snapshot='K=A',
            layer_count=3, flute_type='B', report_length_mm=800, report_width_mm=600,
            quantity=30, stocked_quantity=30)
        db.add(item)
        db.flush()
        receipt = IncomingReceipt(receipt_number='IR-LOT-MOVE', status='posted',
            received_at=now, idempotency_key='receipt-lot-move')
        db.add(receipt)
        db.flush()
        line = IncomingReceiptItem(receipt_id=receipt.id, stock_replenishment_item_id=item.id,
            planned_quantity=30, received_quantity=30, cumulative_received_quantity=30,
            variance_quantity=0, variance_type='matched', resolution_status='not_required',
            status='posted')
        db.add(line)
        db.flush()

        def lot(number, owner, *, ref_id=line.id, available=4, status='active'):
            row = InventoryLot(lot_number=number, inventory_type='semi_finished',
                warehouse_location_id=location.id, quantity_available=available,
                unit='sheets', status=status, source_type='transfer',
                source_ref_type='stock_replenishment_receipt', source_ref_id=ref_id,
                stock_date=date(2026, 10, 10), last_movement_at=now)
            row.semi_finished_detail = SemiFinishedInventoryDetail(
                owner_customer_id=owner, material_code_snapshot='K=A',
                normalized_material_code='K=A', layer_count=3, flute_type='B',
                board_length_mm=800, board_width_mm=600, component_type='whole',
                sheet_type='raw_board')
            db.add(row)
            db.flush()
            return row

        root = lot('SOURCE-ROOT', product.customer_id, available=0, status='closed')
        line.received_inventory_lot_id = root.id
        moved = lot('SOURCE-MOVED', product.customer_id, available=20)
        moved_again = lot('SOURCE-MOVED-AGAIN', product.customer_id, available=10)
        copied_only = lot('SOURCE-COPIED-ONLY', product.customer_id)
        wrong_owner = lot('SOURCE-WRONG-OWNER', other.customer_id)
        wrong_ref = lot('SOURCE-WRONG-REF', product.customer_id, ref_id=line.id + 1000)
        for index, (source, target) in enumerate(((root, moved), (moved, moved_again),
                                                   (root, wrong_owner), (root, wrong_ref)), 1):
            db.add(InventoryLotTransfer(source_lot_id=source.id, target_lot_id=target.id,
                source_location_id=location.id, target_location_id=location.id,
                quantity=30 if target is moved else 10 if target is moved_again else 1,
                available_quantity=30 if target is moved else 10 if target is moved_again else 1,
                reserved_quantity=0,
                source_version_before=index, source_version_after=index + 1,
                idempotency_key=f'source-lot-move-{index}', request_hash='a' * 64,
                transferred_at=now))
        db.commit()
        root_id = root.id
        expected_ids = {moved.id, moved_again.id}
        excluded_ids = {copied_only.id, wrong_owner.id, wrong_ref.id}

    with factory() as db:
        product = db.get(Product, ids['product'])
        found = {lot.id for lot in source_lots(db, product)}
        assert expected_ids <= found
        assert not (found & excluded_ids)
        roots = select(InventoryLot.id).where(InventoryLot.id == root_id)
        descendants = set(db.scalars(source_lot_descendant_ids(roots, product.customer_id)))
        assert expected_ids <= descendants
        assert not (descendants & excluded_ids)
