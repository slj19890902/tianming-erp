"""N016 P1: governed PDF template lifecycle and reviewed gold samples.

Revision ID: bc56v8x9z46
Revises: bb55v8x9z45

The temporary baseline table is migration metadata, not application data.  It
lets downgrade distinguish an untouched legacy mapping from lifecycle facts
that would be lost by removing the new columns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "bc56v8x9z46"
down_revision: Union[str, Sequence[str], None] = "bb55v8x9z45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TEMPLATE_TABLE = "pdf_order_customer_templates"
SAMPLE_TABLE = "pdf_order_training_samples"
BASELINE_TABLE = "pdf_order_template_lifecycle_legacy_baseline"
ACTIVE_INDEX = "uq_pdf_templates_one_active_customer"
VERSION_UNIQUE = "uq_pdf_templates_customer_version"
STATUS_CHECK = "ck_pdf_template_status"
VERSION_CHECK = "ck_pdf_template_version"
SUPERSEDES_FK = "fk_pdf_templates_supersedes_template_id"
GOLD_CHECK = "ck_pdf_sample_gold_review_status"


def _assert_no_legacy_multi_active(connection: sa.Connection) -> None:
    rows = connection.execute(
        sa.text(
            f"""
            SELECT customer_id, COUNT(*) AS active_count
            FROM {TEMPLATE_TABLE}
            WHERE is_active IS TRUE
            GROUP BY customer_id
            HAVING COUNT(*) > 1
            """
        )
    ).mappings().all()
    if rows:
        rendered = ", ".join(
            f"customer_id={row['customer_id']!r}: {row['active_count']} active rows"
            for row in rows
        )
        raise RuntimeError(
            "PDF 客户模板存在同客户多个旧 is_active=true 记录；"
            "N016 P1 迁移已 fail-closed，必须先人工处置，不会静默挑选。 "
            + rendered
        )


def _assign_legacy_versions(connection: sa.Connection) -> None:
    rows = connection.execute(
        sa.text(
            f"SELECT id, customer_id FROM {TEMPLATE_TABLE} "
            "ORDER BY customer_id ASC, created_at ASC, id ASC"
        )
    ).mappings().all()
    versions: dict[object, int] = {}
    for row in rows:
        customer_key = row["customer_id"]
        version = versions.get(customer_key, 0) + 1
        versions[customer_key] = version
        connection.execute(
            sa.text(f"UPDATE {TEMPLATE_TABLE} SET version = :version WHERE id = :id"),
            {"version": version, "id": row["id"]},
        )


def _record_legacy_baseline(connection: sa.Connection) -> None:
    rows = connection.execute(
        sa.text(
            f"SELECT id, customer_id, is_active, status, version FROM {TEMPLATE_TABLE} ORDER BY id"
        )
    ).mappings().all()
    if rows:
        connection.execute(
            sa.text(
                f"""
                INSERT INTO {BASELINE_TABLE}
                    (template_id, customer_id, is_active, status, version)
                VALUES (:template_id, :customer_id, :is_active, :status, :version)
                """
            ),
            [
                {
                    "template_id": row["id"],
                    "customer_id": row["customer_id"],
                    "is_active": row["is_active"],
                    "status": row["status"],
                    "version": row["version"],
                }
                for row in rows
            ],
        )


def upgrade() -> None:
    connection = op.get_bind()
    _assert_no_legacy_multi_active(connection)

    with op.batch_alter_table(SAMPLE_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                "gold_review_status",
                sa.String(length=20),
                nullable=False,
                server_default=sa.text("'pending'"),
            )
        )
        batch_op.add_column(sa.Column("gold_reviewed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("gold_reviewed_by", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("gold_review_note", sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            GOLD_CHECK,
            "gold_review_status IN ('pending', 'approved', 'rejected')",
        )

    with op.batch_alter_table(TEMPLATE_TABLE) as batch_op:
        batch_op.add_column(sa.Column("status", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("supersedes_template_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("updated_by", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("activated_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("activated_by", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("activation_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("evidence_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("retired_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("retired_by", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("retired_reason", sa.Text(), nullable=True))

    # No old reviewed status is promoted.  The non-null server default above
    # turns every existing sample into an explicitly pending gold review.
    # A legacy inactive row has no publish provenance.  Treating it as a new
    # editable draft would invent authority, so preserve it as retired.
    connection.execute(
        sa.text(
            f"UPDATE {TEMPLATE_TABLE} "
            "SET status = CASE WHEN is_active IS TRUE THEN 'active' ELSE 'retired' END"
        )
    )
    _assign_legacy_versions(connection)

    with op.batch_alter_table(TEMPLATE_TABLE) as batch_op:
        batch_op.alter_column(
            "status", existing_type=sa.String(length=20), nullable=False,
            server_default=sa.text("'draft'"),
        )
        batch_op.alter_column("version", existing_type=sa.Integer(), nullable=False)
        batch_op.create_check_constraint(
            STATUS_CHECK, "status IN ('draft', 'active', 'retired')"
        )
        batch_op.create_check_constraint(VERSION_CHECK, "version >= 1")
        batch_op.create_unique_constraint(VERSION_UNIQUE, ["customer_id", "version"])
        batch_op.create_foreign_key(
            SUPERSEDES_FK,
            TEMPLATE_TABLE,
            ["supersedes_template_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    op.create_index(
        ACTIVE_INDEX,
        TEMPLATE_TABLE,
        ["customer_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active' AND customer_id IS NOT NULL"),
        postgresql_where=sa.text("status = 'active' AND customer_id IS NOT NULL"),
    )
    op.create_index(
        "ix_pdf_samples_gold_review_status", SAMPLE_TABLE, ["gold_review_status"]
    )
    op.create_index(
        "ix_pdf_templates_supersedes_template_id",
        TEMPLATE_TABLE,
        ["supersedes_template_id"],
    )
    op.create_table(
        BASELINE_TABLE,
        sa.Column("template_id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    _record_legacy_baseline(connection)


def _assert_downgrade_is_lossless(connection: sa.Connection) -> None:
    missing_baseline = connection.execute(
        sa.text(
            f"""
            SELECT COUNT(*) FROM {TEMPLATE_TABLE} AS template
            LEFT JOIN {BASELINE_TABLE} AS baseline
              ON baseline.template_id = template.id
            WHERE baseline.template_id IS NULL
            """
        )
    ).scalar_one()
    changed_baseline = connection.execute(
        sa.text(
            f"""
            SELECT COUNT(*) FROM {TEMPLATE_TABLE} AS template
            JOIN {BASELINE_TABLE} AS baseline
              ON baseline.template_id = template.id
            WHERE COALESCE(template.customer_id, -1) <> COALESCE(baseline.customer_id, -1)
               OR template.is_active <> baseline.is_active
               OR template.status <> baseline.status
               OR template.version <> baseline.version
               OR template.supersedes_template_id IS NOT NULL
               OR template.updated_by IS NOT NULL
               OR template.activated_at IS NOT NULL
               OR template.activated_by IS NOT NULL
               OR template.activation_reason IS NOT NULL
               OR template.evidence_json IS NOT NULL
               OR template.retired_at IS NOT NULL
               OR template.retired_by IS NOT NULL
               OR template.retired_reason IS NOT NULL
            """
        )
    ).scalar_one()
    missing_legacy = connection.execute(
        sa.text(
            f"""
            SELECT COUNT(*) FROM {BASELINE_TABLE} AS baseline
            LEFT JOIN {TEMPLATE_TABLE} AS template
              ON template.id = baseline.template_id
            WHERE template.id IS NULL
            """
        )
    ).scalar_one()
    gold_facts = connection.execute(
        sa.text(
            f"""
            SELECT COUNT(*) FROM {SAMPLE_TABLE}
            WHERE gold_review_status <> 'pending'
               OR gold_reviewed_at IS NOT NULL
               OR gold_reviewed_by IS NOT NULL
               OR gold_review_note IS NOT NULL
            """
        )
    ).scalar_one()
    if missing_baseline or changed_baseline or missing_legacy or gold_facts:
        raise RuntimeError(
            "N016 P1 生命周期或金样本复核事实已产生，禁止破坏性降级；"
            "请停止服务并恢复 bc56 升级前的完整数据库备份。"
        )


def downgrade() -> None:
    connection = op.get_bind()
    _assert_downgrade_is_lossless(connection)

    op.drop_index("ix_pdf_templates_supersedes_template_id", table_name=TEMPLATE_TABLE)
    op.drop_index(ACTIVE_INDEX, table_name=TEMPLATE_TABLE)
    with op.batch_alter_table(TEMPLATE_TABLE) as batch_op:
        batch_op.drop_constraint(SUPERSEDES_FK, type_="foreignkey")
        batch_op.drop_constraint(VERSION_UNIQUE, type_="unique")
        batch_op.drop_constraint(VERSION_CHECK, type_="check")
        batch_op.drop_constraint(STATUS_CHECK, type_="check")
        batch_op.drop_column("retired_reason")
        batch_op.drop_column("retired_by")
        batch_op.drop_column("retired_at")
        batch_op.drop_column("evidence_json")
        batch_op.drop_column("activation_reason")
        batch_op.drop_column("activated_by")
        batch_op.drop_column("activated_at")
        batch_op.drop_column("updated_by")
        batch_op.drop_column("supersedes_template_id")
        batch_op.drop_column("version")
        batch_op.drop_column("status")

    op.drop_index("ix_pdf_samples_gold_review_status", table_name=SAMPLE_TABLE)
    with op.batch_alter_table(SAMPLE_TABLE) as batch_op:
        batch_op.drop_constraint(GOLD_CHECK, type_="check")
        batch_op.drop_column("gold_review_note")
        batch_op.drop_column("gold_reviewed_by")
        batch_op.drop_column("gold_reviewed_at")
        batch_op.drop_column("gold_review_status")
    op.drop_table(BASELINE_TABLE)
