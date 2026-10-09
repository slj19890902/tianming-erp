"""Private contract seals and immutable use receipts; no business backfill."""
from alembic import op
import sqlalchemy as sa

revision = "ek1009cs"
down_revision = "ej1009sm"
branch_labels = depends_on = None


def upgrade():
    op.create_table("contract_seals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("image_png", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("size_mm", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False))
    op.create_table("contract_seal_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active_seal_id", sa.Integer(), sa.ForeignKey("contract_seals.id", ondelete="RESTRICT")),
        sa.CheckConstraint("id = 1 AND version >= 0", name="ck_contract_seal_state_singleton"))
    op.create_table("contract_sealed_exports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_key", sa.String(100), nullable=False, unique=True),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("contract_id", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("seal_id", sa.Integer(), sa.ForeignKey("contract_seals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("pdf_content", sa.LargeBinary(), nullable=False),
        sa.Column("pdf_sha256", sa.String(64), nullable=False),
        sa.Column("template_version", sa.String(40), nullable=False),
        sa.Column("font_sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(250), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False))
    op.create_index("ix_contract_sealed_exports_contract_id", "contract_sealed_exports", ["contract_id"])
    for table in ("contract_seals", "contract_sealed_exports"):
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                       "BEGIN SELECT RAISE(ABORT, 'contract seal facts are immutable'); END")


def downgrade():
    for table in ("contract_sealed_exports", "contract_seals", "contract_seal_state"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("已有电子章或用章记录，禁止有损降级；保留记录向前修复")
    for table in ("contract_sealed_exports", "contract_seals"):
        for action in ("update", "delete"):
            op.execute(f"DROP TRIGGER {table}_no_{action}")
    op.drop_table("contract_sealed_exports")
    op.drop_table("contract_seal_state")
    op.drop_table("contract_seals")
