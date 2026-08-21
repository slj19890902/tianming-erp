"""add mold multi-customer identity and hidden stable master facts

Revision ID: yy33v8x9z22
Revises: vv30v8x9z19
Create Date: 2026-08-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "yy33v8x9z22"
down_revision = "vv30v8x9z19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mold_tools") as batch:
        batch.add_column(sa.Column("label_name", sa.String(length=200), nullable=True))
        batch.add_column(
            sa.Column("chinese_short_name", sa.String(length=100), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "identity_status",
                sa.String(length=20),
                nullable=False,
                server_default="legacy_unset",
            )
        )
        batch.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.create_check_constraint(
            "ck_mold_tools_version",
            "version >= 1",
        )
        batch.create_check_constraint(
            "ck_mold_tools_identity_status",
            "identity_status IN ('legacy_unset', 'frozen')",
        )
        batch.create_check_constraint(
            "ck_mold_tools_identity_fields",
            "((identity_status = 'legacy_unset' AND label_name IS NULL) OR "
            "(identity_status = 'frozen' AND label_name IS NOT NULL "
            "AND length(trim(label_name)) > 0))",
        )

    op.create_table(
        "mold_tool_customers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mold_tool_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["mold_tool_id"], ["mold_tools.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "mold_tool_id",
            "customer_id",
            name="uq_mold_tool_customers_mold_customer",
        ),
        sa.UniqueConstraint(
            "mold_tool_id",
            "display_order",
            name="uq_mold_tool_customers_mold_display_order",
        ),
        sa.CheckConstraint(
            "display_order IS NULL OR display_order IN (1, 2)",
            name="ck_mold_tool_customers_display_order",
        ),
    )
    op.create_index(
        "ix_mold_tool_customers_customer_mold",
        "mold_tool_customers",
        ["customer_id", "mold_tool_id"],
    )

    op.create_table(
        "mold_master_mutations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mold_tool_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column("result_snapshot_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["mold_tool_id"], ["mold_tools.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_mold_master_mutations_key"
        ),
        sa.CheckConstraint(
            "action IN ('create', 'update')",
            name="ck_mold_master_mutations_action",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_mold_master_mutations_request_hash",
        ),
        sa.CheckConstraint(
            "result_version >= 1",
            name="ck_mold_master_mutations_result_version",
        ),
    )
    op.create_index(
        "ix_mold_master_mutations_mold_time",
        "mold_master_mutations",
        ["mold_tool_id", "created_at"],
    )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            INSERT INTO mold_tool_customers (
                mold_tool_id,
                customer_id,
                display_order,
                created_by
            )
            SELECT DISTINCT
                product.mold_tool_id,
                product.customer_id,
                CASE
                    WHEN (
                        SELECT COUNT(DISTINCT sibling.customer_id)
                        FROM products AS sibling
                        WHERE sibling.mold_tool_id = product.mold_tool_id
                    ) = 1 THEN 1
                    ELSE NULL
                END,
                NULL
            FROM products AS product
            WHERE product.mold_tool_id IS NOT NULL
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_mold_master_mutations_no_update
            BEFORE UPDATE ON mold_master_mutations
            BEGIN
                SELECT RAISE(ABORT, 'mold master mutation facts are immutable');
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_mold_master_mutations_no_delete
            BEFORE DELETE ON mold_master_mutations
            BEGIN
                SELECT RAISE(ABORT, 'mold master mutation facts are immutable');
            END
            """
        )
    )


def _assert_safe_downgrade() -> None:
    connection = op.get_bind()
    frozen = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM mold_tools WHERE identity_status = 'frozen'"
            )
        ).scalar_one()
    )
    mutations = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM mold_master_mutations")
        ).scalar_one()
    )
    changed_links = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*) FROM (
                    SELECT mold_tool_id, customer_id
                    FROM mold_tool_customers
                    EXCEPT
                    SELECT DISTINCT mold_tool_id, customer_id
                    FROM products
                    WHERE mold_tool_id IS NOT NULL
                )
                """
            )
        ).scalar_one()
    )
    missing_links = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT mold_tool_id, customer_id
                    FROM products
                    WHERE mold_tool_id IS NOT NULL
                    EXCEPT
                    SELECT mold_tool_id, customer_id
                    FROM mold_tool_customers
                )
                """
            )
        ).scalar_one()
    )
    if frozen or mutations or changed_links or missing_links:
        raise RuntimeError(
            "cannot downgrade after P1-82 mold identity or customer facts exist"
        )


def downgrade() -> None:
    _assert_safe_downgrade()
    connection = op.get_bind()
    connection.execute(sa.text("DROP TRIGGER trg_mold_master_mutations_no_delete"))
    connection.execute(sa.text("DROP TRIGGER trg_mold_master_mutations_no_update"))
    op.drop_index(
        "ix_mold_master_mutations_mold_time",
        table_name="mold_master_mutations",
    )
    op.drop_table("mold_master_mutations")
    op.drop_index(
        "ix_mold_tool_customers_customer_mold",
        table_name="mold_tool_customers",
    )
    op.drop_table("mold_tool_customers")
    with op.batch_alter_table("mold_tools") as batch:
        batch.drop_constraint("ck_mold_tools_identity_fields", type_="check")
        batch.drop_constraint("ck_mold_tools_identity_status", type_="check")
        batch.drop_constraint("ck_mold_tools_version", type_="check")
        batch.drop_column("version")
        batch.drop_column("identity_status")
        batch.drop_column("chinese_short_name")
        batch.drop_column("label_name")
