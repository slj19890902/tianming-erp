"""Background PDF recognition staging only."""
from alembic import op
import sqlalchemy as sa
revision = 'st32v8x9z94'
down_revision = 'ss31v8x9z93'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('email_pdf_recognitions',
        sa.Column('sha256', sa.String(64), primary_key=True),
        sa.Column('attachment_id', sa.Integer(), sa.ForeignKey('email_intake_attachments.id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('draft_json', sa.Text(), nullable=True),
        sa.Column('error_code', sa.String(80), nullable=True),
        sa.Column('recognized_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('ready','improve')", name='ck_email_pdf_recognition_status'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM email_pdf_recognitions')).scalar():
        raise RuntimeError('已有PDF识别记录，禁止降级删除；请使用已验证备份恢复')
    op.drop_table('email_pdf_recognitions')
