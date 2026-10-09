"""Reviewed one-lot pilot, called only by the stopped, backed-up release job.

No CLI, web authorization bypass, credentials, fake login or admin impersonation.
The maintenance caller owns the lock, verified backup, exact baseline and SQL
transaction. Scope is the owner's explicit 2026-10-09 confirmation of existing
80012043 stock for the two named customers; never infer other common codes.
"""
import re
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from app.services.shared_finished_stock import preview, _apply_confirmed, _error
from app.models.shared_finished_stock import SharedFinishedGroup
from sqlalchemy import select

PRODUCT_IDS = [3371, 3467]
LOT_IDS = [1036]
EVIDENCE = "2026-10-09老板明确确认：研光、光洋80012043（T1K-08B）的现货两客户都能用。仅本次确认批次，不推断其他编码。"


def prepare(db):
    products = [db.get(Product, pid) for pid in PRODUCT_IDS]
    if not all(p is not None and p.product_code == "80012043" and p.product_name == "T1K-08B" for p in products):
        raise _error("首组产品身份已变化，停止维护确认")
    if [p.customer_id for p in products] != [137, 138]:
        raise _error("首组客户归属已变化，停止维护确认")
    lot = db.get(InventoryLot, LOT_IDS[0])
    if not lot or not lot.finished_detail or lot.finished_detail.product_id != PRODUCT_IDS[0]:
        raise _error("首组原库存身份已变化，停止维护确认")
    return preview(db, product_ids=PRODUCT_IDS, lot_ids=LOT_IDS)


def apply_reviewed(db, *, expected_preview_hash, backup_receipt):
    if (not isinstance(backup_receipt, dict) or backup_receipt.get("verified") is not True
            or re.fullmatch(r"[0-9a-fA-F]{64}", str(backup_receipt.get("sha256", ""))) is None):
        raise _error("缺少本次已验证的停服备份回执")
    previous = db.scalar(select(SharedFinishedGroup).where(
        SharedFinishedGroup.operation_key == "shared-stock-pilot-80012043-20261009"))
    if previous is None:
        value = prepare(db)
        if value["preview_hash"] != expected_preview_hash:
            raise _error("首组产品或库存与本次维护预览不一致")
    return _apply_confirmed(db, product_ids=PRODUCT_IDS, lot_ids=LOT_IDS,
        preview_hash=expected_preview_hash, operation_key="shared-stock-pilot-80012043-20261009",
        evidence=EVIDENCE + " 备份校验：" + backup_receipt["sha256"], actor=None, source="script")
