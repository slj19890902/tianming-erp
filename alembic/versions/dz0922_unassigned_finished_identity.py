"""Allow fully described owner-unidentified physical finished inventory."""
from alembic import op
import sqlalchemy as sa

revision = 'dz0922'
down_revision = 'dy0922'
branch_labels = None
depends_on = None

CHECK = "product_id IS NOT NULL OR (is_general = 1 AND owner_customer_id IS NULL AND coalesce(length_mm,0)>0 AND coalesce(width_mm,0)>0 AND coalesce(height_mm,0)>0 AND physical_basis_json IS NOT NULL AND material_code_snapshot IS NOT NULL)"


def alter(nullable):
    bind = op.get_bind()
    tables = ('finished_goods_inventory_details', 'warehouse_ground_occupancies')
    triggers = []
    if bind.dialect.name == 'sqlite':
        if bind.exec_driver_sql('PRAGMA foreign_key_check').first():
            raise RuntimeError('入库身份迁移前外键不完整')
        triggers = [row for row in bind.execute(sa.text("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
            if any(table in (row.sql or '') for table in tables)]
        for name, sql in triggers:
            if not name.replace('_','').isalnum() or not sql:
                raise RuntimeError('不能保全原触发器定义')
            bind.exec_driver_sql(f'DROP TRIGGER "{name}"')
    with op.batch_alter_table(tables[0]) as batch:
        batch.alter_column('product_id', existing_type=sa.Integer(), nullable=nullable)
        if nullable:
            batch.create_check_constraint('ck_finished_unassigned_facts', CHECK)
        else:
            batch.drop_constraint('ck_finished_unassigned_facts', type_='check')
    with op.batch_alter_table(tables[1]) as batch:
        for column in ('customer_id','product_id'):
            batch.alter_column(column, existing_type=sa.Integer(), nullable=nullable)
        if nullable:
            batch.create_check_constraint('ck_ground_identity_pair', '(customer_id IS NULL) = (product_id IS NULL)')
        else:
            batch.drop_constraint('ck_ground_identity_pair', type_='check')
    for _, sql in triggers:
        bind.exec_driver_sql(sql)
    if bind.dialect.name == 'sqlite' and bind.exec_driver_sql('PRAGMA foreign_key_check').first():
        raise RuntimeError('入库身份迁移后外键检查失败')


def upgrade():
    alter(True)


def downgrade():
    bind = op.get_bind()
    for table in ('finished_goods_inventory_details','warehouse_ground_occupancies'):
        if bind.execute(sa.text(f'SELECT 1 FROM {table} WHERE product_id IS NULL LIMIT 1')).first():
            raise RuntimeError('已有客户待认领库存，禁止破坏事实降级；请使用经验证的升级前备份回退')
    alter(False)
