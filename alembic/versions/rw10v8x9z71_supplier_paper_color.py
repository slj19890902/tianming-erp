"""Supplier paper-code color; existing codes default to corrugated brown."""
from alembic import op
import sqlalchemy as sa

revision = "rw10v8x9z71"
down_revision = "rv10v8x9z70"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("supplier_paper_codes", sa.Column("color", sa.String(10), nullable=False, server_default="kraft"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM supplier_paper_codes WHERE color <> 'kraft' LIMIT 1")).first():
        raise RuntimeError("Paper color facts exist; restore a verified backup instead")
    op.drop_column("supplier_paper_codes", "color")
