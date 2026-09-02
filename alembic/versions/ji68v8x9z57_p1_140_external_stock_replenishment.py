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


_SQLITE_RECREATED_TABLES = (
    "external_packaging_purchase_batches",
    "external_packaging_purchase_items",
    "external_packaging_receipt_items",
)


def _sqlite_suspend_dependent_triggers(connection) -> list[str]:
    """Drop and remember triggers SQLite reparses during batch table renames."""

    if connection.dialect.name != "sqlite":
        return []
    triggers: list[tuple[str, str]] = []
    rows = connection.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND sql IS NOT NULL ORDER BY name"
        )
    )
    for name, sql in rows:
        normalized = str(sql).lower()
        if any(table in normalized for table in _SQLITE_RECREATED_TABLES):
            triggers.append((str(name), str(sql)))
    for name, _sql in triggers:
        quoted = name.replace('"', '""')
        op.execute(f'DROP TRIGGER IF EXISTS "{quoted}"')
    return [sql for _name, sql in triggers]


def _sqlite_restore_dependent_triggers(trigger_sql: list[str]) -> None:
    for statement in trigger_sql:
        op.execute(statement)


def _upgrade_schema() -> None:
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


def upgrade() -> None:
    trigger_sql = _sqlite_suspend_dependent_triggers(op.get_bind())
    try:
        _upgrade_schema()
    finally:
        _sqlite_restore_dependent_triggers(trigger_sql)


def _downgrade_schema() -> None:
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


def downgrade() -> None:
    trigger_sql = _sqlite_suspend_dependent_triggers(op.get_bind())
    try:
        _downgrade_schema()
    finally:
        _sqlite_restore_dependent_triggers(trigger_sql)
