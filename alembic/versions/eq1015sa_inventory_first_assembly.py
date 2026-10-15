"""Allow audited assembly commands to originate from qualified inventory lots."""
from alembic import op
import sqlalchemy as sa

revision = "eq1015sa"
down_revision = "ep1010md"
branch_labels = None
depends_on = None

_COMMAND_TRIGGERS = {
    f"stock_preparation_commands_no_{action.lower()}": (
        f"CREATE TRIGGER stock_preparation_commands_no_{action.lower()} "
        f"BEFORE {action} ON stock_preparation_commands "
        "BEGIN SELECT RAISE(ABORT, '备库安排流水不可修改或删除'); END"
    )
    for action in ("UPDATE", "DELETE")
}


def _alter_receipt_item(*, nullable):
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        existing = dict(bind.execute(sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'stock_preparation_commands'"
        )).all())
        if any(existing.get(name) != sql for name, sql in _COMMAND_TRIGGERS.items()):
            raise RuntimeError("stock preparation command protection triggers changed")

    with op.batch_alter_table("stock_preparation_commands") as batch:
        batch.alter_column("receipt_item_id", existing_type=sa.Integer(), nullable=nullable)

    if bind.dialect.name == "sqlite":
        for sql in _COMMAND_TRIGGERS.values():
            op.execute(sql)
        recreated = dict(bind.execute(sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'stock_preparation_commands'"
        )).all())
        if any(recreated.get(name) != sql for name, sql in _COMMAND_TRIGGERS.items()):
            raise RuntimeError("stock preparation command protection triggers not restored")


def upgrade():
    _alter_receipt_item(nullable=True)


def downgrade():
    # A deployed inventory-first command intentionally has no receipt item and
    # cannot be losslessly converted back.  Operators must not invent one.
    count = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM stock_preparation_commands WHERE receipt_item_id IS NULL"
    )).scalar()
    if count:
        raise RuntimeError("inventory-first assembly commands exist; downgrade is not lossless")
    _alter_receipt_item(nullable=False)
