"""P1-33C5 append-only external packaging receipts.

Revision ID: dt02v8x9z91
Revises: ds01v8x9z90
"""

from alembic import op
import sqlalchemy as sa


revision = "dt02v8x9z91"
down_revision = "ds01v8x9z90"
branch_labels = None
depends_on = None


RECEIPTS = "external_packaging_receipts"
ITEMS = "external_packaging_receipt_items"


def _create_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in (RECEIPTS, ITEMS):
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"""
                    CREATE TRIGGER trg_{table}_immutable_{action.lower()}
                    BEFORE {action} ON {table}
                    FOR EACH ROW
                    BEGIN
                        SELECT RAISE(ABORT, '{table} rows are immutable');
                    END
                    """
                )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION p1_33c5_immutable_external_receipt()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'external packaging receipt facts are immutable';
            END;
            $$
            """
        )
        for table in (RECEIPTS, ITEMS):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_immutable_write
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION p1_33c5_immutable_external_receipt()
                """
            )


def _drop_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in (ITEMS, RECEIPTS):
            for action in ("delete", "update"):
                op.execute(
                    f"DROP TRIGGER IF EXISTS trg_{table}_immutable_{action}"
                )
    elif dialect == "postgresql":
        for table in (ITEMS, RECEIPTS):
            op.execute(
                f"DROP TRIGGER IF EXISTS trg_{table}_immutable_write ON {table}"
            )
        op.execute(
            "DROP FUNCTION IF EXISTS p1_33c5_immutable_external_receipt()"
        )


def upgrade() -> None:
    op.create_table(
        RECEIPTS,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("purchase_order_id", sa.Integer(), nullable=False),
        sa.Column("receipt_number", sa.String(60), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("received_by", sa.Integer(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"],
            ["external_packaging_purchase_orders.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["received_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "receipt_number", name="uq_external_packaging_receipt_number"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_external_packaging_receipt_key"
        ),
    )
    op.create_index(
        "ix_external_packaging_receipts_purchase_order_id",
        RECEIPTS,
        ["purchase_order_id"],
    )
    op.create_table(
        ITEMS,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("purchase_item_id", sa.Integer(), nullable=False),
        sa.Column("received_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("purchase_unit_snapshot", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(
            ["receipt_id"], [f"{RECEIPTS}.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["purchase_item_id"],
            ["external_packaging_purchase_items.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "receipt_id",
            "purchase_item_id",
            name="uq_external_packaging_receipt_purchase_item",
        ),
        sa.CheckConstraint(
            "received_quantity > 0",
            name="ck_external_packaging_receipt_item_quantity",
        ),
    )
    op.create_index(
        "ix_external_packaging_receipt_items_receipt_id", ITEMS, ["receipt_id"]
    )
    op.create_index(
        "ix_external_packaging_receipt_items_purchase_item_id",
        ITEMS,
        ["purchase_item_id"],
    )
    _create_immutable_guards()


def downgrade() -> None:
    count = int(
        op.get_bind()
        .execute(sa.text(f"SELECT COUNT(*) FROM {RECEIPTS}"))
        .scalar_one()
        or 0
    )
    if count:
        raise RuntimeError(
            "P1-33C5 已存在外购包装实收到货事实，禁止破坏性降级；请恢复升级前备份"
        )
    _drop_immutable_guards()
    op.drop_index(
        "ix_external_packaging_receipt_items_purchase_item_id", table_name=ITEMS
    )
    op.drop_index("ix_external_packaging_receipt_items_receipt_id", table_name=ITEMS)
    op.drop_table(ITEMS)
    op.drop_index("ix_external_packaging_receipts_purchase_order_id", table_name=RECEIPTS)
    op.drop_table(RECEIPTS)
