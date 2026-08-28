"""bind published map racks to stable cells and register level labels

Revision ID: gr53v8x9z42
Revises: gq52v8x9z41
Create Date: 2026-08-28

No inventory, map geometry, or historical label fact is backfilled.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "gr53v8x9z42"
down_revision = "gq52v8x9z41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("warehouse_locations", recreate="always") as batch:
        batch.add_column(sa.Column("map_rack_id", sa.String(80), nullable=True))
        batch.add_column(sa.Column("rack_display_name", sa.String(100), nullable=True))
        batch.create_index(
            "ix_warehouse_locations_map_rack",
            ["map_rack_id", "is_active"],
            unique=False,
        )

    op.create_table(
        "warehouse_rack_level_label_print_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("floor_code", sa.String(30), nullable=False),
        sa.Column("map_revision", sa.String(64), nullable=False),
        sa.Column(
            "area_id",
            sa.Integer(),
            sa.ForeignKey("warehouse_areas.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("map_feature_id", sa.String(80), nullable=False),
        sa.Column("map_rack_id", sa.String(80), nullable=False),
        sa.Column("map_rack_code", sa.String(50), nullable=False),
        sa.Column("floor_name_snapshot", sa.String(100), nullable=False),
        sa.Column("area_name_snapshot", sa.String(100), nullable=False),
        sa.Column("rack_name_snapshot", sa.String(100), nullable=False),
        sa.Column("level_count", sa.Integer(), nullable=False),
        sa.Column("labels_json", sa.Text(), nullable=False),
        sa.Column("template_version", sa.String(40), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "template_version = 'rack_level_80x40_v1'",
            name="ck_warehouse_rack_level_label_print_jobs_template",
        ),
        sa.CheckConstraint(
            "source = 'region_planning'",
            name="ck_warehouse_rack_level_label_print_jobs_source",
        ),
        sa.CheckConstraint(
            "level_count > 0",
            name="ck_warehouse_rack_level_label_print_jobs_levels",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_warehouse_rack_level_label_print_jobs_idempotency",
        ),
    )
    op.create_index(
        "ix_warehouse_rack_level_label_print_jobs_rack",
        "warehouse_rack_level_label_print_jobs",
        ["floor_code", "map_rack_id", "created_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    print_facts = int(
        connection.scalar(
            sa.text("SELECT COUNT(*) FROM warehouse_rack_level_label_print_jobs")
        )
        or 0
    )
    rack_bindings = int(
        connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM warehouse_locations "
                "WHERE map_rack_id IS NOT NULL OR rack_display_name IS NOT NULL"
            )
        )
        or 0
    )
    if print_facts or rack_bindings:
        raise RuntimeError(
            "cannot downgrade P1-123 after rack-cell bindings or level-label facts exist"
        )

    op.drop_index(
        "ix_warehouse_rack_level_label_print_jobs_rack",
        table_name="warehouse_rack_level_label_print_jobs",
    )
    op.drop_table("warehouse_rack_level_label_print_jobs")
    with op.batch_alter_table("warehouse_locations", recreate="always") as batch:
        batch.drop_index("ix_warehouse_locations_map_rack")
        batch.drop_column("rack_display_name")
        batch.drop_column("map_rack_id")
