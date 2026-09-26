"""Retain voided invoice sources in an audited cancelled-statement snapshot."""
from alembic import op
import sqlalchemy as sa

revision = 'ea0926'
down_revision = 'dz0922'
branch_labels = None
depends_on = None
TABLE = 'finance_invoice_task_items'
CHECK = ('(statement_item_id IS NOT NULL AND archived_statement_item_id IS NULL AND archived_adjustment_id IS NULL) OR '
         '(statement_item_id IS NULL AND archived_statement_item_id IS NOT NULL AND archived_statement_item_id > 0 AND archived_adjustment_id IS NOT NULL)')
TRIGGERS = {
    'trg_invoice_archive_insert_guard': '''CREATE TRIGGER trg_invoice_archive_insert_guard
        BEFORE INSERT ON finance_invoice_task_items WHEN NEW.archived_adjustment_id IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'invoice sources must be archived from a real live statement item'); END''',
    'trg_invoice_archive_source_guard': '''CREATE TRIGGER trg_invoice_archive_source_guard
        BEFORE UPDATE OF statement_item_id, archived_statement_item_id, archived_adjustment_id ON finance_invoice_task_items
        WHEN NEW.archived_adjustment_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN OLD.archived_adjustment_id IS NOT NULL OR NEW.statement_item_id IS NOT NULL
            OR NEW.archived_statement_item_id != OLD.statement_item_id
            OR NOT EXISTS (SELECT 1 FROM finance_invoice_tasks WHERE id=NEW.task_id AND status='voided')
            OR NOT EXISTS (SELECT 1 FROM finance_statement_adjustments a
                JOIN finance_statement_items s ON s.statement_id=a.statement_id
                WHERE a.id=NEW.archived_adjustment_id AND a.action='cancel_statement'
                  AND s.id=OLD.statement_item_id
                  AND EXISTS (SELECT 1 FROM json_each(a.details_json, '$.source_items') j
                              WHERE json_extract(j.value,'$.id')=s.id))
            THEN RAISE(ABORT, 'invoice source archive requires a voided task and its complete statement snapshot') END;
        END''',
    'trg_invoice_archive_snapshot_immutable': '''CREATE TRIGGER trg_invoice_archive_snapshot_immutable
        BEFORE UPDATE ON finance_statement_adjustments
        WHEN EXISTS (SELECT 1 FROM finance_invoice_task_items WHERE archived_adjustment_id=OLD.id)
        BEGIN SELECT RAISE(ABORT, 'archived invoice source snapshot is immutable'); END''',
}


def alter(up):
    bind = op.get_bind()
    triggers = []
    if bind.dialect.name == 'sqlite':
        if bind.exec_driver_sql('PRAGMA foreign_key_check').first():
            raise RuntimeError('Invoice archive migration requires intact foreign keys')
        triggers = list(bind.execute(sa.text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql LIKE '%finance_invoice_task_items%'")))
        for name, sql in triggers:
            bind.exec_driver_sql('DROP TRIGGER "' + name.replace('"','""') + '"')
    with op.batch_alter_table(TABLE) as batch:
        batch.alter_column('statement_item_id', existing_type=sa.Integer(), nullable=up)
        if up:
            batch.add_column(sa.Column('archived_statement_item_id', sa.Integer(), nullable=True))
            batch.add_column(sa.Column('archived_adjustment_id', sa.Integer(), nullable=True))
            batch.create_foreign_key('fk_invoice_task_item_archive', 'finance_statement_adjustments', ['archived_adjustment_id'], ['id'], ondelete='RESTRICT')
            batch.create_check_constraint('ck_invoice_task_items_live_or_archived_source', CHECK)
        else:
            batch.drop_constraint('ck_invoice_task_items_live_or_archived_source', type_='check')
            batch.drop_constraint('fk_invoice_task_item_archive', type_='foreignkey')
            batch.drop_column('archived_adjustment_id')
            batch.drop_column('archived_statement_item_id')
    for name, sql in triggers:
        if name not in TRIGGERS:
            bind.exec_driver_sql(sql)
    if up and bind.dialect.name == 'sqlite':
        for sql in TRIGGERS.values():
            bind.exec_driver_sql(sql)
    if bind.dialect.name == 'sqlite' and bind.exec_driver_sql('PRAGMA foreign_key_check').first():
        raise RuntimeError('Invoice archive migration foreign key check failed')


def upgrade():
    alter(True)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM finance_invoice_task_items WHERE archived_adjustment_id IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('Archived invoice source facts exist; refuse destructive downgrade')
    alter(False)
