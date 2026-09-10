"""Manual goods applicability and supplier face-paper metadata, without stock backfill."""
from alembic import op
import sqlalchemy as sa

revision = "rv10v8x9z70"
down_revision = "rt10v8x9z67"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("materials", sa.Column("is_white_face", sa.Boolean(), server_default="0", nullable=False))
    op.create_table("warehouse_goods_profiles",
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("data_json", sa.Text(), nullable=False))
    op.create_table("warehouse_goods_mutations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(100), unique=True, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False))
    op.create_index("ix_warehouse_goods_mutations_lot_id", "warehouse_goods_mutations", ["lot_id"])


def downgrade():
    connection = op.get_bind()
    for table in ("warehouse_goods_profiles", "warehouse_goods_mutations"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Goods history exists; restore a verified backup instead")
    if connection.execute(sa.text("SELECT 1 FROM materials WHERE is_white_face = 1 LIMIT 1")).first():
        raise RuntimeError("White face facts exist; restore a verified backup instead")
    op.drop_table("warehouse_goods_mutations")
    op.drop_table("warehouse_goods_profiles")
    op.drop_column("materials", "is_white_face")
