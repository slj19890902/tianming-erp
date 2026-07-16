"""Add floor-three warehouse locations and pallet inventory foundation.

Revision ID: ar45v7w8x9n35
Revises: aq44v7w8x9m34
Create Date: 2026-07-14
"""

from __future__ import annotations

import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa


revision = "ar45v7w8x9n35"
down_revision = "aq44v7w8x9m34"
branch_labels = None
depends_on = None

DOWNGRADE_BLOCKED_MESSAGE = (
    "三楼 Phase A 已产生栈板业务数据或被正式库存/补库业务引用，"
    "禁止破坏性降级；请停止服务并恢复 ar45 升级前的完整数据库备份。"
)
SEED_COLLISION_MESSAGE_PREFIX = (
    "三楼 Phase A 的 V11 seed 编码与既有 warehouse_locations 冲突，"
    "升级已在写入前中止："
)
SEED_DRIFT_MESSAGE_PREFIX = (
    "三楼 Phase A 的 396 个 V11 seed 结构已漂移，禁止破坏性降级；异常编码："
)


def _seed_locations() -> list[dict[str, object]]:
    """Expand the checked-in V11 ranges in stable spreadsheet order."""
    seed_path = Path(__file__).resolve().parents[1] / "seed_data" / "floor3_locations_v11.json"
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []

    for group in payload["groups"]:
        for segment in group["segments"]:
            side_code = segment.get("side_code")
            level_no = segment.get("level_no")
            for number in range(segment["start"], segment["end"] + 1):
                code = group["code_template"].format(
                    area_code=group["area_code"],
                    side_code=side_code or "",
                    level_no=level_no or "",
                    number=number,
                )
                rows.append(
                    {
                        "location_code": code,
                        "location_name": code,
                        "warehouse_type": payload["warehouse_type"],
                        "warehouse_floor": payload["warehouse_floor"],
                        "area_code": group["area_code"],
                        "storage_type": group["storage_type"],
                        "level_no": level_no,
                        "side_code": side_code,
                        "sort_order": len(rows) + 1,
                        "is_temporary": group["storage_type"] == "temporary_aisle",
                        "remarks": group.get("remarks"),
                        "source_version": payload["source_version"],
                    }
                )

    codes = [row["location_code"] for row in rows]
    if len(rows) != 396 or len(codes) != len(set(codes)):
        raise RuntimeError("Floor-three V11 seed must expand to 396 unique locations")
    if set(payload["excluded_codes"]) & set(codes):
        raise RuntimeError("Floor-three V11 seed contains cancelled location codes")
    return rows


def _assert_seed_codes_available(rows: list[dict[str, object]]) -> None:
    """Abort before any schema or seed write if a V11 code already exists."""
    bind = op.get_bind()
    locations = sa.table(
        "warehouse_locations",
        sa.column("location_code", sa.String()),
    )
    seed_codes = [str(row["location_code"]) for row in rows]
    collisions = sorted(
        str(code)
        for code in bind.execute(
            sa.select(locations.c.location_code).where(
                locations.c.location_code.in_(seed_codes)
            )
        ).scalars()
    )
    if collisions:
        raise RuntimeError(SEED_COLLISION_MESSAGE_PREFIX + ", ".join(collisions))


def _has_v11_references(connection: sa.Connection) -> bool:
    """Return whether any declared foreign key still references a V11 location."""
    inspector = sa.inspect(connection)
    metadata = sa.MetaData()
    locations = sa.Table("warehouse_locations", metadata, autoload_with=connection)
    reflected_tables: dict[str, sa.Table] = {}

    for table_name in inspector.get_table_names():
        if table_name == "warehouse_locations":
            continue
        for foreign_key in inspector.get_foreign_keys(table_name):
            if foreign_key.get("referred_table") != "warehouse_locations":
                continue
            constrained_columns = foreign_key.get("constrained_columns") or []
            referred_columns = foreign_key.get("referred_columns") or []
            if not constrained_columns or len(constrained_columns) != len(
                referred_columns
            ):
                # An uninspectable reference cannot be proven safe to discard.
                return True
            table = reflected_tables.get(table_name)
            if table is None:
                table = sa.Table(table_name, metadata, autoload_with=connection)
                reflected_tables[table_name] = table
            join_condition = sa.and_(
                *(
                    table.c[constrained_column] == locations.c[referred_column]
                    for constrained_column, referred_column in zip(
                        constrained_columns, referred_columns
                    )
                )
            )
            reference_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(table.join(locations, join_condition))
                .where(locations.c.source_version == "V11")
            ).scalar_one()
            if int(reference_count or 0):
                return True
    return False


