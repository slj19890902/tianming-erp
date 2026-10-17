"""Owner-selected invoice file labels; frozen amounts and task rows stay intact."""
from sqlalchemy import select

from app.models.delivery import DeliveryItem
from app.models.finance import StatementItem, ReturnReceiptItem
from app.models.order import OrderItem
from app.services.customer_document_fields import decode_snapshot
from app.services.invoice_tax_template import InvoiceTaxTemplateError

POLICY = "code-drawing-v1"
EXPORT_FIELDS = ("project_name", "tax_classification_code", "specification", "unit",
                 "quantity", "unit_price", "amount", "tax_rate")


def drawing_number(delivery_snapshot, order_snapshot):
    """An explicit blank is authoritative; dimensions are never a drawing."""
    for raw in (delivery_snapshot, order_snapshot):
        if raw is None:
            continue
        try:
            value = decode_snapshot(raw)
        except ValueError as error:
            raise InvoiceTaxTemplateError("开票来源的客户图号快照损坏，请核对原单据") from error
        if value is not None and "customer_drawing_number" in value:
            drawing = value["customer_drawing_number"]
            if drawing is None:
                return None
            return drawing.strip() or None
    return None


def export_lines(db, items):
    ids = {item.statement_item_id for item in items
           if item.source_type == "delivery" and item.statement_item_id is not None}
    snapshots = {}
    if ids:
        rows = db.execute(select(
            StatementItem.id, DeliveryItem.customer_document_snapshot_json,
            OrderItem.customer_document_snapshot_json,
        ).join(ReturnReceiptItem, ReturnReceiptItem.id == StatementItem.return_receipt_item_id)
          .join(DeliveryItem, DeliveryItem.id == ReturnReceiptItem.delivery_item_id)
          .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
          .where(StatementItem.id.in_(ids)))
        snapshots = {row[0]: (row[1], row[2]) for row in rows}
    result = []
    for item in items:
        line = {field: getattr(item, field) for field in EXPORT_FIELDS}
        if item.source_type == "delivery":
            code = (item.product_code_snapshot or "").strip()
            if not code:
                raise InvoiceTaxTemplateError(f"开票明细 {item.sequence_no} 缺少存货编码，不能生成项目名称")
            line["project_name"] = code
            line["specification"] = drawing_number(*snapshots.get(item.statement_item_id, (None, None)))
        result.append(line)
    return result
