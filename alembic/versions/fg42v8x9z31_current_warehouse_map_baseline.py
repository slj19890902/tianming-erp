"""migrate warehouse locations to the owner-confirmed current map

Revision ID: fg42v8x9z31
Revises: ef41v8x9z30
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.services.warehouse_current_map_baseline import (
    downgrade_current_map,
    upgrade_current_map,
)


revision = "fg42v8x9z31"
down_revision = "ef41v8x9z30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "warehouse_current_map_migration_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("migration_key", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("post_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "migration_key",
            name="uq_warehouse_current_map_migration_snapshots_key",
        ),
    )
    with op.batch_alter_table("warehouse_area_storage_policies") as batch_op:
        batch_op.drop_constraint(
            "ck_warehouse_area_storage_policies_layout",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_warehouse_area_storage_policies_layout",
            "storage_layout IN ('rack','pallet_ground','mixed','functional')",
        )
    with op.batch_alter_table("floor3_location_layouts") as batch_op:
        batch_op.drop_constraint(
            "ck_floor3_location_layouts_layout_kind",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_floor3_location_layouts_layout_kind",
            "layout_kind IN ('unknown','physical_pallet','physical_rack','logical_anchor')",
        )
    connection = op.get_bind()
    twin_count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM warehouse_locations "
                "WHERE source_version='TWIN_V1'"
            )
        ).scalar_one()
        or 0
    )
    business_fact_count = int(
        connection.execute(
            sa.text(
                "SELECT (SELECT COUNT(*) FROM inventory_lots) + "
                "(SELECT COUNT(*) FROM inventory_pallets)"
            )
        ).scalar_one()
        or 0
    )
    # Brand-new empty installations do not contain the factory's manually
    # confirmed current-map rows and therefore have no business facts to
    # migrate.  Formal/copy databases must pass the strict audited profile.
    if twin_count or business_fact_count:
        upgrade_current_map(connection)


def downgrade() -> None:
    connection = op.get_bind()
    snapshot_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM warehouse_current_map_migration_snapshots")
        ).scalar_one()
        or 0
    )
    if snapshot_count:
        downgrade_current_map(connection)
    with op.batch_alter_table("floor3_location_layouts") as batch_op:
        batch_op.drop_constraint(
            "ck_floor3_location_layouts_layout_kind",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_floor3_location_layouts_layout_kind",
            "layout_kind IN ('unknown','physical_pallet','logical_anchor')",
        )
    with op.batch_alter_table("warehouse_area_storage_policies") as batch_op:
        batch_op.drop_constraint(
            "ck_warehouse_area_storage_policies_layout",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_warehouse_area_storage_policies_layout",
            "storage_layout IN ('rack','pallet_ground','mixed')",
        )
    op.drop_table("warehouse_current_map_migration_snapshots")
