"""add no-order external packaging stock replenishment source

Revision ID: ji68v8x9z57
Revises: jh67v8x9z56
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ji68v8x9z57"
down_revision = "jh67v8x9z56"
branch_labels = None
depends_on = None


_PURGE_AUTH = "external_packaging_purchase_purge_authorizations"
_BATCH = "external_packaging_purchase_batches"
_PURCHASE = "external_packaging_purchase_orders"
_ITEM = "external_packaging_purchase_items"
_CANCEL = "external_packaging_purchase_cancellations"
_RECEIPT = "external_packaging_receipts"
_RECEIPT_ITEM = "external_packaging_receipt_items"


def _sqlite_drop_external_purchase_guards() -> None:
    """Remove cross-table guards before SQLite recreates their target tables.

    SQLite batch alters rename the old table to a temporary name.  A trigger on
    the purge-authorisation table refers to the batch table, so leaving it in
    place makes the temporary-table rename fail on a populated formal schema.
    Recreate the same guards once all affected tables have their final names.
    """

    for trigger in (
        f"trg_{_PURGE_AUTH}_safe_insert",
        f"trg_{_BATCH}_immutable_update",
        f"trg_{_BATCH}_immutable_delete",
        f"trg_{_PURCHASE}_immutable_update",
        f"trg_{_PURCHASE}_immutable_delete",
        f"trg_{_ITEM}_immutable_update",
        f"trg_{_ITEM}_immutable_delete",
        f"trg_{_CANCEL}_immutable_update",
        f"trg_{_CANCEL}_immutable_delete",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _sqlite_create_external_purchase_guards() -> None:
    for table in (_BATCH, _PURCHASE, _ITEM, _CANCEL):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable_update
            BEFORE UPDATE ON {table}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )
    op.execute(
        f"""
        CREATE TRIGGER trg_{_PURGE_AUTH}_safe_insert
        BEFORE INSERT ON {_PURGE_AUTH}
        FOR EACH ROW WHEN
            NOT EXISTS (SELECT 1 FROM {_BATCH} b WHERE b.id = NEW.batch_id)
            OR NOT EXISTS (SELECT 1 FROM {_PURCHASE} p WHERE p.batch_id = NEW.batch_id)
            OR EXISTS (
                SELECT 1 FROM {_PURCHASE} p
                LEFT JOIN {_CANCEL} c ON c.purchase_order_id = p.id
                WHERE p.batch_id = NEW.batch_id AND c.id IS NULL
            )
            OR EXISTS (
                SELECT 1 FROM {_RECEIPT} r
                JOIN {_PURCHASE} p ON p.id = r.purchase_order_id
                WHERE p.batch_id = NEW.batch_id
            )
        BEGIN
            SELECT RAISE(ABORT, 'external purchase batch is not fully cancelled and unreceived');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{_ITEM}_immutable_delete
        BEFORE DELETE ON {_ITEM}
        FOR EACH ROW WHEN
            NOT EXISTS (
                SELECT 1 FROM {_PURCHASE} p
                JOIN {_PURGE_AUTH} a ON a.batch_id = p.batch_id
                WHERE p.id = OLD.purchase_order_id
            )
            OR EXISTS (SELECT 1 FROM {_RECEIPT_ITEM} r WHERE r.purchase_item_id = OLD.id)
        BEGIN
            SELECT RAISE(ABORT, '{_ITEM} rows are immutable');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{_CANCEL}_immutable_delete
        BEFORE DELETE ON {_CANCEL}
        FOR EACH ROW WHEN
            NOT EXISTS (
                SELECT 1 FROM {_PURCHASE} p
                JOIN {_PURGE_AUTH} a ON a.batch_id = p.batch_id
                WHERE p.id = OLD.purchase_order_id
            )
            OR EXISTS (SELECT 1 FROM {_RECEIPT} r WHERE r.purchase_order_id = OLD.purchase_order_id)
        BEGIN
            SELECT RAISE(ABORT, '{_CANCEL} rows are immutable');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{_PURCHASE}_immutable_delete
        BEFORE DELETE ON {_PURCHASE}
        FOR EACH ROW WHEN
            NOT EXISTS (SELECT 1 FROM {_PURGE_AUTH} a WHERE a.batch_id = OLD.batch_id)
            OR EXISTS (SELECT 1 FROM {_RECEIPT} r WHERE r.purchase_order_id = OLD.id)
        BEGIN
            SELECT RAISE(ABORT, '{_PURCHASE} rows are immutable');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{_BATCH}_immutable_delete
        BEFORE DELETE ON {_BATCH}
        FOR EACH ROW WHEN NOT EXISTS (
            SELECT 1 FROM {_PURGE_AUTH} a WHERE a.batch_id = OLD.id
        )
        BEGIN
            SELECT RAISE(ABORT, '{_BATCH} rows are immutable');
        END
        """
    )


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        _sqlite_drop_external_purchase_guards()
    with op.batch_alter_table(
        "external_packaging_purchase_batches", recreate="always"
    ) as batch:
        batch.alter_column("sales_order_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(
            sa.Column("stock_replenishment_order_id", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_external_packaging_purchase_batch_stock_replenishment",
            "stock_replenishment_orders",
            ["stock_replenishment_order_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_external_packaging_purchase_batch_source",
            "((sales_order_id IS NOT NULL AND stock_replenishment_order_id IS NULL) "
            "OR (sales_order_id IS NULL AND stock_replenishment_order_id IS NOT NULL))",
        )
        batch.create_index(
            "ix_external_packaging_purchase_batches_stock_replenishment_order_id",
            ["stock_replenishment_order_id"],
        )

    with op.batch_alter_table(
        "external_packaging_purchase_items", recreate="always"
    ) as batch:
        batch.alter_column("sales_order_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column(
            "sales_order_item_id", existing_type=sa.Integer(), nullable=True
        )
        batch.alter_column(
            "order_component_id", existing_type=sa.Integer(), nullable=True
        )
        batch.alter_column(
            "order_candidate_id", existing_type=sa.Integer(), nullable=True
        )
        batch.add_column(
            sa.Column("stock_replenishment_item_id", sa.Integer(), nullable=True)
        )
        batch.add_column(
            sa.Column("customer_product_id_snapshot", sa.Integer(), nullable=True)
        )
        batch.add_column(
            sa.Column("order_quantity_basis_snapshot", sa.Numeric(18, 6), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "purchase_quantity_basis_snapshot", sa.Numeric(18, 6), nullable=True
            )
        )
        batch.create_foreign_key(
            "fk_external_packaging_purchase_item_stock_replenishment",
            "stock_replenishment_order_items",
            ["stock_replenishment_item_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_external_packaging_purchase_item_customer_product",
            "products",
            ["customer_product_id_snapshot"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_external_packaging_purchase_stock_item",
            ["purchase_order_id", "stock_replenishment_item_id"],
        )
        batch.create_check_constraint(
            "ck_external_packaging_purchase_item_source",
            "((stock_replenishment_item_id IS NULL "
            "AND sales_order_id IS NOT NULL "
            "AND sales_order_item_id IS NOT NULL "
            "AND order_component_id IS NOT NULL "
            "AND order_candidate_id IS NOT NULL) "
            "OR (stock_replenishment_item_id IS NOT NULL "
            "AND sales_order_id IS NULL "
            "AND sales_order_item_id IS NULL "
            "AND order_component_id IS NULL "
            "AND order_candidate_id IS NULL "
            "AND customer_product_id_snapshot IS NOT NULL "
            "AND order_quantity_basis_snapshot > 0 "
            "AND purchase_quantity_basis_snapshot > 0))",
        )
        batch.create_index(
            "ix_external_packaging_purchase_items_stock_replenishment_item_id",
            ["stock_replenishment_item_id"],
        )

    with op.batch_alter_table(
        "external_packaging_receipt_items", recreate="always"
    ) as batch:
        batch.add_column(
            sa.Column(
                "converted_finished_quantity",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "loose_remainder_quantity_after",
                sa.Numeric(18, 6),
                nullable=False,
                server_default="0",
            )
        )
        batch.create_check_constraint(
            "ck_external_packaging_receipt_item_conversion",
            "converted_finished_quantity >= 0 AND loose_remainder_quantity_after >= 0",
        )
    if sqlite:
        _sqlite_create_external_purchase_guards()


def downgrade() -> None:
    connection = op.get_bind()
    stock_batches = int(
        connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM external_packaging_purchase_batches "
                "WHERE stock_replenishment_order_id IS NOT NULL"
            )
        )
        or 0
    )
    converted_receipts = int(
        connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM external_packaging_receipt_items "
                "WHERE converted_finished_quantity <> 0 "
                "OR loose_remainder_quantity_after <> 0"
            )
        )
        or 0
    )
    if stock_batches or converted_receipts:
        raise RuntimeError(
            "已存在无订单外购备库或散件换算事实，拒绝降级丢失正式业务来源"
        )

    sqlite = connection.dialect.name == "sqlite"
    if sqlite:
        _sqlite_drop_external_purchase_guards()

    with op.batch_alter_table(
        "external_packaging_receipt_items", recreate="always"
    ) as batch:
        batch.drop_constraint(
            "ck_external_packaging_receipt_item_conversion", type_="check"
        )
        batch.drop_column("loose_remainder_quantity_after")
        batch.drop_column("converted_finished_quantity")

    with op.batch_alter_table(
        "external_packaging_purchase_items", recreate="always"
    ) as batch:
        batch.drop_index(
            "ix_external_packaging_purchase_items_stock_replenishment_item_id"
        )
        batch.drop_constraint(
            "ck_external_packaging_purchase_item_source", type_="check"
        )
        batch.drop_constraint(
            "uq_external_packaging_purchase_stock_item", type_="unique"
        )
        batch.drop_constraint(
            "fk_external_packaging_purchase_item_customer_product",
            type_="foreignkey",
        )
        batch.drop_constraint(
            "fk_external_packaging_purchase_item_stock_replenishment",
            type_="foreignkey",
        )
        batch.drop_column("purchase_quantity_basis_snapshot")
        batch.drop_column("order_quantity_basis_snapshot")
        batch.drop_column("customer_product_id_snapshot")
        batch.drop_column("stock_replenishment_item_id")
        batch.alter_column(
            "order_candidate_id", existing_type=sa.Integer(), nullable=False
        )
        batch.alter_column(
            "order_component_id", existing_type=sa.Integer(), nullable=False
        )
        batch.alter_column(
            "sales_order_item_id", existing_type=sa.Integer(), nullable=False
        )
        batch.alter_column("sales_order_id", existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table(
        "external_packaging_purchase_batches", recreate="always"
    ) as batch:
        batch.drop_index(
            "ix_external_packaging_purchase_batches_stock_replenishment_order_id"
        )
        batch.drop_constraint(
            "ck_external_packaging_purchase_batch_source", type_="check"
        )
        batch.drop_constraint(
            "fk_external_packaging_purchase_batch_stock_replenishment",
            type_="foreignkey",
        )
        batch.drop_column("stock_replenishment_order_id")
        batch.alter_column("sales_order_id", existing_type=sa.Integer(), nullable=False)
    if sqlite:
        _sqlite_create_external_purchase_guards()
