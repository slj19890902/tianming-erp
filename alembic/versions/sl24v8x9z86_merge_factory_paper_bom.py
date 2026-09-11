"""Join factory supplier paper colors with the BOM candidate history."""
from alembic import op
import sqlalchemy as sa

revision = "sl24v8x9z86"
down_revision = ("sk23v8x9z85", "rw10v8x9z71")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    # Fail before the merge revision metadata changes, not halfway through a
    # multi-revision downgrade whose lower migration would reject these facts.
    db = op.get_bind()
    if db.execute(sa.text("SELECT 1 FROM order_bom_rule_revisions LIMIT 1")).scalar() is not None:
        raise RuntimeError("已有BOM规则版本事实，须恢复已验证的完整备份，不得降级删除事实")
    if db.execute(sa.text("SELECT 1 FROM supplier_paper_codes WHERE color <> 'kraft' LIMIT 1")).scalar() is not None:
        raise RuntimeError("已有供应商纸种颜色事实，须恢复已验证的完整备份，不得降级删除事实")
