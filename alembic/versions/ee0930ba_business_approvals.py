"""Scoped business requests; no historical fact backfill."""
from alembic import op
import sqlalchemy as sa
revision="ee0930ba"
down_revision="ed0928ml"
branch_labels=None
depends_on=None

def upgrade():
    op.create_table("business_approvals",
        sa.Column("id",sa.Integer(),primary_key=True),
        sa.Column("applicant_id",sa.Integer(),sa.ForeignKey("users.id"),nullable=False),
        sa.Column("customer_id",sa.Integer(),sa.ForeignKey("customers.id"),nullable=False),
        sa.Column("action",sa.String(50),nullable=False),sa.Column("target_id",sa.Integer()),
        sa.Column("payload_json",sa.Text(),nullable=False),sa.Column("before_json",sa.Text(),nullable=False),sa.Column("basis_hash",sa.String(64),nullable=False),
        sa.Column("request_hash",sa.String(64),nullable=False),sa.Column("idempotency_key",sa.String(160),nullable=False),
        sa.Column("note",sa.Text(),nullable=False),sa.Column("status",sa.String(20),nullable=False),sa.Column("version",sa.Integer(),nullable=False),
        sa.Column("reviewer_id",sa.Integer(),sa.ForeignKey("users.id")),sa.Column("decision_note",sa.Text()),sa.Column("result_json",sa.Text()),
        sa.Column("created_at",sa.DateTime(),nullable=False,server_default=sa.func.current_timestamp()),sa.Column("reviewed_at",sa.DateTime()),
        sa.UniqueConstraint("applicant_id","idempotency_key",name="uq_business_approval_request"),
        sa.CheckConstraint("status IN ('pending','applied','rejected','withdrawn')",name="ck_business_approval_status"))
    for name in ("applicant_id","customer_id","status"): op.create_index("ix_business_approvals_"+name,"business_approvals",[name])
    op.execute("""CREATE TRIGGER business_approval_payload_immutable
        BEFORE UPDATE OF applicant_id, customer_id, action, target_id, payload_json, before_json,
        basis_hash, request_hash, idempotency_key, note, created_at ON business_approvals
        BEGIN SELECT RAISE(ABORT, 'business approval request is immutable'); END""")
    op.execute("""CREATE TRIGGER business_approval_no_delete BEFORE DELETE ON business_approvals
        BEGIN SELECT RAISE(ABORT, 'business approval history cannot be deleted'); END""")

def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM business_approvals LIMIT 1")).first():
        raise RuntimeError("已有业务申请历史，禁止有损降级；请保留数据向前修复")
    op.drop_table("business_approvals")
