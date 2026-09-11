"""Append-only BOM production revisions, without rewriting original snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "sb14v8x9z76"
down_revision = "sa13v8x9z75"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("order_bom_production_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_bom_graphs.order_item_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("previous_id", sa.Integer(), nullable=True),
        sa.Column("document_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("order_item_id", "revision", name="uq_bom_production_revision"),
        sa.UniqueConstraint("id", "order_item_id", name="uq_bom_production_revision_identity"),
        sa.ForeignKeyConstraint(["previous_id", "order_item_id"],
            ["order_bom_production_revisions.id", "order_bom_production_revisions.order_item_id"],
            ondelete="RESTRICT", name="fk_bom_production_previous"),
        sa.CheckConstraint("revision > 0 AND length(content_hash) = 64", name="ck_bom_production_revision"),
        sa.CheckConstraint("(revision = 1 AND previous_id IS NULL) OR (revision > 1 AND previous_id IS NOT NULL)",
            name="ck_bom_production_previous"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM order_bom_production_revisions LIMIT 1")).first():
        raise RuntimeError("已有BOM生产资料修订，禁止删除；请使用已验证备份回退")
    op.drop_table("order_bom_production_revisions")
