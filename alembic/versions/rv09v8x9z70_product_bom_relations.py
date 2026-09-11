"""Explicit physical BOM profiles on existing product and edge identities."""
from alembic import op
import sqlalchemy as sa

revision = "rv09v8x9z70"
down_revision = "ru09v8x9z69"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("product_bom_profiles",
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.CheckConstraint("source IN ('manufactured','purchased','assembled')", name="ck_product_bom_profile_source"))
    op.create_table("product_bom_inventory_relations",
        sa.Column("bom_component_id", sa.Integer(), sa.ForeignKey("product_bom_components.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("relation", sa.String(20), nullable=False),
        sa.CheckConstraint("relation IN ('assembly','accompany')", name="ck_product_bom_inventory_relation"))


def downgrade():
    for table in ("product_bom_inventory_relations", "product_bom_profiles"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("已有多级BOM设置，禁止删除；请使用经验证的发布前备份回滚")
    op.drop_table("product_bom_inventory_relations")
    op.drop_table("product_bom_profiles")
