"""Allow single-layer, unfluted sheet inventory without changing existing facts."""
from alembic import op
import sqlalchemy as sa

revision = "mr0914"
down_revision = "mq0912"
branch_labels = None
depends_on = None


def _change(single_layer):
    bind = op.get_bind()
    triggers = []
    if bind.dialect.name == "sqlite":
        if bind.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("原纸迁移前外键检查失败")
        triggers = list(bind.execute(sa.text(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='semi_finished_inventory_details' AND sql IS NOT NULL"
        )).scalars())
    with op.batch_alter_table("semi_finished_inventory_details") as batch:
        batch.drop_constraint("ck_semi_inventory_layer", type_="check")
        batch.drop_constraint("ck_semi_inventory_flute", type_="check")
        batch.create_check_constraint("ck_semi_inventory_layer",
            "layer_count IN (1,3,5,7)" if single_layer else "layer_count IN (3,5,7)")
        batch.create_check_constraint("ck_semi_inventory_flute",
            ("(layer_count=1 AND flute_type='NONE') OR " if single_layer else "") +
            "(layer_count=3 AND flute_type IN ('A','B','E')) OR "
            "(layer_count=5 AND flute_type IN ('AB','BE')) OR "
            "(layer_count=7 AND flute_type IN ('AAA','ABC'))")
    for sql in triggers:
        bind.exec_driver_sql(sql)
    if bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("原纸迁移后外键检查失败")


def upgrade():
    _change(True)


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM semi_finished_inventory_details WHERE layer_count=1 LIMIT 1"
    )).first():
        raise RuntimeError("已有单层原纸库存，拒绝有损降级；请使用完整时点备份回滚")
    _change(False)
