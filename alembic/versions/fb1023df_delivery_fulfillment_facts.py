"""Append-only actual delivery facts; preserve original invoices and stock.

Revision guards are frozen here, not imported from mutable application models.
No business backfill. Populated or partial new structures cannot be downgraded.
"""
from alembic import op
import sqlalchemy as sa

revision = "fb1023df"
down_revision = "fa1023oc"
branch_labels = None
depends_on = None

TABLES = ("delivery_fulfillment_revisions", "delivery_fulfillment_segments")
GUARDS = (
    "delivery_fulfillment_revisions_update_immutable", "delivery_fulfillment_revisions_delete_immutable",
    "delivery_fulfillment_segments_update_immutable", "delivery_fulfillment_segments_delete_immutable",
    "delivery_fulfillment_segment_unsealed", "delivery_fulfillment_revision_seal",
    "delivery_fulfillment_original_delivery_guard", "delivery_fulfillment_original_delivery_delete",
    "delivery_fulfillment_original_item_insert", "delivery_fulfillment_original_item_update", "delivery_fulfillment_original_item_delete",
)


def _transaction_connection():
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        raise RuntimeError("Actual delivery fact migration requires SQLite")
    # SQLite's legacy driver does not begin a transaction for DDL. Explicitly
    # hold the writer lock so an error cannot leave only half of the schema.
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
        raise RuntimeError("Actual delivery migration requires foreign keys enabled")
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    return connection


def upgrade():
    connection = _transaction_connection()
    op.create_table(TABLES[0],
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("delivery_id", sa.Integer(), sa.ForeignKey("sales_deliveries.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("previous_revision_id", sa.Integer(), sa.ForeignKey("delivery_fulfillment_revisions.id", ondelete="RESTRICT")),
        sa.Column("operation_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("reviewed_hash", sa.String(64), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("delivery_id", "revision", name="uq_delivery_fulfillment_revision"),
        sa.UniqueConstraint("operation_key", name="uq_delivery_fulfillment_operation"),
        sa.CheckConstraint("revision > 0", name="ck_delivery_fulfillment_revision_positive"),
        sa.CheckConstraint("length(request_hash) = 64 AND length(reviewed_hash) = 64", name="ck_delivery_fulfillment_hashes"),
        sa.CheckConstraint("length(trim(reason)) > 0 AND length(trim(operation_key)) > 0", name="ck_delivery_fulfillment_reason_key"))
    op.create_index("ix_delivery_fulfillment_revisions_delivery_id", TABLES[0], ["delivery_id"])
    op.create_table(TABLES[1],
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("revision_id", sa.Integer(), sa.ForeignKey("delivery_fulfillment_revisions.id", ondelete="RESTRICT", deferrable=True, initially="DEFERRED"), nullable=False),
        sa.Column("delivery_item_id", sa.Integer(), sa.ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actual_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("revision_id", "delivery_item_id", "actual_date", name="uq_delivery_fulfillment_segment"),
        sa.CheckConstraint("quantity > 0", name="ck_delivery_fulfillment_segment_quantity"))
    op.create_index("ix_delivery_fulfillment_segments_revision_id", TABLES[1], ["revision_id"])
    op.create_index("ix_delivery_fulfillment_segments_delivery_item_id", TABLES[1], ["delivery_item_id"])
    op.create_index("ix_delivery_fulfillment_actual_date_item", TABLES[1], ["actual_date", "delivery_item_id"])
    _install_frozen_guards(connection)


def downgrade():
    connection = _transaction_connection()
    # Inspect both tables before the first DDL, including orphan/unsealed rows.
    # Missing tables/unknown structures raise and remain available for diagnosis.
    for table in TABLES:
        if connection.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first():
            raise RuntimeError("Existing actual delivery facts: refuse lossy downgrade; preserve database and repair forward")
    for guard in GUARDS:
        connection.exec_driver_sql(f"DROP TRIGGER {guard}")
    op.drop_table(TABLES[1])
    op.drop_table(TABLES[0])


def _install_frozen_guards(connection) -> None:
    """Frozen fb1023df DDL; independent of future application model changes."""
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
