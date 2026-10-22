"""Append-only physical delivery facts; original accounting documents are retained.

SQLite candidate only. Install guards in a schema migration (or metadata test
creation), never from a request. Segments precede their deferred-FK header; the
header insertion validates and seals the complete revision atomically.
"""
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event, func, inspect
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

FULFILLMENT_GUARD_NAMES = frozenset({
    "delivery_fulfillment_revisions_update_immutable", "delivery_fulfillment_revisions_delete_immutable",
    "delivery_fulfillment_segments_update_immutable", "delivery_fulfillment_segments_delete_immutable",
    "delivery_fulfillment_segment_unsealed", "delivery_fulfillment_revision_seal",
    "delivery_fulfillment_original_delivery_guard", "delivery_fulfillment_original_delivery_delete",
    "delivery_fulfillment_original_item_insert", "delivery_fulfillment_original_item_update", "delivery_fulfillment_original_item_delete",
})


class DeliveryFulfillmentRevision(Base):
    __tablename__ = "delivery_fulfillment_revisions"
    __table_args__ = (
        UniqueConstraint("delivery_id", "revision", name="uq_delivery_fulfillment_revision"),
        UniqueConstraint("operation_key", name="uq_delivery_fulfillment_operation"),
        CheckConstraint("revision > 0", name="ck_delivery_fulfillment_revision_positive"),
        CheckConstraint("length(request_hash) = 64 AND length(reviewed_hash) = 64", name="ck_delivery_fulfillment_hashes"),
        CheckConstraint("length(trim(reason)) > 0 AND length(trim(operation_key)) > 0", name="ck_delivery_fulfillment_reason_key"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("sales_deliveries.id", ondelete="RESTRICT"), nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_revision_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_fulfillment_revisions.id", ondelete="RESTRICT"), nullable=True)
    operation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    before_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)


class DeliveryFulfillmentSegment(Base):
    __tablename__ = "delivery_fulfillment_segments"
    __table_args__ = (
        UniqueConstraint("revision_id", "delivery_item_id", "actual_date", name="uq_delivery_fulfillment_segment"),
        Index("ix_delivery_fulfillment_actual_date_item", "actual_date", "delivery_item_id"),
        CheckConstraint("quantity > 0", name="ck_delivery_fulfillment_segment_quantity"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("delivery_fulfillment_revisions.id", ondelete="RESTRICT", deferrable=True, initially="DEFERRED"), nullable=False, index=True)
    delivery_item_id: Mapped[int] = mapped_column(ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    actual_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)


def install_delivery_fulfillment_guards(connection) -> None:
    """Migration/test helper. No DML, and no invocation from service/API code."""
    if connection.dialect.name != "sqlite":
        raise RuntimeError("Delivery fulfillment guards currently require SQLite")
    for table in ("delivery_fulfillment_revisions", "delivery_fulfillment_segments"):
        for operation in ("UPDATE", "DELETE"):
            connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()}_immutable
                BEFORE {operation} ON {table} BEGIN
                SELECT RAISE(ABORT, 'delivery fulfillment facts are append-only'); END""")
    connection.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS delivery_fulfillment_segment_unsealed
        BEFORE INSERT ON delivery_fulfillment_segments BEGIN
        SELECT CASE WHEN EXISTS(SELECT 1 FROM delivery_fulfillment_revisions WHERE id=NEW.revision_id)
          THEN RAISE(ABORT, 'delivery fulfillment revision is sealed') END;
        SELECT CASE WHEN typeof(NEW.quantity) != 'integer' OR NEW.quantity <= 0 OR NEW.quantity > 2147483647
          OR date(NEW.actual_date) IS NULL OR date(NEW.actual_date) != NEW.actual_date
          OR NEW.actual_date > date('now','+8 hours')
          OR NOT json_valid(NEW.evidence_json)
          THEN RAISE(ABORT, 'invalid delivery fulfillment segment') END;
        END""")
    connection.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS delivery_fulfillment_revision_seal
        BEFORE INSERT ON delivery_fulfillment_revisions BEGIN
        SELECT CASE WHEN NEW.revision != COALESCE((SELECT max(revision) FROM delivery_fulfillment_revisions WHERE delivery_id=NEW.delivery_id),0)+1
          OR NEW.previous_revision_id IS NOT (SELECT id FROM delivery_fulfillment_revisions WHERE delivery_id=NEW.delivery_id ORDER BY revision DESC LIMIT 1)
          THEN RAISE(ABORT, 'delivery fulfillment revision conflict') END;
        SELECT CASE WHEN NOT json_valid(NEW.before_json) OR NOT json_valid(NEW.evidence_json)
          OR NOT EXISTS(SELECT 1 FROM sales_deliveries d WHERE d.id=NEW.delivery_id AND d.customer_id=NEW.customer_id AND d.status='dispatched' AND d.source_mode='order'
            AND d.total_quantity=(SELECT sum(i.delivered_quantity) FROM sales_delivery_items i WHERE i.delivery_id=d.id AND i.is_current=1))
          THEN RAISE(ABORT, 'invalid delivery fulfillment source') END;
        SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM delivery_fulfillment_segments WHERE revision_id=NEW.id)
          OR EXISTS(SELECT 1 FROM delivery_fulfillment_segments s
            JOIN sales_delivery_items i ON i.id=s.delivery_item_id
            JOIN sales_order_items oi ON oi.id=i.order_item_id JOIN sales_orders o ON o.id=oi.order_id
            WHERE s.revision_id=NEW.id AND (i.delivery_id!=NEW.delivery_id OR i.is_current!=1 OR i.source_type!='order'
              OR o.customer_id!=NEW.customer_id OR s.actual_date<o.order_date
              OR oi.supply_mode_snapshot!='corrugated_production' OR oi.combination_role!='standalone'
              OR EXISTS(SELECT 1 FROM sales_order_item_bom_components b WHERE b.sales_order_item_id=oi.id)
              OR EXISTS(SELECT 1 FROM order_subkits b WHERE b.order_item_id=oi.id)))
          OR EXISTS(SELECT 1 FROM delivery_fulfillment_segments s JOIN sales_delivery_items i ON i.id=s.delivery_item_id
            WHERE s.revision_id=NEW.id AND (i.source_type!='order' OR i.order_item_id IS NULL))
          OR EXISTS(SELECT 1 FROM sales_delivery_items i WHERE i.delivery_id=NEW.delivery_id AND i.is_current=1
            AND (i.source_type!='order' OR i.delivered_quantity != COALESCE((SELECT sum(s.quantity) FROM delivery_fulfillment_segments s WHERE s.revision_id=NEW.id AND s.delivery_item_id=i.id),0)
              OR NOT EXISTS(SELECT 1 FROM finance_return_receipt_items ri JOIN finance_return_receipts r ON r.id=ri.return_receipt_id
                JOIN finance_statement_items si ON si.return_receipt_item_id=ri.id JOIN finance_statements st ON st.id=si.statement_id
                WHERE ri.delivery_item_id=i.id AND r.delivery_id=NEW.delivery_id AND r.status='confirmed'
                  AND ri.actual_received_quantity=i.delivered_quantity AND si.actual_received_quantity=i.delivered_quantity
                  AND st.customer_id=NEW.customer_id AND st.confirmation_status='confirmed' AND st.settled_amount=0)))
          THEN RAISE(ABORT, 'delivery fulfillment quantity or chain mismatch') END;
        SELECT CASE WHEN (SELECT count(DISTINCT si.statement_id) FROM finance_statement_items si
            JOIN finance_return_receipt_items ri ON ri.id=si.return_receipt_item_id
            JOIN sales_delivery_items i ON i.id=ri.delivery_item_id WHERE i.delivery_id=NEW.delivery_id AND i.is_current=1)!=1
          OR EXISTS(SELECT 1 FROM finance_statements st WHERE st.id IN (
            SELECT si.statement_id FROM finance_statement_items si
            JOIN finance_return_receipt_items ri ON ri.id=si.return_receipt_item_id
            JOIN sales_delivery_items i ON i.id=ri.delivery_item_id WHERE i.delivery_id=NEW.delivery_id AND i.is_current=1)
            AND (CAST(COALESCE(st.total_receivable_exact, st.total_receivable) AS NUMERIC)<=0
              OR NOT EXISTS(SELECT 1 FROM finance_invoices inv WHERE inv.statement_id=st.id AND inv.invoice_status='issued')
              OR EXISTS(SELECT 1 FROM finance_invoices inv WHERE inv.statement_id=st.id AND inv.invoice_status!='issued')
              OR abs((SELECT sum(inv.invoice_amount) FROM finance_invoices inv WHERE inv.statement_id=st.id AND inv.invoice_status='issued')-CAST(COALESCE(st.total_receivable_exact, st.total_receivable) AS NUMERIC))>0.000001
              OR abs((SELECT sum(inv.invoice_amount) FROM finance_invoices inv WHERE inv.statement_id=st.id AND inv.invoice_status='issued')-st.invoiced_amount)>0.000001
              OR EXISTS(SELECT 1 FROM finance_settlement_records sr WHERE sr.statement_id=st.id)))
          THEN RAISE(ABORT, 'delivery fulfillment requires one fully invoiced statement') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM sales_delivery_items i JOIN sales_order_items oi ON oi.id=i.order_item_id
          WHERE i.delivery_id=NEW.delivery_id AND i.is_current=1 AND CASE WHEN i.quantity_contract_json IS NULL THEN
            NOT COALESCE(json_extract(NEW.evidence_json,'$.quantity_basis_confirmed')=1
              AND length(trim(json_extract(NEW.evidence_json,'$.customer_unit')))>0
              AND json_extract(NEW.evidence_json,'$.customer_unit')=json_extract(NEW.evidence_json,'$.physical_unit')
              AND (oi.sales_unit_snapshot IS NULL OR oi.sales_unit_snapshot=json_extract(NEW.evidence_json,'$.customer_unit'))
              AND (i.unit_snapshot IS NULL OR i.unit_snapshot=json_extract(NEW.evidence_json,'$.customer_unit')),0)
            ELSE NOT COALESCE(json_valid(i.quantity_contract_json)
              AND json_extract(i.quantity_contract_json,'$.schema')=1
              AND json_extract(i.quantity_contract_json,'$.customer_id')=NEW.customer_id
              AND json_extract(i.quantity_contract_json,'$.product_id')=oi.product_id
              AND json_extract(i.quantity_contract_json,'$.customer_basis')=json_extract(i.quantity_contract_json,'$.physical_basis')
              AND json_extract(i.quantity_contract_json,'$.customer_unit')=json_extract(i.quantity_contract_json,'$.physical_unit')
              AND json_extract(i.quantity_contract_json,'$.customer_quantity')=i.delivered_quantity
              AND json_extract(i.quantity_contract_json,'$.physical_quantity')=i.delivered_quantity,0) END)
          THEN RAISE(ABORT, 'delivery fulfillment physical quantity basis is unproven') END;
        END""")
    connection.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS delivery_fulfillment_original_delivery_guard
        BEFORE UPDATE ON sales_deliveries WHEN EXISTS(SELECT 1 FROM delivery_fulfillment_revisions WHERE delivery_id=OLD.id)
          AND (NEW.id IS NOT OLD.id OR NEW.customer_id IS NOT OLD.customer_id OR NEW.delivery_date IS NOT OLD.delivery_date
            OR NEW.status IS NOT OLD.status OR NEW.total_quantity IS NOT OLD.total_quantity OR NEW.source_mode IS NOT OLD.source_mode)
        BEGIN SELECT RAISE(ABORT, 'delivery has immutable fulfillment source'); END""")
    connection.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS delivery_fulfillment_original_delivery_delete
        BEFORE DELETE ON sales_deliveries WHEN EXISTS(SELECT 1 FROM delivery_fulfillment_revisions WHERE delivery_id=OLD.id)
        BEGIN SELECT RAISE(ABORT, 'delivery has immutable fulfillment source'); END""")
    for operation, alias in (("INSERT", "NEW"), ("UPDATE", "OLD"), ("DELETE", "OLD")):
        extra = " OR EXISTS(SELECT 1 FROM delivery_fulfillment_revisions WHERE delivery_id=NEW.delivery_id)" if operation == "UPDATE" else ""
        connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS delivery_fulfillment_original_item_{operation.lower()}
            BEFORE {operation} ON sales_delivery_items WHEN EXISTS(SELECT 1 FROM delivery_fulfillment_revisions WHERE delivery_id={alias}.delivery_id){extra}
            BEGIN SELECT RAISE(ABORT, 'delivery item has immutable fulfillment source'); END""")


@event.listens_for(Base.metadata, "after_create")
def _create_guards(_metadata, connection, **_kwargs):
    # Metadata creation is test support; production must use the reviewed migration.
    if connection.dialect.name == "sqlite" and inspect(connection).has_table("delivery_fulfillment_segments"):
        install_delivery_fulfillment_guards(connection)
