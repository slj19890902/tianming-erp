"""Company-owned seals and contract issuer snapshots; no historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = "el1009cp"
down_revision = "ek1009cs"
branch_labels = depends_on = None


def upgrade():
    op.create_table("company_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_name", sa.String(100), nullable=False, unique=True),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("seal_version", sa.Integer(), nullable=False),
        sa.Column("active_seal_id", sa.Integer(), sa.ForeignKey("contract_seals.id", ondelete="RESTRICT")),
        sa.CheckConstraint("version >= 0 AND seal_version >= 0", name="ck_company_profiles_versions"))
    op.create_table("company_selection",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active_company_id", sa.Integer(), sa.ForeignKey("company_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("legacy_details_json", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1 AND version >= 0", name="ck_company_selection_singleton"))
    op.create_table("contract_company_snapshots",
        sa.Column("contract_id", sa.Integer(), sa.ForeignKey("customer_contracts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("company_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False))
    op.execute("CREATE TRIGGER company_selection_legacy_immutable BEFORE UPDATE OF legacy_details_json ON company_selection "
               "WHEN NEW.legacy_details_json IS NOT OLD.legacy_details_json "
               "BEGIN SELECT RAISE(ABORT, 'legacy issuer snapshot is immutable'); END")
    op.execute("CREATE TRIGGER contract_company_snapshot_immutable BEFORE UPDATE ON contract_company_snapshots "
               "BEGIN SELECT RAISE(ABORT, 'contract issuer snapshot is immutable'); END")


def downgrade():
    for table in ("contract_company_snapshots", "company_selection", "company_profiles"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("已有公司抬头或合同抬头快照，禁止有损降级；保留数据向前修复")
    op.execute("DROP TRIGGER contract_company_snapshot_immutable")
    op.execute("DROP TRIGGER company_selection_legacy_immutable")
    op.drop_table("contract_company_snapshots")
    op.drop_table("company_selection")
    op.drop_table("company_profiles")
