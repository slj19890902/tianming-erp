"""Advisory candidate-use history only; no inventory backfill."""
from alembic import op
import sqlalchemy as sa

revision = "rt10v8x9z67"
down_revision = "ru10v8x9z69"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("material_candidate_selections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lot_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("candidates_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("lot_id", "lot_version", name="uq_material_candidate_version"))
    op.create_index("ix_material_candidate_selections_lot_id", "material_candidate_selections", ["lot_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM material_candidate_selections LIMIT 1")).first():
        raise RuntimeError("Candidate-use history exists; restore a verified backup instead")
    op.drop_table("material_candidate_selections")
