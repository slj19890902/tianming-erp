"""freeze purchase-purpose allocation by physical source

Revision ID: ww31v8x9z20
Revises: zz34v8x9z23
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ww31v8x9z20"
down_revision = "zz34v8x9z23"
branch_labels = None
depends_on = None


TABLE = "purchase_purpose_source_snapshots"
DOWNGRADE_BLOCKED_MESSAGE = (
    "P1-80 已存在采购用途冻结或幂等事实，拒绝破坏性降级"
)
SQLITE_UPDATE_TRIGGER = "trg_purchase_purpose_source_snapshots_immutable_update"
SQLITE_DELETE_TRIGGER = "trg_purchase_purpose_source_snapshots_immutable_delete"
POSTGRES_TRIGGER = "trg_purchase_purpose_source_snapshots_immutable"
POSTGRES_FUNCTION = "purchase_purpose_source_snapshots_immutable"


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER {SQLITE_UPDATE_TRIGGER}
            BEFORE UPDATE ON {TABLE}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, '{TABLE} rows are immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {SQLITE_DELETE_TRIGGER}
            BEFORE DELETE ON {TABLE}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, '{TABLE} rows are immutable');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {POSTGRES_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION '{TABLE} rows are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {POSTGRES_TRIGGER}
            BEFORE UPDATE OR DELETE ON {TABLE}
            FOR EACH ROW EXECUTE FUNCTION {POSTGRES_FUNCTION}()
            """
        )


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {SQLITE_DELETE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {SQLITE_UPDATE_TRIGGER}")
    elif dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {POSTGRES_TRIGGER} ON {TABLE}")
        op.execute(f"DROP FUNCTION IF EXISTS {POSTGRES_FUNCTION}()")


def _assert_safe_downgrade() -> None:
    connection = op.get_bind()
    snapshot_count = int(
        connection.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one()
    )
    supplier_request_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM supplier_requisition_orders "
                "WHERE request_hash IS NOT NULL OR request_actor_id IS NOT NULL"
            )
        ).scalar_one()
    )
    requisition_request_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM material_requisitions "
                "WHERE request_key IS NOT NULL OR request_hash IS NOT NULL "
                "OR request_actor_id IS NOT NULL"
            )
        ).scalar_one()
    )
    if snapshot_count or supplier_request_facts or requisition_request_facts:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)


