"""Add dedicated editable mold-label content without touching product facts."""

from alembic import op
import sqlalchemy as sa


revision = "ed0928ml"
down_revision = "ec0927xl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mold_tools", sa.Column("label_overrides_json", sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT 1 FROM mold_tools WHERE label_overrides_json IS NOT NULL LIMIT 1")
    ).first():
        raise RuntimeError(
            "已有模具标签专用内容，禁止降级；保留数据库，使用支持当前标签配置的程序向前修复"
        )
    op.drop_column("mold_tools", "label_overrides_json")
