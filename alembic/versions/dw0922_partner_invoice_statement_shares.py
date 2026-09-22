"""Link one partner invoice task to independent, immutable statement shares."""
from alembic import op
import sqlalchemy as sa

revision = "dw0922"
down_revision = "dv0922"
branch_labels = None
depends_on = None
TABLE = "finance_invoice_task_statements"


def upgrade():
    op.create_table(TABLE,
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("finance_invoice_tasks.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("statement_id", sa.Integer(), sa.ForeignKey("finance_statements.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("statement_version", sa.Integer(), nullable=False),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.CheckConstraint("statement_version >= 1 AND total_amount > 0", name="ck_invoice_task_statement_values"),
        sa.CheckConstraint("abs(net_amount + tax_amount - total_amount) < 0.005", name="ck_invoice_task_statement_balance"))
    op.create_index("ix_invoice_task_statements_statement", TABLE, ["statement_id"])
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER {TABLE}_no_{action.lower()} BEFORE {action} ON {TABLE} "
                       "BEGIN SELECT RAISE(ABORT, 'Frozen invoice statement shares are immutable'); END")


def downgrade():
    if op.get_bind().execute(sa.text(f"SELECT 1 FROM {TABLE} LIMIT 1")).first():
        raise RuntimeError("Invoice statement shares exist; refuse destructive downgrade")
    op.drop_table(TABLE)
