"""Join factory goods applicability and immutable BOM source revisions."""
from alembic import op
import sqlalchemy as sa

revision = "sk23v8x9z85"
down_revision = ("sj22v8x9z84", "rv10v8x9z70")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    # A lower BOM revision would reject these facts. Refuse before changing
    # even the merge-head metadata during a multi-revision downgrade.
    if op.get_bind().execute(sa.text("SELECT 1 FROM order_bom_rule_revisions LIMIT 1")).scalar() is not None:
        raise RuntimeError("已有BOM规则版本事实，须恢复已验证的完整备份，不得降级删除事实")
