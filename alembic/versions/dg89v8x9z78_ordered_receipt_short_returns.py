"""add ordered delivery short-receipt return inventory facts

Revision ID: dg89v8x9z78
Revises: df88v8x9z77
Create Date: 2026-08-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "dg89v8x9z78"
down_revision = "df88v8x9z77"
branch_labels = None
depends_on = None


def _drop_sqlite_triggers_referencing(connection, table_name: str) -> list[str]:
    if connection.dialect.name != "sqlite":
        return []
    rows = connection.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND sql IS NOT NULL AND sql LIKE :needle"
        ),
        {"needle": f"%{table_name}%"},
    ).mappings().all()
    definitions: list[str] = []
    for row in rows:
        name = str(row["name"])
        if not name.replace("_", "").isalnum():
            raise RuntimeError(f"检测到无法安全处理的 SQLite 触发器名称：{name}")
        definitions.append(str(row["sql"]))
        connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
    return definitions


def _restore_sqlite_triggers(connection, definitions: list[str]) -> None:
    for definition in definitions:
        connection.exec_driver_sql(definition)


def upgrade() -> None:
    op.create_table(
        "ordered_finished_receipt_returns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("return_receipt_item_id", sa.Integer(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("delivery_item_id", sa.Integer(), nullable=False),
        sa.Column("delivery_inventory_allocation_id", sa.Integer(), nullable=False),
        sa.Column("source_inventory_lot_id", sa.Integer(), nullable=False),
        sa.Column("return_inventory_lot_id", sa.Integer(), nullable=False),
        sa.Column("return_location_id", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=True),
        sa.Column("return_in_movement_id", sa.Integer(), nullable=False),
        sa.Column("source_reverse_movement_id", sa.Integer(), nullable=False),
        sa.Column("source_transfer_movement_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("resolution_action", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("reconsume_movement_id", sa.Integer(), nullable=True),
        sa.Column("source_reconsume_movement_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("reconsumed_by", sa.Integer(), nullable=True),
        sa.Column("reconsumed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("quantity > 0", name="ck_ordered_receipt_returns_quantity"),
        sa.CheckConstraint(
            "resolution_action IN ('continue_delivery','accept_short')",
            name="ck_ordered_receipt_returns_resolution",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND reconsume_movement_id IS NULL "
            "AND source_reconsume_movement_id IS NULL "
            "AND reconsumed_at IS NULL) OR "
            "(status = 'reconsumed' AND reconsume_movement_id IS NOT NULL "
            "AND source_reconsume_movement_id IS NOT NULL "
            "AND reconsumed_at IS NOT NULL)",
            name="ck_ordered_receipt_returns_status",
        ),
        sa.ForeignKeyConstraint(
            ["return_receipt_item_id"],
            ["finance_return_receipt_items.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_item_id"], ["sales_delivery_items.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["delivery_inventory_allocation_id"],
            ["delivery_inventory_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_inventory_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["return_inventory_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["return_location_id"], ["warehouse_locations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["inventory_reservations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["return_in_movement_id"], ["inventory_movements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_reverse_movement_id"], ["inventory_movements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_transfer_movement_id"], ["inventory_movements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reconsume_movement_id"], ["inventory_movements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_reconsume_movement_id"], ["inventory_movements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reconsumed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "return_receipt_item_id",
            "sequence_no",
            name="uq_ordered_receipt_returns_item_sequence",
        ),
        sa.UniqueConstraint(
            "return_inventory_lot_id", name="uq_ordered_receipt_returns_lot"
        ),
        sa.UniqueConstraint(
            "return_in_movement_id", name="uq_ordered_receipt_returns_in_movement"
        ),
        sa.UniqueConstraint(
            "source_reverse_movement_id",
            name="uq_ordered_receipt_returns_source_reverse_movement",
        ),
        sa.UniqueConstraint(
            "source_transfer_movement_id",
            name="uq_ordered_receipt_returns_source_transfer_movement",
        ),
        sa.UniqueConstraint(
            "reconsume_movement_id", name="uq_ordered_receipt_returns_reconsume_movement"
        ),
        sa.UniqueConstraint(
            "source_reconsume_movement_id",
            name="uq_ordered_receipt_returns_source_reconsume_movement",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_ordered_receipt_returns_idempotency"
        ),
    )
    op.create_index(
        "ix_ordered_receipt_returns_receipt_item",
        "ordered_finished_receipt_returns",
        ["return_receipt_item_id", "status"],
    )
    op.create_index(
        "ix_ordered_receipt_returns_location",
        "ordered_finished_receipt_returns",
        ["return_location_id", "status"],
    )

    connection = op.get_bind()
    lot_triggers = _drop_sqlite_triggers_referencing(connection, "inventory_lots")
    with op.batch_alter_table("inventory_lots") as batch:
        batch.drop_constraint("ck_inventory_lots_source_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_lots_source_type",
            "source_type IN ('manual','production_completion','production_surplus',"
            "'purchase_surplus','stocktake','transfer','replenishment','delivery_return')",
        )
    _restore_sqlite_triggers(connection, lot_triggers)

    movement_triggers = _drop_sqlite_triggers_referencing(
        connection, "inventory_movements"
    )
    with op.batch_alter_table("inventory_movements") as batch:
        batch.drop_constraint("ck_inventory_movements_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_movements_type",
            "movement_type IN ('manual_in','adjust','freeze','unfreeze','damage','scrap',"
            "'transfer_to_general','location_transfer','reserve','release_reserve',"
            "'consume','reverse_consume','return_in','return_reconsume')",
        )
    _restore_sqlite_triggers(connection, movement_triggers)


def downgrade() -> None:
    connection = op.get_bind()
    facts = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM ordered_finished_receipt_returns")
        ).scalar()
        or 0
    )
    lots = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM inventory_lots WHERE source_type='delivery_return'")
        ).scalar()
        or 0
    )
    movements = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM inventory_movements "
                "WHERE movement_type IN ('return_in','return_reconsume')"
            )
        ).scalar()
        or 0
    )
    if facts or lots or movements:
        raise RuntimeError("已有客户短收回库事实，拒绝破坏性降级")

    movement_triggers = _drop_sqlite_triggers_referencing(
        connection, "inventory_movements"
    )
    with op.batch_alter_table("inventory_movements") as batch:
        batch.drop_constraint("ck_inventory_movements_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_movements_type",
            "movement_type IN ('manual_in','adjust','freeze','unfreeze','damage','scrap',"
            "'transfer_to_general','location_transfer','reserve','release_reserve',"
            "'consume','reverse_consume')",
        )
    _restore_sqlite_triggers(connection, movement_triggers)

    lot_triggers = _drop_sqlite_triggers_referencing(connection, "inventory_lots")
    with op.batch_alter_table("inventory_lots") as batch:
        batch.drop_constraint("ck_inventory_lots_source_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_lots_source_type",
            "source_type IN ('manual','production_completion','production_surplus',"
            "'purchase_surplus','stocktake','transfer','replenishment')",
        )
    _restore_sqlite_triggers(connection, lot_triggers)

    op.drop_index(
        "ix_ordered_receipt_returns_location",
        table_name="ordered_finished_receipt_returns",
    )
    op.drop_index(
        "ix_ordered_receipt_returns_receipt_item",
        table_name="ordered_finished_receipt_returns",
    )
    op.drop_table("ordered_finished_receipt_returns")
