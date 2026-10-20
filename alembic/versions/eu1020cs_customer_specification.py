"""Store customer specification text separately from physical production sizes."""
from alembic import op
import sqlalchemy as sa

revision = "eu1020cs"
down_revision = "et1019fl"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("customer_specification", sa.String(1000), nullable=True))


def downgrade():
    db = op.get_bind()
    if db.execute(sa.text(
        "SELECT 1 FROM products WHERE customer_specification IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("已有客户规格原文（含明确留空），禁止有损降级")
    if db.execute(sa.text(
        "SELECT 1 FROM master_data_object_versions "
        "WHERE object_type='product' AND json_valid(snapshot_json) "
        "AND json_type(snapshot_json, '$.customer_specification') NOT IN ('null') LIMIT 1"
    )).first():
        raise RuntimeError("已有客户规格历史版本，禁止有损降级")
    op.drop_column("products", "customer_specification")
