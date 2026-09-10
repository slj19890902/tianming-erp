"""Freeze explicit material, inventory and delivery choices; preserve old profiles."""
from alembic import op
import sqlalchemy as sa

revision = "sf18v8x9z80"
down_revision = "se17v8x9z79"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("product_bom_profiles") as batch:
        batch.add_column(sa.Column("material_mode", sa.String(30), nullable=True))
        batch.add_column(sa.Column("delivery_mode", sa.String(20), nullable=True))
        batch.drop_constraint("ck_product_bom_profile_source", type_="check")
        batch.create_check_constraint("ck_product_bom_profile_source",
            "source IN ('manufactured','purchased','assembled','separate')")
        batch.create_check_constraint("ck_product_bom_profile_modes",
            "(material_mode IS NULL AND delivery_mode IS NULL AND source <> 'separate') OR "
            "(material_mode = 'expand_children' AND material_mode IS NOT NULL "
            "AND delivery_mode IS NOT NULL AND delivery_mode IN ('parent','components') "
            "AND NOT (source = 'assembled' AND delivery_mode = 'components'))")


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM product_bom_profiles WHERE "
            "material_mode IS NOT NULL OR delivery_mode IS NOT NULL OR source = 'separate' LIMIT 1")).first():
        raise RuntimeError("已有独立BOM配置，禁止删除；请使用经验证的升级前备份")
    # Master configuration can be removed later; frozen order evidence must
    # still prevent running an old reader against a new snapshot contract.
    if bind.execute(sa.text("SELECT 1 FROM order_bom_graphs WHERE schema_version >= 3 LIMIT 1")).first():
        raise RuntimeError("已有三维BOM订单快照，禁止降级删除执行能力")
    with op.batch_alter_table("product_bom_profiles") as batch:
        batch.drop_constraint("ck_product_bom_profile_modes", type_="check")
        batch.drop_constraint("ck_product_bom_profile_source", type_="check")
        batch.create_check_constraint("ck_product_bom_profile_source",
            "source IN ('manufactured','purchased','assembled')")
        batch.drop_column("material_mode")
        batch.drop_column("delivery_mode")
