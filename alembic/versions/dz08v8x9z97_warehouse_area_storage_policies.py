"""Formal warehouse-area storage policies and twin-zone bindings.

Revision ID: dz08v8x9z97
Revises: dy07v8x9z96
"""

from alembic import op
import sqlalchemy as sa


revision = "dz08v8x9z97"
down_revision = "dy07v8x9z96"
branch_labels = None
depends_on = None

TABLE = "warehouse_area_storage_policies"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "area_id",
            sa.Integer(),
            sa.ForeignKey("warehouse_areas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("map_feature_id", sa.String(80), nullable=False),
        sa.Column("allowed_inventory_types_json", sa.Text(), nullable=False),
        sa.Column("storage_layout", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("draft_map_revision", sa.String(64), nullable=True),
        sa.Column("published_map_revision", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("area_id", name="uq_warehouse_area_storage_policies_area"),
        sa.UniqueConstraint(
            "map_feature_id", name="uq_warehouse_area_storage_policies_feature"
        ),
        sa.CheckConstraint(
            "storage_layout IN ('rack','pallet_ground','mixed')",
            name="ck_warehouse_area_storage_policies_layout",
        ),
        sa.CheckConstraint(
            "status IN ('draft','published')",
            name="ck_warehouse_area_storage_policies_status",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_warehouse_area_storage_policies_version"
        ),
    )
    op.create_index(
        "ix_warehouse_area_storage_policies_status", TABLE, ["status"]
    )


def downgrade() -> None:
    count = int(
        op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one()
        or 0
    )
    if count:
        raise RuntimeError(
            "已存在正式仓库区域与地图绑定，禁止破坏性降级；请恢复升级前备份"
        )
    op.drop_index("ix_warehouse_area_storage_policies_status", table_name=TABLE)
    op.drop_table(TABLE)
