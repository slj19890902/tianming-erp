"""Freeze customer and sender headers for newly created delivery documents."""
from alembic import op
import sqlalchemy as sa

revision = "dc0920"
down_revision = "db0919"
branch_labels = None
depends_on = None


COLUMNS = (
    ("print_snapshot_version", sa.Integer()),
    ("customer_name_snapshot", sa.String(length=250)),
    ("customer_contact_snapshot", sa.String(length=100)),
    ("customer_phone_snapshot", sa.String(length=100)),
    ("customer_address_snapshot", sa.Text()),
    ("sender_company_name_snapshot", sa.String(length=100)),
    ("sender_address_snapshot", sa.String(length=200)),
    ("sender_phone_snapshot", sa.String(length=50)),
    ("sender_fax_snapshot", sa.String(length=50)),
    ("sender_tax_number_snapshot", sa.String(length=50)),
    ("sender_bank_name_snapshot", sa.String(length=100)),
    ("sender_bank_account_snapshot", sa.String(length=50)),
    ("sender_contact_snapshot", sa.String(length=50)),
    ("sender_contact_phone_snapshot", sa.String(length=50)),
)


def upgrade():
    for name, column_type in COLUMNS:
        op.add_column("sales_deliveries", sa.Column(name, column_type, nullable=True))


def downgrade():
    db = op.get_bind()
    if db.execute(sa.text(
        "SELECT 1 FROM sales_deliveries WHERE print_snapshot_version IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError(
            "已有送货打印快照事实，禁止删除；请使用已验证恢复备份"
        )
    with op.batch_alter_table("sales_deliveries") as batch:
        for name, _column_type in reversed(COLUMNS):
            batch.drop_column(name)
