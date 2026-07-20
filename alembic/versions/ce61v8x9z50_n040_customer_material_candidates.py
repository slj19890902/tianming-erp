"""add customer material candidates and immutable selection history

Revision ID: ce61v8x9z50
Revises: cd60v8x9z49
Create Date: 2026-07-19
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ce61v8x9z50"
down_revision: Union[str, Sequence[str], None] = "cd60v8x9z49"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CANDIDATE_TABLE = "customer_material_candidates"
HISTORY_TABLE = "customer_material_selection_history"
HISTORY_UPDATE_TRIGGER = "trg_customer_material_selection_history_no_update"
HISTORY_DELETE_TRIGGER = "trg_customer_material_selection_history_no_delete"
HISTORY_GUARD_FUNCTION = "n040_immutable_customer_material_selection_history"
DOWNGRADE_BLOCKED_MESSAGE = (
    "N040 客户材质候选、选择历史或新订单原始材质事实已产生，禁止破坏性降级；"
    "请停止服务并恢复 ce61 升级前的完整数据库备份。"
)


def _create_history_guards() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER {HISTORY_UPDATE_TRIGGER}
            BEFORE UPDATE ON {HISTORY_TABLE}
            FOR EACH ROW
            WHEN NOT (
                NEW.customer_id IS OLD.customer_id
                AND (NEW.order_item_id IS OLD.order_item_id OR NEW.order_item_id IS NULL)
                AND (NEW.product_id IS OLD.product_id OR NEW.product_id IS NULL)
                AND (NEW.candidate_id IS OLD.candidate_id OR NEW.candidate_id IS NULL)
                AND NEW.original_material_code_snapshot IS OLD.original_material_code_snapshot
                AND NEW.normalized_original_material_code_snapshot IS OLD.normalized_original_material_code_snapshot
                AND NEW.original_material_confidence IS OLD.original_material_confidence
                AND (NEW.selected_material_id IS OLD.selected_material_id OR NEW.selected_material_id IS NULL)
                AND NEW.selected_material_code_snapshot IS OLD.selected_material_code_snapshot
                AND NEW.selected_supplier_name_snapshot IS OLD.selected_supplier_name_snapshot
                AND NEW.layer_count_snapshot IS OLD.layer_count_snapshot
                AND NEW.flute_type_snapshot IS OLD.flute_type_snapshot
                AND NEW.source_type IS OLD.source_type
                AND NEW.source_reference IS OLD.source_reference
                AND NEW.selection_reason IS OLD.selection_reason
                AND NEW.sync_product IS OLD.sync_product
                AND (NEW.selected_by IS OLD.selected_by OR NEW.selected_by IS NULL)
                AND NEW.selected_at IS OLD.selected_at
            )
            BEGIN
                SELECT RAISE(ABORT, 'customer material selection history is immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {HISTORY_DELETE_TRIGGER}
            BEFORE DELETE ON {HISTORY_TABLE}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'customer material selection history is immutable');
            END
            """
        )
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE FUNCTION {HISTORY_GUARD_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'UPDATE'
                   AND NEW.customer_id IS NOT DISTINCT FROM OLD.customer_id
                   AND (NEW.order_item_id IS NOT DISTINCT FROM OLD.order_item_id OR NEW.order_item_id IS NULL)
                   AND (NEW.product_id IS NOT DISTINCT FROM OLD.product_id OR NEW.product_id IS NULL)
                   AND (NEW.candidate_id IS NOT DISTINCT FROM OLD.candidate_id OR NEW.candidate_id IS NULL)
                   AND NEW.original_material_code_snapshot IS NOT DISTINCT FROM OLD.original_material_code_snapshot
                   AND NEW.normalized_original_material_code_snapshot IS NOT DISTINCT FROM OLD.normalized_original_material_code_snapshot
                   AND NEW.original_material_confidence IS NOT DISTINCT FROM OLD.original_material_confidence
                   AND (NEW.selected_material_id IS NOT DISTINCT FROM OLD.selected_material_id OR NEW.selected_material_id IS NULL)
                   AND NEW.selected_material_code_snapshot IS NOT DISTINCT FROM OLD.selected_material_code_snapshot
                   AND NEW.selected_supplier_name_snapshot IS NOT DISTINCT FROM OLD.selected_supplier_name_snapshot
                   AND NEW.layer_count_snapshot IS NOT DISTINCT FROM OLD.layer_count_snapshot
                   AND NEW.flute_type_snapshot IS NOT DISTINCT FROM OLD.flute_type_snapshot
                   AND NEW.source_type IS NOT DISTINCT FROM OLD.source_type
                   AND NEW.source_reference IS NOT DISTINCT FROM OLD.source_reference
                   AND NEW.selection_reason IS NOT DISTINCT FROM OLD.selection_reason
                   AND NEW.sync_product IS NOT DISTINCT FROM OLD.sync_product
                   AND (NEW.selected_by IS NOT DISTINCT FROM OLD.selected_by OR NEW.selected_by IS NULL)
                   AND NEW.selected_at IS NOT DISTINCT FROM OLD.selected_at THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'customer material selection history is immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {HISTORY_UPDATE_TRIGGER}
            BEFORE UPDATE ON {HISTORY_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {HISTORY_GUARD_FUNCTION}()
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {HISTORY_DELETE_TRIGGER}
            BEFORE DELETE ON {HISTORY_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {HISTORY_GUARD_FUNCTION}()
            """
        )


def _drop_history_guards() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {HISTORY_UPDATE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {HISTORY_DELETE_TRIGGER}")
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {HISTORY_UPDATE_TRIGGER} ON {HISTORY_TABLE}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS {HISTORY_DELETE_TRIGGER} ON {HISTORY_TABLE}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {HISTORY_GUARD_FUNCTION}()")


def _assert_safe_downgrade() -> None:
    connection = op.get_bind()
    fact_count = connection.execute(
        sa.text(
            f"""
            SELECT
                (SELECT COUNT(*) FROM {CANDIDATE_TABLE})
              + (SELECT COUNT(*) FROM {HISTORY_TABLE})
              + (SELECT COUNT(*) FROM sales_order_items
                   WHERE snapshot_original_material_code IS NOT NULL)
            """
        )
    ).scalar_one()
    if int(fact_count or 0) > 0:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)


def upgrade() -> None:
    with op.batch_alter_table("sales_order_items") as batch_op:
        batch_op.add_column(
            sa.Column("snapshot_original_material_code", sa.String(250), nullable=True)
        )

    with op.batch_alter_table("supplier_requisition_order_items") as batch_op:
        batch_op.add_column(sa.Column("product_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("material_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("material_code_snapshot", sa.String(100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("supplier_name_snapshot", sa.String(200), nullable=True)
        )
        batch_op.add_column(
            sa.Column("layer_count_snapshot", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("flute_type_snapshot", sa.String(50), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_supplier_requisition_order_items_product_id",
            "products",
            ["product_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_supplier_requisition_order_items_material_id",
            "materials",
            ["material_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_supplier_requisition_order_items_product_created",
        "supplier_requisition_order_items",
        ["product_id", "supplier_order_id"],
    )
    op.create_index(
        "ix_supplier_requisition_order_items_material_id",
        "supplier_requisition_order_items",
        ["material_id"],
    )

    op.create_table(
        CANDIDATE_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("original_material_code", sa.String(250), nullable=False),
        sa.Column(
            "normalized_original_material_code", sa.String(250), nullable=False
        ),
        sa.Column("supplier_name", sa.String(200), nullable=False),
        sa.Column("normalized_supplier_name", sa.String(200), nullable=False),
        sa.Column("actual_material_id", sa.Integer(), nullable=True),
        sa.Column("actual_material_code_snapshot", sa.String(100), nullable=False),
        sa.Column(
            "manual_priority", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("source", sa.String(50), server_default="manual", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "manual_priority >= 0",
            name="ck_customer_material_candidates_priority",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actual_material_id"], ["materials.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id",
            "normalized_original_material_code",
            "normalized_supplier_name",
            "actual_material_code_snapshot",
            name="uq_customer_material_candidates_business_key",
        ),
    )
    op.create_index(
        "ix_customer_material_candidates_customer_id",
        CANDIDATE_TABLE,
        ["customer_id"],
    )
    op.create_index(
        "ix_customer_material_candidates_actual_material_id",
        CANDIDATE_TABLE,
        ["actual_material_id"],
    )
    op.create_index(
        "ix_customer_material_candidates_lookup",
        CANDIDATE_TABLE,
        ["customer_id", "normalized_original_material_code", "is_active"],
    )

    op.create_table(
        HISTORY_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("candidate_id", sa.Integer(), nullable=True),
        sa.Column("original_material_code_snapshot", sa.String(250), nullable=True),
        sa.Column(
            "normalized_original_material_code_snapshot",
            sa.String(250),
            nullable=True,
        ),
        sa.Column(
            "original_material_confidence",
            sa.String(30),
            server_default="frozen",
            nullable=False,
        ),
        sa.Column("selected_material_id", sa.Integer(), nullable=True),
        sa.Column("selected_material_code_snapshot", sa.String(100), nullable=False),
        sa.Column(
            "selected_supplier_name_snapshot", sa.String(200), nullable=True
        ),
        sa.Column("layer_count_snapshot", sa.Integer(), nullable=True),
        sa.Column("flute_type_snapshot", sa.String(50), nullable=True),
        sa.Column("source_type", sa.String(50), server_default="manual", nullable=False),
        sa.Column("source_reference", sa.String(250), nullable=True),
        sa.Column("selection_reason", sa.Text(), nullable=True),
        sa.Column("sync_product", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("selected_by", sa.Integer(), nullable=True),
        sa.Column(
            "selected_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"], ["sales_order_items.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["candidate_id"], [f"{CANDIDATE_TABLE}.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["selected_material_id"], ["materials.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["selected_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_customer_material_selection_history_customer_id",
        HISTORY_TABLE,
        ["customer_id"],
    )
    op.create_index(
        "ix_customer_material_selection_history_order_item_id",
        HISTORY_TABLE,
        ["order_item_id"],
    )
    op.create_index(
        "ix_customer_material_selection_history_lookup",
        HISTORY_TABLE,
        [
            "customer_id",
            "normalized_original_material_code_snapshot",
            "selected_at",
        ],
    )
    op.create_index(
        "ix_customer_material_selection_history_order_item",
        HISTORY_TABLE,
        ["order_item_id", "selected_at"],
    )
    op.create_index(
        "ix_customer_material_selection_history_product",
        HISTORY_TABLE,
        ["product_id", "selected_at"],
    )
    _create_history_guards()


def downgrade() -> None:
    _assert_safe_downgrade()
    _drop_history_guards()
    op.drop_index(
        "ix_customer_material_selection_history_product",
        table_name=HISTORY_TABLE,
    )
    op.drop_index(
        "ix_customer_material_selection_history_order_item",
        table_name=HISTORY_TABLE,
    )
    op.drop_index(
        "ix_customer_material_selection_history_lookup", table_name=HISTORY_TABLE
    )
    op.drop_index(
        "ix_customer_material_selection_history_order_item_id",
        table_name=HISTORY_TABLE,
    )
    op.drop_index(
        "ix_customer_material_selection_history_customer_id",
        table_name=HISTORY_TABLE,
    )
    op.drop_table(HISTORY_TABLE)
    op.drop_index(
        "ix_customer_material_candidates_lookup", table_name=CANDIDATE_TABLE
    )
    op.drop_index(
        "ix_customer_material_candidates_actual_material_id",
        table_name=CANDIDATE_TABLE,
    )
    op.drop_index(
        "ix_customer_material_candidates_customer_id", table_name=CANDIDATE_TABLE
    )
    op.drop_table(CANDIDATE_TABLE)
    op.drop_index(
        "ix_supplier_requisition_order_items_material_id",
        table_name="supplier_requisition_order_items",
    )
    op.drop_index(
        "ix_supplier_requisition_order_items_product_created",
        table_name="supplier_requisition_order_items",
    )
    with op.batch_alter_table("supplier_requisition_order_items") as batch_op:
        batch_op.drop_constraint(
            "fk_supplier_requisition_order_items_material_id", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_supplier_requisition_order_items_product_id", type_="foreignkey"
        )
        batch_op.drop_column("flute_type_snapshot")
        batch_op.drop_column("layer_count_snapshot")
        batch_op.drop_column("supplier_name_snapshot")
        batch_op.drop_column("material_code_snapshot")
        batch_op.drop_column("material_id")
        batch_op.drop_column("product_id")
    with op.batch_alter_table("sales_order_items") as batch_op:
        batch_op.drop_column("snapshot_original_material_code")
