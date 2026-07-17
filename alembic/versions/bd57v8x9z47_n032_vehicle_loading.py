"""N032 vehicle volume-loading snapshots and confirmation.

Revision ID: bd57v8x9z47
Revises: bc56v8x9z46
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "bd57v8x9z47"
down_revision = "bc56v8x9z46"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_vehicles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("vehicle_type", sa.String(100), nullable=True),
        sa.Column("plate_number", sa.String(50), nullable=False),
        sa.Column("cargo_length_mm", sa.Numeric(12, 2), nullable=False),
        sa.Column("cargo_width_mm", sa.Numeric(12, 2), nullable=False),
        sa.Column("cargo_height_mm", sa.Numeric(12, 2), nullable=False),
        sa.Column("safety_load_factor", sa.Numeric(6, 4), nullable=False, server_default="1.0"),
        sa.Column("yellow_threshold_pct", sa.Numeric(6, 2), nullable=False, server_default="80"),
        sa.Column("red_threshold_pct", sa.Numeric(6, 2), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("plate_number", name="uq_delivery_vehicles_plate_number"),
        sa.CheckConstraint("cargo_length_mm > 0 AND cargo_width_mm > 0 AND cargo_height_mm > 0", name="ck_delivery_vehicles_positive_dimensions"),
        sa.CheckConstraint("safety_load_factor > 0 AND safety_load_factor <= 1", name="ck_delivery_vehicles_safety_factor"),
        sa.CheckConstraint("yellow_threshold_pct > 0 AND yellow_threshold_pct <= red_threshold_pct", name="ck_delivery_vehicles_thresholds"),
    )
    op.create_index("ix_delivery_vehicles_active_plate", "delivery_vehicles", ["is_active", "plate_number"])
    op.create_table(
        "product_loading_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(30), nullable=False),
        sa.Column("manual_unit_m3", sa.Numeric(14, 8), nullable=True),
        sa.Column("package_piece_count", sa.Integer(), nullable=True),
        sa.Column("package_length_mm", sa.Numeric(12, 2), nullable=True),
        sa.Column("package_width_mm", sa.Numeric(12, 2), nullable=True),
        sa.Column("package_height_mm", sa.Numeric(12, 2), nullable=True),
        sa.Column("source_note", sa.Text(), nullable=True),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("product_id", name="uq_product_loading_profiles_product_id"),
        sa.CheckConstraint("mode IN ('theoretical_box', 'manual_unit', 'package')", name="ck_product_loading_profiles_mode"),
        sa.CheckConstraint("manual_unit_m3 IS NULL OR manual_unit_m3 > 0", name="ck_product_loading_profiles_manual_volume"),
        sa.CheckConstraint("package_piece_count IS NULL OR package_piece_count > 0", name="ck_product_loading_profiles_package_piece_count"),
        sa.CheckConstraint("(package_length_mm IS NULL OR package_length_mm > 0) AND (package_width_mm IS NULL OR package_width_mm > 0) AND (package_height_mm IS NULL OR package_height_mm > 0)", name="ck_product_loading_profiles_package_dimensions"),
    )
    with op.batch_alter_table("sales_deliveries") as batch_op:
        batch_op.add_column(sa.Column("vehicle_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("vehicle_capacity_snapshot_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("loading_total_snapshot_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("estimated_total_volume_m3", sa.Numeric(14, 8), nullable=True))
        batch_op.add_column(sa.Column("load_rate_pct", sa.Numeric(8, 2), nullable=True))
        batch_op.add_column(sa.Column("loading_status", sa.String(30), nullable=False, server_default="not_evaluated"))
        batch_op.add_column(sa.Column("loading_calculation_hash", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("loading_confirmed_by", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("loading_confirmed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("loading_confirmed_hash", sa.String(64), nullable=True))
        batch_op.create_foreign_key("fk_sales_deliveries_vehicle_id", "delivery_vehicles", ["vehicle_id"], ["id"], ondelete="RESTRICT")
        batch_op.create_foreign_key("fk_sales_deliveries_loading_confirmed_by", "users", ["loading_confirmed_by"], ["id"], ondelete="SET NULL")
        batch_op.create_check_constraint("ck_sales_deliveries_loading_status", "loading_status IN ('normal', 'warning', 'over_capacity', 'data_pending', 'not_evaluated')")
        batch_op.create_index("ix_sales_deliveries_vehicle_id", ["vehicle_id"])
    with op.batch_alter_table("sales_delivery_items") as batch_op:
        batch_op.add_column(sa.Column("estimated_volume_m3", sa.Numeric(14, 8), nullable=True))
        batch_op.add_column(sa.Column("loading_snapshot_json", sa.Text(), nullable=True))


def _require_empty_for_downgrade(bind: sa.Connection) -> None:
    checks = (
        ("delivery_vehicles", "车辆主数据"),
        ("product_loading_profiles", "产品装载参数"),
        (
            "sales_deliveries",
            "送货装载事实",
            " OR ".join(
                (
                    "vehicle_id IS NOT NULL",
                    "vehicle_capacity_snapshot_json IS NOT NULL",
                    "loading_total_snapshot_json IS NOT NULL",
                    "estimated_total_volume_m3 IS NOT NULL",
                    "load_rate_pct IS NOT NULL",
                    "loading_status != 'not_evaluated'",
                    "loading_calculation_hash IS NOT NULL",
                    "loading_confirmed_by IS NOT NULL",
                    "loading_confirmed_at IS NOT NULL",
                    "loading_confirmed_hash IS NOT NULL",
                )
            ),
        ),
        ("sales_delivery_items", "送货明细装载事实", "estimated_volume_m3 IS NOT NULL OR loading_snapshot_json IS NOT NULL"),
    )
    for check in checks:
        table, label, *where = check
        clause = f" WHERE {where[0]}" if where else ""
        if bind.execute(sa.text(f"SELECT 1 FROM {table}{clause} LIMIT 1")).first() is not None:
            raise RuntimeError(f"N032 downgrade refused: {label}已存在，请恢复迁移前备份后再降级。")


def downgrade() -> None:
    bind = op.get_bind()
    _require_empty_for_downgrade(bind)
    with op.batch_alter_table("sales_delivery_items") as batch_op:
        batch_op.drop_column("loading_snapshot_json")
        batch_op.drop_column("estimated_volume_m3")
    with op.batch_alter_table("sales_deliveries") as batch_op:
        batch_op.drop_index("ix_sales_deliveries_vehicle_id")
        batch_op.drop_constraint("ck_sales_deliveries_loading_status", type_="check")
        batch_op.drop_constraint("fk_sales_deliveries_loading_confirmed_by", type_="foreignkey")
        batch_op.drop_constraint("fk_sales_deliveries_vehicle_id", type_="foreignkey")
        batch_op.drop_column("loading_confirmed_hash")
        batch_op.drop_column("loading_confirmed_at")
        batch_op.drop_column("loading_confirmed_by")
        batch_op.drop_column("loading_calculation_hash")
        batch_op.drop_column("loading_status")
        batch_op.drop_column("load_rate_pct")
        batch_op.drop_column("estimated_total_volume_m3")
        batch_op.drop_column("loading_total_snapshot_json")
        batch_op.drop_column("vehicle_capacity_snapshot_json")
        batch_op.drop_column("vehicle_id")
    op.drop_table("product_loading_profiles")
    op.drop_index("ix_delivery_vehicles_active_plate", table_name="delivery_vehicles")
    op.drop_table("delivery_vehicles")
