"""Product receipt destinations and explicitly configured staging areas.

Revision ID: rp0912
Revises: st32v8x9z94
"""
from alembic import op
import sqlalchemy as sa

revision = "rp0912"
down_revision = "st32v8x9z94"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("product_storage_preferences",
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("area_id", sa.Integer(), sa.ForeignKey("warehouse_areas.id", ondelete="RESTRICT")),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("warehouse_locations.id", ondelete="RESTRICT")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_product_storage_version"))
    op.create_table("receipt_staging_areas",
        sa.Column("area_id", sa.Integer(), sa.ForeignKey("warehouse_areas.id", ondelete="RESTRICT"), primary_key=True))


def downgrade():
    bind = op.get_bind()
    for name in ("product_storage_preferences", "receipt_staging_areas"):
        if bind.execute(sa.text(f"SELECT count(*) FROM {name}")).scalar():
            raise RuntimeError("收料归位配置已有业务记录，禁止有损降级；请按发布备份回退")
    op.drop_table("receipt_staging_areas")
    op.drop_table("product_storage_preferences")
