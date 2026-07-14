"""Add N005 incoming receipt facts and quantity variance decisions.

Revision ID: an41v7w8x9j31
Revises: aj37v7w8x9f27
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "an41v7w8x9j31"
down_revision = "aj37v7w8x9f27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incoming_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("receipt_number", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="posted"),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column(
            "received_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("remarks", sa.Text()),
        sa.Column("reversed_at", sa.DateTime()),
        sa.Column(
            "reversed_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("reversal_reason", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "status IN ('posted','reversed')",
            name="ck_incoming_receipts_status",
        ),
        sa.UniqueConstraint(
            "receipt_number",
            name="uq_incoming_receipts_number",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_incoming_receipts_idempotency",
        ),
    )
    op.create_index(
        "ix_incoming_receipts_received_at",
        "incoming_receipts",
        ["received_at"],
    )
    op.create_index(
        "ix_incoming_receipts_status",
        "incoming_receipts",
        ["status"],
    )

    op.create_table(
        "incoming_receipt_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "receipt_id",
            sa.Integer(),
            sa.ForeignKey("incoming_receipts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("sales_orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "order_item_id",
            sa.Integer(),
            sa.ForeignKey("sales_order_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requisition_id",
            sa.Integer(),
            sa.ForeignKey("material_requisitions.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "requisition_item_id",
            sa.Integer(),
            sa.ForeignKey("material_requisition_items.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "supplier_order_id",
            sa.Integer(),
            sa.ForeignKey("supplier_requisition_orders.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "supplier_order_item_id",
            sa.Integer(),
            sa.ForeignKey(
                "supplier_requisition_order_items.id",
                ondelete="SET NULL",
            ),
        ),
        sa.Column("planned_quantity", sa.Integer(), nullable=False),
        sa.Column("received_quantity", sa.Integer(), nullable=False),
        sa.Column("cumulative_received_quantity", sa.Integer(), nullable=False),
        sa.Column("variance_quantity", sa.Integer(), nullable=False),
        sa.Column("variance_type", sa.String(20), nullable=False),
        sa.Column("resolution_status", sa.String(20), nullable=False),
        sa.Column("resolution_action", sa.String(40)),
        sa.Column("resolution_reason", sa.Text()),
        sa.Column(
            "resolved_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("resolved_at", sa.DateTime()),
        sa.Column(
            "surplus_inventory_lot_id",
            sa.Integer(),
            sa.ForeignKey("inventory_lots.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="posted"),
        sa.Column("reversal_reason", sa.Text()),
        sa.Column(
            "reversed_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("reversed_at", sa.DateTime()),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "planned_quantity > 0",
            name="ck_incoming_receipt_items_planned",
        ),
        sa.CheckConstraint(
            "received_quantity > 0",
            name="ck_incoming_receipt_items_received",
        ),
        sa.CheckConstraint(
            "cumulative_received_quantity > 0",
            name="ck_incoming_receipt_items_cumulative",
        ),
        sa.CheckConstraint(
            "variance_type IN ('matched','short','over')",
            name="ck_incoming_receipt_items_variance_type",
        ),
        sa.CheckConstraint(
            "resolution_status IN ('not_required','pending','resolved')",
            name="ck_incoming_receipt_items_resolution_status",
        ),
        sa.CheckConstraint(
            "resolution_action IS NULL OR resolution_action IN "
            "('await_supplier','accept_short','all_to_production',"
            "'transfer_to_semi_inventory')",
            name="ck_incoming_receipt_items_resolution_action",
        ),
        sa.CheckConstraint(
            "status IN ('posted','reversed')",
            name="ck_incoming_receipt_items_status",
        ),
    )
    op.create_index(
        "ix_incoming_receipt_items_order_item",
        "incoming_receipt_items",
        ["order_item_id", "status"],
    )
    op.create_index(
        "ix_incoming_receipt_items_requisition_item",
        "incoming_receipt_items",
        ["requisition_item_id", "status"],
    )
    op.create_index(
        "ix_incoming_receipt_items_receipt",
        "incoming_receipt_items",
        ["receipt_id"],
    )
    op.create_index(
        "ix_incoming_receipt_items_surplus_lot",
        "incoming_receipt_items",
        ["surplus_inventory_lot_id"],
    )

    # SQLite can add this nullable column in place.  Do not use batch/recreate:
    # finance_statement_items may already reference this table, and rebuilding
    # it is unsafe when PRAGMA foreign_keys is enabled.
    op.add_column(
        "finance_return_receipt_items",
        sa.Column("resolution_action", sa.String(30), nullable=True),
    )
    op.execute(
        """
        CREATE TRIGGER trg_finance_receipt_resolution_action_insert
        BEFORE INSERT ON finance_return_receipt_items
        FOR EACH ROW
        WHEN NEW.resolution_action IS NOT NULL
         AND NEW.resolution_action NOT IN
             ('continue_delivery','accept_short','accept_over')
        BEGIN
            SELECT RAISE(ABORT, 'invalid finance receipt resolution_action');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_finance_receipt_resolution_action_update
        BEFORE UPDATE OF resolution_action ON finance_return_receipt_items
        FOR EACH ROW
        WHEN NEW.resolution_action IS NOT NULL
         AND NEW.resolution_action NOT IN
             ('continue_delivery','accept_short','accept_over')
        BEGIN
            SELECT RAISE(ABORT, 'invalid finance receipt resolution_action');
        END
        """
    )


def downgrade() -> None:
    connection = op.get_bind()
    losses = connection.execute(
        sa.text(
            """
            SELECT
                (SELECT COUNT(*) FROM incoming_receipts) AS receipts,
                (SELECT COUNT(*) FROM incoming_receipt_items) AS receipt_items,
                (SELECT COUNT(*)
                   FROM finance_return_receipt_items
                  WHERE resolution_action IS NOT NULL) AS finance_decisions
            """
        )
    ).mappings().one()
    if any(int(losses[key] or 0) for key in losses.keys()):
        raise RuntimeError(
            "N005 已产生来料实收或回单差异决策，禁止破坏性降级；"
            "请停止服务并恢复升级前的完整 SQLite 备份。"
        )

    op.execute("DROP TRIGGER IF EXISTS trg_finance_receipt_resolution_action_update")
    op.execute("DROP TRIGGER IF EXISTS trg_finance_receipt_resolution_action_insert")
    op.execute(
        "ALTER TABLE finance_return_receipt_items DROP COLUMN resolution_action"
    )

    op.drop_table("incoming_receipt_items")
    op.drop_table("incoming_receipts")
