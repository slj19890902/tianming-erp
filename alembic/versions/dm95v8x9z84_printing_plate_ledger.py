"""add printing plate ledger and frozen production setup

Revision ID: dm95v8x9z84
Revises: dl94v8x9z83
Create Date: 2026-08-08
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "dm95v8x9z84"
down_revision: Union[str, Sequence[str], None] = "dl94v8x9z83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "printing_plates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plate_code", sa.String(length=30), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("plate_name", sa.String(length=200), nullable=False),
        sa.Column("color_name", sa.String(length=100), nullable=False),
        sa.Column("rack_location", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("location_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_location_confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("last_location_confirmed_by", sa.Integer(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('active','inactive','damaged')",
            name="ck_printing_plates_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_printing_plates_version"),
        sa.CheckConstraint(
            "location_version >= 1", name="ck_printing_plates_location_version"
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["last_location_confirmed_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plate_code", name="uq_printing_plates_code"),
    )
    op.create_index(
        "ix_printing_plates_customer_status",
        "printing_plates",
        ["customer_id", "status"],
    )
    op.create_index(
        "uq_printing_plates_occupied_location",
        "printing_plates",
        ["rack_location"],
        unique=True,
        sqlite_where=sa.text("status IN ('active','damaged')"),
        postgresql_where=sa.text("status IN ('active','damaged')"),
    )
    op.create_table(
        "printing_plate_location_movements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("printing_plate_id", sa.Integer(), nullable=False),
        sa.Column("plate_code_snapshot", sa.String(length=30), nullable=False),
        sa.Column("from_location", sa.String(length=100), nullable=False),
        sa.Column("to_location", sa.String(length=100), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column(
            "moved_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=30), server_default="manual_input", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "expected_version >= 1",
            name="ck_printing_plate_movements_expected_version",
        ),
        sa.CheckConstraint(
            "resulting_version = expected_version + 1",
            name="ck_printing_plate_movements_resulting_version",
        ),
        sa.CheckConstraint(
            "from_location <> to_location",
            name="ck_printing_plate_movements_actual_change",
        ),
        sa.ForeignKeyConstraint(
            ["printing_plate_id"], ["printing_plates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_printing_plate_location_movements_idempotency",
        ),
    )
    op.create_index(
        "ix_printing_plate_movements_plate_time",
        "printing_plate_location_movements",
        ["printing_plate_id", "moved_at"],
    )

    with op.batch_alter_table("products", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "printing_plate_mode",
                sa.String(length=20),
                server_default="no_plate",
                nullable=False,
            )
        )
        for index in range(1, 4):
            batch.add_column(
                sa.Column(f"printing_plate_{index}_id", sa.Integer(), nullable=True)
            )
            batch.create_foreign_key(
                f"fk_products_printing_plate_{index}_id",
                "printing_plates",
                [f"printing_plate_{index}_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_index(
                f"ix_products_printing_plate_{index}_id",
                [f"printing_plate_{index}_id"],
            )
        for column_name in (
            "plate_alignment_value_mm",
            "plate_mount_value_mm",
            "machine_set_length_mm",
            "machine_set_width_mm",
            "machine_set_height_mm",
        ):
            batch.add_column(sa.Column(column_name, sa.Numeric(12, 2), nullable=True))
        batch.create_check_constraint(
            "ck_products_printing_plate_mode",
            "printing_plate_mode IN ('no_plate', 'plate')",
        )
        batch.create_check_constraint(
            "ck_products_no_plate_has_no_binding",
            "printing_plate_mode = 'plate' OR "
            "(printing_plate_1_id IS NULL AND printing_plate_2_id IS NULL "
            "AND printing_plate_3_id IS NULL)",
        )

    with op.batch_alter_table("production_tasks", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "printing_plate_mode_snapshot",
                sa.String(length=20),
                server_default="no_plate",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("print_content_snapshot", sa.String(length=100), nullable=True))
        batch.add_column(
            sa.Column(
                "printing_plate_codes_snapshot",
                sa.Text(),
                server_default="[]",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "printing_plate_details_snapshot",
                sa.Text(),
                server_default="[]",
                nullable=False,
            )
        )
        for column_name in (
            "plate_alignment_value_mm_snapshot",
            "plate_mount_value_mm_snapshot",
            "machine_set_length_mm_snapshot",
            "machine_set_width_mm_snapshot",
            "machine_set_height_mm_snapshot",
        ):
            batch.add_column(sa.Column(column_name, sa.Numeric(12, 2), nullable=True))
        batch.create_check_constraint(
            "ck_production_tasks_printing_plate_mode",
            "printing_plate_mode_snapshot IN ('no_plate', 'plate')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    plate_facts = connection.execute(
        sa.text(
            "SELECT "
            "(SELECT COUNT(*) FROM printing_plates) + "
            "(SELECT COUNT(*) FROM printing_plate_location_movements)"
        )
    ).scalar_one()
    configured_products = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM products WHERE "
            "printing_plate_mode <> 'no_plate' OR "
            "printing_plate_1_id IS NOT NULL OR printing_plate_2_id IS NOT NULL OR "
            "printing_plate_3_id IS NOT NULL OR plate_alignment_value_mm IS NOT NULL OR "
            "plate_mount_value_mm IS NOT NULL OR machine_set_length_mm IS NOT NULL OR "
            "machine_set_width_mm IS NOT NULL OR machine_set_height_mm IS NOT NULL"
        )
    ).scalar_one()
    configured_tasks = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM production_tasks WHERE "
            "printing_plate_mode_snapshot <> 'no_plate' OR "
            "print_content_snapshot IS NOT NULL OR "
            "printing_plate_codes_snapshot <> '[]' OR "
            "printing_plate_details_snapshot <> '[]' OR "
            "plate_alignment_value_mm_snapshot IS NOT NULL OR "
            "plate_mount_value_mm_snapshot IS NOT NULL OR "
            "machine_set_length_mm_snapshot IS NOT NULL OR "
            "machine_set_width_mm_snapshot IS NOT NULL OR "
            "machine_set_height_mm_snapshot IS NOT NULL"
        )
    ).scalar_one()
    if int(plate_facts or 0) or int(configured_products or 0) or int(configured_tasks or 0):
        raise RuntimeError(
            "已有挂板台账、常用箱挂板参数或生产任务快照，禁止破坏性降级；"
            "请恢复 dm95v8x9z84 升级前完整备份。"
        )

    with op.batch_alter_table("production_tasks", recreate="always") as batch:
        batch.drop_constraint("ck_production_tasks_printing_plate_mode", type_="check")
        for column_name in (
            "machine_set_height_mm_snapshot",
            "machine_set_width_mm_snapshot",
            "machine_set_length_mm_snapshot",
            "plate_mount_value_mm_snapshot",
            "plate_alignment_value_mm_snapshot",
            "printing_plate_details_snapshot",
            "printing_plate_codes_snapshot",
            "print_content_snapshot",
            "printing_plate_mode_snapshot",
        ):
            batch.drop_column(column_name)

    with op.batch_alter_table("products", recreate="always") as batch:
        batch.drop_constraint("ck_products_no_plate_has_no_binding", type_="check")
        batch.drop_constraint("ck_products_printing_plate_mode", type_="check")
        for index in range(1, 4):
            batch.drop_index(f"ix_products_printing_plate_{index}_id")
            batch.drop_constraint(
                f"fk_products_printing_plate_{index}_id", type_="foreignkey"
            )
        for column_name in (
            "machine_set_height_mm",
            "machine_set_width_mm",
            "machine_set_length_mm",
            "plate_mount_value_mm",
            "plate_alignment_value_mm",
            "printing_plate_3_id",
            "printing_plate_2_id",
            "printing_plate_1_id",
            "printing_plate_mode",
        ):
            batch.drop_column(column_name)

    op.drop_index(
        "ix_printing_plate_movements_plate_time",
        table_name="printing_plate_location_movements",
    )
    op.drop_table("printing_plate_location_movements")
    op.drop_index("uq_printing_plates_occupied_location", table_name="printing_plates")
    op.drop_index("ix_printing_plates_customer_status", table_name="printing_plates")
    op.drop_table("printing_plates")