def _delete_v11_locations() -> None:
    """Delete the complete, verified V11 seed set before dropping its metadata."""
    locations = sa.table(
        "warehouse_locations",
        sa.column("source_version", sa.String()),
    )
    op.get_bind().execute(
        sa.delete(locations).where(locations.c.source_version == "V11")
    )


def _create_storage_type_validation_triggers() -> None:
    """Validate new values without rebuilding the referenced location table."""
    if op.get_bind().dialect.name != "sqlite":
        # The ORM validates this field for application writes.  The deployed
        # database is SQLite; avoid a cross-dialect ALTER CONSTRAINT that could
        # take a stronger lock on warehouse_locations.
        return

    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_warehouse_locations_storage_type_insert
        BEFORE INSERT ON warehouse_locations
        FOR EACH ROW
        WHEN NEW.storage_type IS NOT NULL
         AND NEW.storage_type NOT IN ('ground','rack','temporary_aisle')
        BEGIN
            SELECT RAISE(ABORT, 'invalid warehouse_locations.storage_type');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_warehouse_locations_storage_type_update
        BEFORE UPDATE OF storage_type ON warehouse_locations
        FOR EACH ROW
        WHEN NEW.storage_type IS NOT NULL
         AND NEW.storage_type NOT IN ('ground','rack','temporary_aisle')
        BEGIN
            SELECT RAISE(ABORT, 'invalid warehouse_locations.storage_type');
        END
        """
    )


def upgrade() -> None:
    seed_rows = _seed_locations()
    _assert_seed_codes_available(seed_rows)

    # warehouse_locations is already referenced by inventory_lots,
    # inventory_stock_policies, and stock_replenishment_order_items at an41.
    # Add every nullable/defaulted column in place so the existing table,
    # unique index, ix_warehouse_locations_type_active, and inbound FKs survive.
    op.add_column(
        "warehouse_locations",
        sa.Column("warehouse_floor", sa.Integer(), nullable=True),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column("area_code", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column("storage_type", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column("level_no", sa.Integer(), nullable=True),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column("side_code", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column(
            "is_temporary",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column("source_version", sa.String(length=30), nullable=True),
    )
    _create_storage_type_validation_triggers()

    op.create_table(
        "inventory_pallets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("pallet_code", sa.String(length=100), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "needs_relocation", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('active','closed')", name="ck_inventory_pallets_status"
        ),
        sa.CheckConstraint(
            "is_current = 0 OR location_id IS NOT NULL",
            name="ck_inventory_pallets_current_location",
        ),
        sa.CheckConstraint("version > 0", name="ck_inventory_pallets_version"),
        sa.ForeignKeyConstraint(
            ["location_id"], ["warehouse_locations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("pallet_code", name="uq_inventory_pallets_code"),
    )
    op.create_index(
        "uq_inventory_pallets_current_location",
        "inventory_pallets",
        ["location_id"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
        postgresql_where=sa.text("is_current = true"),
    )

    op.create_table(
        "inventory_pallet_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("pallet_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("inventory_code", sa.String(length=150), nullable=True),
        sa.Column("order_no", sa.String(length=100), nullable=True),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("product_name", sa.String(length=250), nullable=True),
        sa.Column("item_type", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("unit", sa.String(length=30), nullable=False),
        sa.Column(
            "match_status", sa.String(length=20), nullable=False, server_default="pending"
        ),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "item_type IN ('finished','semi_finished','raw_material')",
            name="ck_inventory_pallet_items_type",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_inventory_pallet_items_quantity"),
        sa.CheckConstraint(
            "match_status IN ('matched','pending')",
            name="ck_inventory_pallet_items_match_status",
        ),
        sa.ForeignKeyConstraint(
            ["pallet_id"], ["inventory_pallets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_inventory_pallet_items_pallet_id", "inventory_pallet_items", ["pallet_id"]
    )

    op.create_table(
        "inventory_location_movements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("pallet_id", sa.Integer(), nullable=False),
        sa.Column("from_location_id", sa.Integer(), nullable=True),
        sa.Column("to_location_id", sa.Integer(), nullable=True),
        sa.Column("movement_type", sa.String(length=20), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column(
            "moved_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "movement_type IN ('create','add_item','move','clear')",
            name="ck_inventory_location_movements_type",
        ),
        sa.ForeignKeyConstraint(
            ["pallet_id"], ["inventory_pallets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["from_location_id"], ["warehouse_locations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["to_location_id"], ["warehouse_locations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["operator_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_inventory_location_movements_pallet_moved",
        "inventory_location_movements",
        ["pallet_id", "moved_at"],
    )

    locations = sa.table(
        "warehouse_locations",
        sa.column("location_code", sa.String()),
        sa.column("location_name", sa.String()),
        sa.column("warehouse_type", sa.String()),
        sa.column("warehouse_floor", sa.Integer()),
        sa.column("area_code", sa.String()),
        sa.column("storage_type", sa.String()),
        sa.column("level_no", sa.Integer()),
        sa.column("side_code", sa.String()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_temporary", sa.Boolean()),
        sa.column("remarks", sa.Text()),
        sa.column("source_version", sa.String()),
    )
    op.get_bind().execute(sa.insert(locations), seed_rows)


def _seed_structure_drift_codes(connection) -> list[str]:
    expected = {str(row["location_code"]): row for row in _seed_locations()}
    actual_rows = connection.execute(
        sa.text(
            """
            SELECT
                location_code, location_name, warehouse_type, warehouse_floor,
                area_code, storage_type, level_no, side_code, sort_order,
                is_temporary, is_active, remarks, source_version
            FROM warehouse_locations
            WHERE source_version = 'V11'
            """
        )
    ).mappings()
    actual = {str(row["location_code"]): row for row in actual_rows}
    drift = set(expected) ^ set(actual)
    for code in set(expected) & set(actual):
        expected_row = expected[code]
        expected_signature = (
            expected_row["location_name"],
            expected_row["warehouse_type"],
            expected_row["warehouse_floor"],
            expected_row["area_code"],
            expected_row["storage_type"],
            expected_row["level_no"],
            expected_row["side_code"],
            expected_row["sort_order"],
            bool(expected_row["is_temporary"]),
            True,
            expected_row["remarks"],
            expected_row["source_version"],
        )
        actual_row = actual[code]
        actual_signature = (
            actual_row["location_name"],
            actual_row["warehouse_type"],
            actual_row["warehouse_floor"],
            actual_row["area_code"],
            actual_row["storage_type"],
            actual_row["level_no"],
            actual_row["side_code"],
            actual_row["sort_order"],
            bool(actual_row["is_temporary"]),
            bool(actual_row["is_active"]),
            actual_row["remarks"],
            actual_row["source_version"],
        )
        if actual_signature != expected_signature:
            drift.add(code)
    return sorted(drift)


def _assert_safe_downgrade() -> None:
    """Refuse to discard Phase A data or any references to its V11 locations."""
    connection = op.get_bind()
    usage = connection.execute(
        sa.text(
            """
            SELECT
                (SELECT COUNT(*) FROM inventory_pallets) AS pallets,
                (SELECT COUNT(*) FROM inventory_pallet_items) AS pallet_items,
                (SELECT COUNT(*) FROM inventory_location_movements) AS movements
            """
        )
    ).mappings().one()
    if any(int(usage[key] or 0) for key in usage.keys()) or _has_v11_references(
        connection
    ):
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)
    drift_codes = _seed_structure_drift_codes(connection)
    if drift_codes:
        raise RuntimeError(SEED_DRIFT_MESSAGE_PREFIX + ", ".join(drift_codes))


def downgrade() -> None:
    _assert_safe_downgrade()

    op.drop_index(
        "ix_inventory_location_movements_pallet_moved",
        table_name="inventory_location_movements",
    )
    op.drop_table("inventory_location_movements")
    op.drop_index("ix_inventory_pallet_items_pallet_id", table_name="inventory_pallet_items")
    op.drop_table("inventory_pallet_items")
    op.drop_index("uq_inventory_pallets_current_location", table_name="inventory_pallets")
    op.drop_table("inventory_pallets")

    # The pre-drop guard covers every declared inbound FK, including future or
    # unexpected tables, so no V11 row can survive without its classification.
    _delete_v11_locations()

    op.execute("DROP TRIGGER IF EXISTS trg_warehouse_locations_storage_type_update")
    op.execute("DROP TRIGGER IF EXISTS trg_warehouse_locations_storage_type_insert")

    # SQLite 3.35+ supports in-place DROP COLUMN.  Use explicit ALTER statements
    # so Alembic never chooses batch/recreate for this inbound-FK target table.
    for column_name in (
        "source_version",
        "is_temporary",
        "sort_order",
        "side_code",
        "level_no",
        "storage_type",
        "area_code",
        "warehouse_floor",
    ):
        op.execute(f"ALTER TABLE warehouse_locations DROP COLUMN {column_name}")