def upgrade() -> None:
    # Existing formal rows deliberately remain legacy_unset: no purpose row is
    # created and none of the new nullable request facts are inferred/backfilled.
    with op.batch_alter_table("supplier_requisition_orders") as batch:
        batch.add_column(sa.Column("request_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("request_actor_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_supplier_requisition_orders_request_actor_id_users",
            "users",
            ["request_actor_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_supplier_requisition_orders_request_fact",
            "((request_hash IS NULL AND request_actor_id IS NULL) OR "
            "(request_key IS NOT NULL AND length(trim(request_key)) > 0 "
            "AND length(request_hash) = 64 AND request_actor_id IS NOT NULL))",
        )

    with op.batch_alter_table("material_requisitions") as batch:
        batch.add_column(sa.Column("request_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("request_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("request_actor_id", sa.Integer(), nullable=True))
        batch.create_unique_constraint(
            "uq_material_requisitions_request_key", ["request_key"]
        )
        batch.create_foreign_key(
            "fk_material_requisitions_request_actor_id_users",
            "users",
            ["request_actor_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_material_requisitions_request_fact",
            "((request_key IS NULL AND request_hash IS NULL "
            "AND request_actor_id IS NULL) OR "
            "(request_key IS NOT NULL AND length(trim(request_key)) > 0 "
            "AND length(request_hash) = 64 AND request_actor_id IS NOT NULL))",
        )

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("snapshot_key", sa.String(length=160), nullable=False),
        sa.Column("allocation_group_key", sa.String(length=160), nullable=False),
        sa.Column("supplier_requisition_order_item_id", sa.Integer(), nullable=True),
        sa.Column("material_requisition_item_id", sa.Integer(), nullable=True),
        sa.Column("source_kind", sa.String(length=30), nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("source_order_item_id", sa.Integer(), nullable=True),
        sa.Column("source_requisition_item_id", sa.Integer(), nullable=True),
        sa.Column("source_bom_requisition_source_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column(
            "component_type",
            sa.String(length=20),
            nullable=False,
            server_default="whole",
        ),
        sa.Column("source_finished_qty_snapshot", sa.Integer(), nullable=False),
        sa.Column("pieces_per_finished_snapshot", sa.Integer(), nullable=False),
        sa.Column("source_required_piece_qty_snapshot", sa.Integer(), nullable=False),
        sa.Column(
            "source_semi_reserved_piece_qty_snapshot", sa.Integer(), nullable=False
        ),
        sa.Column("source_effective_piece_qty_snapshot", sa.Integer(), nullable=False),
        sa.Column("yield_per_sheet_snapshot", sa.Integer(), nullable=False),
        sa.Column("group_effective_piece_qty_snapshot", sa.Integer(), nullable=False),
        sa.Column(
            "group_authoritative_order_sheet_qty_snapshot",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("purchase_sheet_qty", sa.Integer(), nullable=False),
        sa.Column("order_purpose_sheet_qty", sa.Integer(), nullable=False),
        sa.Column("reserve_purpose_sheet_qty", sa.Integer(), nullable=False),
        sa.Column("calculation_rule_version", sa.String(length=40), nullable=False),
        sa.Column(
            "snapshot_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_requisition_order_item_id"],
            ["supplier_requisition_order_items.id"],
            name="fk_purchase_purpose_snapshots_supplier_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["material_requisition_item_id"],
            ["material_requisition_items.id"],
            name="fk_purchase_purpose_snapshots_requisition_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_order_item_id"],
            ["sales_order_items.id"],
            name="fk_purchase_purpose_snapshots_order_item_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_requisition_item_id"],
            ["material_requisition_items.id"],
            name="fk_purchase_purpose_snapshots_requisition_item_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_bom_requisition_source_id"],
            ["requisition_item_bom_sources.id"],
            name="fk_purchase_purpose_snapshots_bom_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_purchase_purpose_snapshots_customer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_purchase_purpose_snapshots_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "snapshot_key",
            name="uq_purchase_purpose_source_snapshots_snapshot_key",
        ),
        sa.UniqueConstraint(
            "supplier_requisition_order_item_id",
            name="uq_purchase_purpose_source_snapshots_supplier_item",
        ),
        sa.UniqueConstraint(
            "material_requisition_item_id",
            name="uq_purchase_purpose_source_snapshots_requisition_item",
        ),
        sa.CheckConstraint(
            "((supplier_requisition_order_item_id IS NOT NULL "
            "AND material_requisition_item_id IS NULL) OR "
            "(supplier_requisition_order_item_id IS NULL "
            "AND material_requisition_item_id IS NOT NULL))",
            name="ck_purchase_purpose_source_snapshots_formal_item",
        ),
        sa.CheckConstraint(
            "source_kind IN ('order_item','requisition_item','bom_component',"
            "'direct_supplier_item')",
            name="ck_purchase_purpose_source_snapshots_source_kind",
        ),
        sa.CheckConstraint(
            "((source_kind = 'order_item' AND source_order_item_id IS NOT NULL "
            "AND source_requisition_item_id IS NULL "
            "AND source_bom_requisition_source_id IS NULL) OR "
            "(source_kind = 'requisition_item' AND source_order_item_id IS NOT NULL "
            "AND source_requisition_item_id IS NOT NULL "
            "AND source_bom_requisition_source_id IS NULL) OR "
            "(source_kind = 'bom_component' AND source_order_item_id IS NULL "
            "AND source_requisition_item_id IS NULL "
            "AND source_bom_requisition_source_id IS NOT NULL) OR "
            "(source_kind = 'direct_supplier_item' "
            "AND supplier_requisition_order_item_id IS NOT NULL "
            "AND source_order_item_id IS NULL "
            "AND source_requisition_item_id IS NULL "
            "AND source_bom_requisition_source_id IS NULL))",
            name="ck_purchase_purpose_source_snapshots_source_identity",
        ),
        sa.CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_purchase_purpose_source_snapshots_component_type",
        ),
        sa.CheckConstraint(
            "source_finished_qty_snapshot >= 0 "
            "AND pieces_per_finished_snapshot > 0 "
            "AND source_required_piece_qty_snapshot >= 0 "
            "AND source_semi_reserved_piece_qty_snapshot >= 0 "
            "AND source_semi_reserved_piece_qty_snapshot "
            "<= source_required_piece_qty_snapshot "
            "AND source_effective_piece_qty_snapshot >= 0 "
            "AND source_effective_piece_qty_snapshot <= "
            "source_required_piece_qty_snapshot - "
            "source_semi_reserved_piece_qty_snapshot",
            name="ck_purchase_purpose_source_snapshots_source_quantities",
        ),
        sa.CheckConstraint(
            "yield_per_sheet_snapshot > 0 "
            "AND group_effective_piece_qty_snapshot >= 0 "
            "AND group_effective_piece_qty_snapshot >= "
            "source_effective_piece_qty_snapshot "
            "AND group_authoritative_order_sheet_qty_snapshot > 0 "
            "AND group_authoritative_order_sheet_qty_snapshot * "
            "yield_per_sheet_snapshot >= group_effective_piece_qty_snapshot",
            name="ck_purchase_purpose_source_snapshots_group_conversion",
        ),
        sa.CheckConstraint(
            "purchase_sheet_qty >= 0 AND order_purpose_sheet_qty >= 0 "
            "AND reserve_purpose_sheet_qty >= 0 "
            "AND order_purpose_sheet_qty + reserve_purpose_sheet_qty = "
            "purchase_sheet_qty "
            "AND order_purpose_sheet_qty <= "
            "group_authoritative_order_sheet_qty_snapshot",
            name="ck_purchase_purpose_source_snapshots_purpose_balance",
        ),
        sa.CheckConstraint(
            "snapshot_version >= 1",
            name="ck_purchase_purpose_source_snapshots_version",
        ),
        sa.CheckConstraint(
            "length(trim(snapshot_key)) > 0 "
            "AND length(trim(allocation_group_key)) > 0 "
            "AND length(trim(source_key)) > 0 "
            "AND length(trim(customer_name_snapshot)) > 0 "
            "AND length(trim(calculation_rule_version)) > 0 "
            "AND length(preview_fingerprint) = 64 "
            "AND length(request_hash) = 64",
            name="ck_purchase_purpose_source_snapshots_frozen_text",
        ),
    )
    op.create_index(
        "ix_purchase_purpose_source_snapshots_supplier_item",
        TABLE,
        ["supplier_requisition_order_item_id"],
    )
    op.create_index(
        "ix_purchase_purpose_source_snapshots_requisition_item",
        TABLE,
        ["material_requisition_item_id"],
    )
    op.create_index(
        "ix_purchase_purpose_source_snapshots_customer_group",
        TABLE,
        ["customer_id", "allocation_group_key"],
    )
    _create_immutability_guards()


