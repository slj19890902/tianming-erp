"""One effective-receipt predicate shared by stock, procurement and finance."""
from sqlalchemy import exists, select
from app.models.external_packaging_purchase import ExternalPackagingReceiptItem, ExternalPackagingReceiptReversal


def active_receipt_item():
    return ~exists(select(ExternalPackagingReceiptReversal.receipt_id).where(
        ExternalPackagingReceiptReversal.receipt_id == ExternalPackagingReceiptItem.receipt_id
    ).correlate(ExternalPackagingReceiptItem))
