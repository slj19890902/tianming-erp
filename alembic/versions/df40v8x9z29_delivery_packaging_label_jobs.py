"""allow production packaging label jobs to freeze a delivery source

Revision ID: df40v8x9z29
Revises: de39v8x9z28
Create Date: 2026-08-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "df40v8x9z29"
down_revision = "de39v8x9z28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("production_packaging_label_print_jobs") as batch:
        batch.drop_constraint(
            "ck_production_packaging_label_print_jobs_source",
            type_="check",
        )
        batch.add_column(sa.Column("delivery_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_production_packaging_label_print_jobs_delivery_id",
            "sales_deliveries",
            ["delivery_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_production_packaging_label_print_jobs_source",
            "((supplier_order_id IS NOT NULL AND material_requisition_id IS NULL "
            "AND delivery_id IS NULL) OR "
            "(supplier_order_id IS NULL AND material_requisition_id IS NOT NULL "
            "AND delivery_id IS NULL) OR "
            "(supplier_order_id IS NULL AND material_requisition_id IS NULL "
            "AND delivery_id IS NOT NULL))",
        )
        batch.create_index(
            "ix_production_packaging_label_print_jobs_delivery_created",
            ["delivery_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    connection = op.get_bind()
    delivery_jobs = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM production_packaging_label_print_jobs "
                "WHERE delivery_id IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if delivery_jobs:
        raise RuntimeError(
            "Refusing destructive downgrade: delivery packaging-label jobs exist."
        )
    with op.batch_alter_table("production_packaging_label_print_jobs") as batch:
        batch.drop_index(
            "ix_production_packaging_label_print_jobs_delivery_created"
        )
        batch.drop_constraint(
            "ck_production_packaging_label_print_jobs_source",
            type_="check",
        )
        batch.drop_constraint(
            "fk_production_packaging_label_print_jobs_delivery_id",
            type_="foreignkey",
        )
        batch.drop_column("delivery_id")
        batch.create_check_constraint(
            "ck_production_packaging_label_print_jobs_source",
            "((supplier_order_id IS NOT NULL AND material_requisition_id IS NULL) OR "
            "(supplier_order_id IS NULL AND material_requisition_id IS NOT NULL))",
        )