def downgrade() -> None:
    _assert_safe_downgrade()
    _drop_immutability_guards()
    op.drop_index("ix_purchase_purpose_source_snapshots_customer_group", table_name=TABLE)
    op.drop_index("ix_purchase_purpose_source_snapshots_requisition_item", table_name=TABLE)
    op.drop_index("ix_purchase_purpose_source_snapshots_supplier_item", table_name=TABLE)
    op.drop_table(TABLE)

    with op.batch_alter_table("material_requisitions") as batch:
        batch.drop_constraint(
            "ck_material_requisitions_request_fact", type_="check"
        )
        batch.drop_constraint(
            "fk_material_requisitions_request_actor_id_users", type_="foreignkey"
        )
        batch.drop_constraint(
            "uq_material_requisitions_request_key", type_="unique"
        )
        batch.drop_column("request_actor_id")
        batch.drop_column("request_hash")
        batch.drop_column("request_key")

    with op.batch_alter_table("supplier_requisition_orders") as batch:
        batch.drop_constraint(
            "ck_supplier_requisition_orders_request_fact", type_="check"
        )
        batch.drop_constraint(
            "fk_supplier_requisition_orders_request_actor_id_users",
            type_="foreignkey",
        )
        batch.drop_column("request_actor_id")
        batch.drop_column("request_hash")
