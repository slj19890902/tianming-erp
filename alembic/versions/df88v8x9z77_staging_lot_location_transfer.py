"""P1-25C2 staging lot whole or partial location transfer.

Revision ID: df88v8x9z77
Revises: de87v8x9z76
Create Date: 2026-08-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "df88v8x9z77"
down_revision = "de87v8x9z76"
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
        "inventory_lot_transfers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_lot_id", sa.Integer(), nullable=False),
        sa.Column("target_lot_id", sa.Integer(), nullable=False),
        sa.Column("source_location_id", sa.Integer(), nullable=False),
        sa.Column("target_location_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("available_quantity", sa.Integer(), nullable=False),
        sa.Column("reserved_quantity", sa.Integer(), nullable=False),
        sa.Column("source_version_before", sa.Integer(), nullable=False),
        sa.Column("source_version_after", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("transferred_by", sa.Integer(), nullable=True),
        sa.Column("transferred_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("quantity > 0", name="ck_inventory_lot_transfers_quantity"),
        sa.CheckConstraint(
            "available_quantity >= 0 AND reserved_quantity >= 0 "
            "AND available_quantity + reserved_quantity = quantity",
            name="ck_inventory_lot_transfers_balance",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_inventory_lot_transfers_request_hash",
        ),
        sa.ForeignKeyConstraint(["source_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_location_id"], ["warehouse_locations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_location_id"], ["warehouse_locations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["transferred_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_inventory_lot_transfers_idempotency"),
    )
    op.create_index(
        "ix_inventory_lot_transfers_source",
        "inventory_lot_transfers",
        ["source_lot_id", "transferred_at"],
    )
    op.create_index(
        "ix_inventory_lot_transfers_target",
        "inventory_lot_transfers",
        ["target_lot_id", "transferred_at"],
    )

    connection = op.get_bind()
    trigger_sql = _drop_sqlite_triggers_referencing(connection, "inventory_movements")
    with op.batch_alter_table("inventory_movements") as batch:
        batch.drop_constraint("ck_inventory_movements_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_movements_type",
            "movement_type IN ('manual_in','adjust','freeze','unfreeze','damage','scrap',"
            "'transfer_to_general','location_transfer','reserve','release_reserve',"
            "'consume','reverse_consume')",
        )
    _restore_sqlite_triggers(connection, trigger_sql)


def downgrade() -> None:
    connection = op.get_bind()
    facts = int(
        connection.execute(sa.text("SELECT COUNT(*) FROM inventory_lot_transfers")).scalar()
        or 0
    )
    movements = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM inventory_movements "
                "WHERE movement_type='location_transfer'"
            )
        ).scalar()
        or 0
    )
    if facts or movements:
        raise RuntimeError("已有待送区库位转移事实，拒绝破坏性降级")

    trigger_sql = _drop_sqlite_triggers_referencing(connection, "inventory_movements")
    with op.batch_alter_table("inventory_movements") as batch:
        batch.drop_constraint("ck_inventory_movements_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_movements_type",
            "movement_type IN ('manual_in','adjust','freeze','unfreeze','damage','scrap',"
            "'transfer_to_general','reserve','release_reserve','consume','reverse_consume')",
        )
    _restore_sqlite_triggers(connection, trigger_sql)
    op.drop_index("ix_inventory_lot_transfers_target", table_name="inventory_lot_transfers")
    op.drop_index("ix_inventory_lot_transfers_source", table_name="inventory_lot_transfers")
    op.drop_table("inventory_lot_transfers")
