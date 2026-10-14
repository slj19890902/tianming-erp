"""Allow audited assembly commands to originate from qualified inventory lots."""
from alembic import op
import sqlalchemy as sa

revision = "eq1015sa"
down_revision = "ep1010md"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("stock_preparation_commands") as batch:
        batch.alter_column("receipt_item_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    # A deployed inventory-first command intentionally has no receipt item and
    # cannot be losslessly converted back.  Operators must not invent one.
    count = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM stock_preparation_commands WHERE receipt_item_id IS NULL"
    )).scalar()
    if count:
        raise RuntimeError("inventory-first assembly commands exist; downgrade is not lossless")
    with op.batch_alter_table("stock_preparation_commands") as batch:
        batch.alter_column("receipt_item_id", existing_type=sa.Integer(), nullable=False)
