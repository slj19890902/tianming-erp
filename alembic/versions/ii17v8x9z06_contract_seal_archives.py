"""add controlled contract seals and immutable sealed PDF archives

Revision ID: ii17v8x9z06
Revises: hh16v8x9z05
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ii17v8x9z06"
down_revision = "hh16v8x9z05"
branch_labels = None
depends_on = None


def _immutable(table: str) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER trg_{table}_{action.lower()}_immutable
                    BEFORE {action} ON {table}
                    BEGIN
                        SELECT RAISE(ABORT, '{table} rows are immutable');
                    END
                    """
                )
            )


def upgrade() -> None:
    op.create_table(
        "contract_seal_asset_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(200), nullable=False),
        sa.Column("mime_type", sa.String(50), nullable=False),
        sa.Column("png_content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_snapshot", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_contract_seal_asset_versions_version"),
        sa.CheckConstraint("width_px > 0 AND height_px > 0", name="ck_contract_seal_asset_versions_dimensions"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version", name="uq_contract_seal_asset_versions_version"),
        sa.UniqueConstraint("sha256", name="uq_contract_seal_asset_versions_sha256"),
    )
    op.create_table(
        "contract_seal_selection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_version_id", sa.Integer(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_operation_key", sa.String(120), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_by_snapshot", sa.String(100), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_contract_seal_selection_singleton"),
        sa.CheckConstraint("version >= 1", name="ck_contract_seal_selection_version"),
        sa.CheckConstraint("is_enabled = 0 OR asset_version_id IS NOT NULL", name="ck_contract_seal_selection_enabled_asset"),
        sa.ForeignKeyConstraint(["asset_version_id"], ["contract_seal_asset_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("last_operation_key", name="uq_contract_seal_selection_last_operation_key"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "contract_sealed_pdf_archives",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("contract_id", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_status", sa.String(20), nullable=False),
        sa.Column("contract_no_snapshot", sa.String(40), nullable=False),
        sa.Column("customer_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("customer_name_snapshot", sa.String(250), nullable=False),
        sa.Column("template_version", sa.String(50), nullable=False),
        sa.Column("seal_asset_version_id", sa.Integer(), nullable=False),
        sa.Column("seal_asset_version", sa.Integer(), nullable=False),
        sa.Column("seal_selection_version", sa.Integer(), nullable=False),
        sa.Column("seal_sha256", sa.String(64), nullable=False),
        sa.Column("pdf_content", sa.LargeBinary(), nullable=False),
        sa.Column("pdf_sha256", sa.String(64), nullable=False),
        sa.Column("download_filename", sa.String(240), nullable=False),
        sa.Column("operation_key", sa.String(120), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_username_snapshot", sa.String(100), nullable=False),
        sa.Column("actor_role_snapshot", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("contract_version >= 1", name="ck_contract_sealed_pdf_archives_contract_version"),
        sa.CheckConstraint("contract_status IN ('confirmed','converted')", name="ck_contract_sealed_pdf_archives_status"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["contract_id"], ["customer_contracts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["seal_asset_version_id"], ["contract_seal_asset_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_key", name="uq_contract_sealed_pdf_archives_operation_key"),
    )
    op.create_index(
        "ix_contract_sealed_pdf_archives_contract_time",
        "contract_sealed_pdf_archives",
        ["contract_id", "created_at"],
    )
    _immutable("contract_seal_asset_versions")
    _immutable("contract_sealed_pdf_archives")


def downgrade() -> None:
    connection = op.get_bind()
    facts = sum(
        int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() or 0)
        for table in (
            "contract_seal_asset_versions",
            "contract_seal_selection",
            "contract_sealed_pdf_archives",
        )
    )
    if facts:
        raise RuntimeError(
            "P1-35B 已存在印章版本、启用状态或带章合同归档，拒绝破坏性降级；请恢复升级前完整备份。"
        )
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for table in ("contract_seal_asset_versions", "contract_sealed_pdf_archives"):
            for action in ("update", "delete"):
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_{action}_immutable"))
    op.drop_index("ix_contract_sealed_pdf_archives_contract_time", table_name="contract_sealed_pdf_archives")
    op.drop_table("contract_sealed_pdf_archives")
    op.drop_table("contract_seal_selection")
    op.drop_table("contract_seal_asset_versions")
