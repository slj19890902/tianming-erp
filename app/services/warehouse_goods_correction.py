"""Split remaining physical stock for a metadata correction, without moving it."""
import hashlib
from sqlalchemy import select, update
from app.core.time_contract import utc_now_naive
from app.models.warehouse_inventory import (
    InventoryLot, InventoryLotTransfer, InventoryPallet, InventoryPalletItem,
    FinishedGoodsInventoryDetail, SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct, Floor3LocationLayout,
)
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.services import warehouse_inventory as inv
from app.services.warehouse_sheet_transfer import _copy_columns


def split_for_correction(db, lot, quantity, user, key):
    if lot.status != "active" or lot.quantity_reserved or not 0 < quantity <= lot.quantity_available:
        raise inv.WarehouseInventoryError("只可修正正常且未预占的剩余库存", 409)
    layout = db.scalar(select(Floor3LocationLayout).where(
        Floor3LocationLayout.location_id == lot.warehouse_location_id))
    inv._claim_inventory_destination(db, lot.warehouse_location_id,
        expected_layout_version=layout.version if layout else None)
    pallet_item = lot.pallet_item
    if pallet_item:
        pallet = pallet_item.pallet
        if not pallet or not pallet.is_current or pallet.location_id != lot.warehouse_location_id:
            raise inv.WarehouseInventoryError("栈板与批次位置不一致，请先核对", 409)
        physical = lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged
        if pallet_item.quantity != physical:
            raise inv.WarehouseInventoryError("栈板数量与库存不一致，请先核对", 409)
        changed = db.execute(update(InventoryPallet).where(
            InventoryPallet.id == pallet.id, InventoryPallet.version == pallet.version
        ).values(version=InventoryPallet.version + 1, updated_by=user.id))
        if changed.rowcount != 1:
            raise inv.WarehouseInventoryError("栈板已变化，请刷新", 409)
    before = inv._balances(lot)
    target = InventoryLot(**_copy_columns(lot, {
        "id", "lot_number", "quantity_available", "quantity_reserved", "quantity_consumed",
        "quantity_damaged", "quantity_scrapped", "version", "created_at", "updated_at",
        "created_by", "last_movement_at", "source_type", "remarks"}),
        lot_number=inv._number("FC" if lot.finished_detail else "SC"),
        quantity_available=quantity, quantity_reserved=0, source_type="transfer",
        created_by=user.id, last_movement_at=utc_now_naive(), remarks=f"由批次 {lot.lot_number} 原位资料修正拆分")
    if lot.finished_detail:
        target.finished_detail = FinishedGoodsInventoryDetail(**_copy_columns(
            lot.finished_detail, {"inventory_lot_id"}))
    else:
        target.semi_finished_detail = SemiFinishedInventoryDetail(**_copy_columns(
            lot.semi_finished_detail, {"inventory_lot_id"}))
    db.add(target)
    db.flush()
    for binding in lot.allowed_products:
        db.add(SemiFinishedLotAllowedProduct(inventory_lot_id=target.id,
            **_copy_columns(binding, {"id", "inventory_lot_id"})))
    profile = db.get(WarehouseGoodsProfile, lot.id)
    if profile:
        db.add(WarehouseGoodsProfile(lot_id=target.id, data_json=profile.data_json))
    version = lot.version
    lot.quantity_available -= quantity
    lot.version += 1
    lot.last_movement_at = utc_now_naive()
    if pallet_item:
        # A spent lot retains its historical pallet row; live quantity always
        # comes from InventoryLot, as in the existing finished-lot edit flow.
        pallet_item.quantity = max(lot.quantity_available + lot.quantity_damaged, 1)
        db.add(InventoryPalletItem(**_copy_columns(pallet_item, {
            "id", "inventory_lot_id", "quantity", "created_at", "updated_at", "created_by"}),
            inventory_lot_id=target.id, quantity=quantity, created_by=user.id))
    db.add(InventoryLotTransfer(source_lot_id=lot.id, target_lot_id=target.id,
        source_location_id=lot.warehouse_location_id, target_location_id=lot.warehouse_location_id,
        quantity=quantity, available_quantity=quantity, reserved_quantity=0,
        source_version_before=version, source_version_after=lot.version,
        idempotency_key="correction:" + key, transferred_by=user.id,
        request_hash=hashlib.sha256(f"{lot.id}:{version}:{quantity}:{key}".encode()).hexdigest(),
        transferred_at=utc_now_naive()))
    inv._movement(db, lot=lot, movement_type="location_transfer", quantity=quantity,
        before=before, operator_id=user.id, reason="资料修正原位拆分",
        idempotency_key=inv._transfer_key("goods-correction", key, "source"))
    inv._movement(db, lot=target, movement_type="location_transfer", quantity=quantity,
        before={name: 0 for name in before}, operator_id=user.id, reason="资料修正原位拆分",
        idempotency_key=inv._transfer_key("goods-correction", key, "target"))
    db.flush()
    return target
