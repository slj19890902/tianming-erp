"""Capture new finished-stock physical identity without backfilling old facts."""
from alembic import op
import sqlalchemy as sa

revision = "sh20v8x9z82"
down_revision = "sg19v8x9z81"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("finished_goods_inventory_details", sa.Column("physical_basis_json", sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM finished_goods_inventory_details WHERE physical_basis_json IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("已有库存规格工艺身份依据，禁止删除；请恢复经验证的升级前备份")
    with op.batch_alter_table("finished_goods_inventory_details") as batch:
        batch.drop_column("physical_basis_json")
