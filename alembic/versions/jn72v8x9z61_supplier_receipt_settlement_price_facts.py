"""add immutable supplier receipt settlement price facts

Revision ID: jn72v8x9z61
Revises: jm71v8x9z60
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "jn72v8x9z61"
down_revision = "jm71v8x9z60"
branch_labels = None
depends_on = None


FACT_TABLE = "supplier_receipt_settlement_price_facts"
SQLITE_UPDATE_TRIGGER = "trg_supplier_receipt_price_facts_immutable_update"
SQLITE_DELETE_TRIGGER = "trg_supplier_receipt_price_facts_immutable_delete"
POSTGRES_TRIGGER = "trg_supplier_receipt_price_facts_immutable"
POSTGRES_FUNCTION = "p0_39_immutable_supplier_receipt_price_fact"
LINE_FACT_FK = "fk_supplier_statement_lines_receipt_price_fact"
LINE_FACT_CHECK = "ck_supplier_monthly_statement_lines_price_fact"
LINE_FACT_INDEX = "ix_supplier_monthly_statement_lines_price_fact"
RECEIPT_REVERSE_TRIGGER = "trg_supplier_statement_receipt_reverse_guard"
SQLITE_LINE_MATCH_INSERT_TRIGGER = "trg_supplier_statement_price_fact_match_insert"
SQLITE_LINE_MATCH_UPDATE_TRIGGER = "trg_supplier_statement_price_fact_match_update"
POSTGRES_LINE_MATCH_TRIGGER = "trg_supplier_statement_price_fact_match"
POSTGRES_LINE_MATCH_FUNCTION = "p0_39_supplier_statement_price_fact_match"
STOCK_ITEM_TABLE = "stock_replenishment_order_items"
STOCK_ROUTE_COLUMN = "procurement_route_snapshot"
STOCK_ROUTE_CHECK = "ck_stock_replenishment_items_procurement_route"
SQLITE_STOCK_ROUTE_INSERT_TRIGGER = "trg_stock_replenishment_route_validate_insert"
SQLITE_STOCK_ROUTE_UPDATE_TRIGGER = "trg_stock_replenishment_route_immutable_update"
POSTGRES_STOCK_ROUTE_TRIGGER = "trg_stock_replenishment_route_immutable"
POSTGRES_STOCK_ROUTE_FUNCTION = "p0_39_stock_replenishment_route_immutable"


def _create_stock_route_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {SQLITE_STOCK_ROUTE_INSERT_TRIGGER}
                BEFORE INSERT ON {STOCK_ITEM_TABLE}
                FOR EACH ROW
                WHEN NEW.{STOCK_ROUTE_COLUMN} IS NOT NULL
                 AND NEW.{STOCK_ROUTE_COLUMN} NOT IN (
                     'paperboard', 'external_packaging'
                 )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'invalid stock replenishment procurement route snapshot'
                    );
                END
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {SQLITE_STOCK_ROUTE_UPDATE_TRIGGER}
                BEFORE UPDATE OF {STOCK_ROUTE_COLUMN} ON {STOCK_ITEM_TABLE}
                FOR EACH ROW
                WHEN COALESCE(OLD.{STOCK_ROUTE_COLUMN}, '')
                   <> COALESCE(NEW.{STOCK_ROUTE_COLUMN}, '')
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'stock replenishment procurement route snapshot is immutable'
                    );
                END
                """
            )
        )
    elif dialect == "postgresql":
        op.create_check_constraint(
            STOCK_ROUTE_CHECK,
            STOCK_ITEM_TABLE,
            f"{STOCK_ROUTE_COLUMN} IS NULL OR "
            f"{STOCK_ROUTE_COLUMN} IN ('paperboard','external_packaging')",
        )
        op.execute(
            sa.text(
                f"""
                CREATE OR REPLACE FUNCTION {POSTGRES_STOCK_ROUTE_FUNCTION}()
                RETURNS trigger AS $$
                BEGIN
                    IF NEW.{STOCK_ROUTE_COLUMN}
                       IS DISTINCT FROM OLD.{STOCK_ROUTE_COLUMN} THEN
                        RAISE EXCEPTION
                            'stock replenishment procurement route snapshot is immutable';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {POSTGRES_STOCK_ROUTE_TRIGGER}
                BEFORE UPDATE OF {STOCK_ROUTE_COLUMN} ON {STOCK_ITEM_TABLE}
                FOR EACH ROW EXECUTE FUNCTION {POSTGRES_STOCK_ROUTE_FUNCTION}()
                """
            )
        )


def _drop_stock_route_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {SQLITE_STOCK_ROUTE_UPDATE_TRIGGER}")
        )
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {SQLITE_STOCK_ROUTE_INSERT_TRIGGER}")
        )
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS {POSTGRES_STOCK_ROUTE_TRIGGER} "
                f"ON {STOCK_ITEM_TABLE}"
            )
        )
        op.execute(
            sa.text(f"DROP FUNCTION IF EXISTS {POSTGRES_STOCK_ROUTE_FUNCTION}()")
        )
        op.drop_constraint(
            STOCK_ROUTE_CHECK,
            STOCK_ITEM_TABLE,
            type_="check",
        )


def _add_stock_route_snapshot() -> None:
    op.add_column(
        STOCK_ITEM_TABLE,
        sa.Column(STOCK_ROUTE_COLUMN, sa.String(30), nullable=True),
    )
    _create_stock_route_guards()


def _drop_stock_route_snapshot() -> None:
    _drop_stock_route_guards()
    op.drop_column(STOCK_ITEM_TABLE, STOCK_ROUTE_COLUMN)


def _drop_receipt_reverse_guard() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {RECEIPT_REVERSE_TRIGGER}"))


def _create_receipt_reverse_guard() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {RECEIPT_REVERSE_TRIGGER}
            BEFORE UPDATE OF status ON incoming_receipt_items
            FOR EACH ROW
            WHEN OLD.status = 'posted'
             AND NEW.status = 'reversed'
             AND EXISTS (
                 SELECT 1
                 FROM supplier_monthly_statement_lines AS line
                 JOIN supplier_monthly_statements AS statement
                   ON statement.id = line.statement_id
                 WHERE line.incoming_receipt_item_id = OLD.id
                   AND line.active_guard = 1
                   AND statement.active_guard = 1
                   AND statement.status IN (
                       'confirmed_pending_invoice',
                       'invoiced_pending_payment',
                       'partial_payment',
                       'paid'
                   )
             )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'receipt belongs to a confirmed supplier monthly statement'
                );
            END
            """
        )
    )


def _create_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {SQLITE_UPDATE_TRIGGER}
                BEFORE UPDATE ON {FACT_TABLE}
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'supplier receipt settlement price facts are immutable'
                    );
                END
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {SQLITE_DELETE_TRIGGER}
                BEFORE DELETE ON {FACT_TABLE}
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'supplier receipt settlement price facts are immutable'
                    );
                END
                """
            )
        )
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                f"""
                CREATE OR REPLACE FUNCTION {POSTGRES_FUNCTION}()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION
                        'supplier receipt settlement price facts are immutable';
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {POSTGRES_TRIGGER}
                BEFORE UPDATE OR DELETE ON {FACT_TABLE}
                FOR EACH ROW EXECUTE FUNCTION {POSTGRES_FUNCTION}()
                """
            )
        )


def _drop_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {SQLITE_DELETE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {SQLITE_UPDATE_TRIGGER}"))
    elif dialect == "postgresql":
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {POSTGRES_TRIGGER} ON {FACT_TABLE}")
        )
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {POSTGRES_FUNCTION}()"))


def _create_line_fact_match_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        predicate = (
            "NEW.supplier_receipt_price_fact_id IS NOT NULL "
            "AND NOT EXISTS ("
            f"SELECT 1 FROM {FACT_TABLE} AS fact "
            "WHERE fact.id = NEW.supplier_receipt_price_fact_id "
            "AND fact.incoming_receipt_item_id = NEW.incoming_receipt_item_id "
            "AND fact.purchase_document_number_snapshot = NEW.purchase_document_number "
            "AND fact.receipt_number_snapshot = NEW.receipt_number "
            "AND fact.receipt_date_snapshot = NEW.receipt_date "
            "AND fact.material_code_snapshot = NEW.material_or_product_snapshot "
            "AND fact.received_quantity_snapshot = NEW.received_quantity "
            "AND fact.quantity_unit = NEW.quantity_unit "
            "AND fact.unit_price = NEW.frozen_unit_price "
            "AND fact.price_unit = NEW.price_unit "
            "AND fact.currency = NEW.currency "
            "AND fact.tax_rate = NEW.tax_rate "
            "AND ((fact.tax_included AND NEW.tax_basis = 'tax_inclusive') "
            "OR (NOT fact.tax_included AND NEW.tax_basis = 'tax_exclusive')))"
        )
        for name, event in (
            (SQLITE_LINE_MATCH_INSERT_TRIGGER, "INSERT"),
            (SQLITE_LINE_MATCH_UPDATE_TRIGGER, "UPDATE"),
        ):
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER {name}
                    BEFORE {event} ON supplier_monthly_statement_lines
                    FOR EACH ROW WHEN {predicate}
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'supplier statement price fact does not match receipt item'
                        );
                    END
                    """
                )
            )
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                f"""
                CREATE OR REPLACE FUNCTION {POSTGRES_LINE_MATCH_FUNCTION}()
                RETURNS trigger AS $$
                BEGIN
                    IF NEW.supplier_receipt_price_fact_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM {FACT_TABLE} AS fact
                            WHERE fact.id = NEW.supplier_receipt_price_fact_id
                              AND fact.incoming_receipt_item_id = NEW.incoming_receipt_item_id
                              AND fact.purchase_document_number_snapshot = NEW.purchase_document_number
                              AND fact.receipt_number_snapshot = NEW.receipt_number
                              AND fact.receipt_date_snapshot = NEW.receipt_date
                              AND fact.material_code_snapshot = NEW.material_or_product_snapshot
                              AND fact.received_quantity_snapshot = NEW.received_quantity
                              AND fact.quantity_unit = NEW.quantity_unit
                              AND fact.unit_price = NEW.frozen_unit_price
                              AND fact.price_unit = NEW.price_unit
                              AND fact.currency = NEW.currency
                              AND fact.tax_rate = NEW.tax_rate
                              AND (
                                  (fact.tax_included AND NEW.tax_basis = 'tax_inclusive')
                                  OR (NOT fact.tax_included AND NEW.tax_basis = 'tax_exclusive')
                              )
                       ) THEN
                        RAISE EXCEPTION
                            'supplier statement price fact does not match receipt item';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {POSTGRES_LINE_MATCH_TRIGGER}
                BEFORE INSERT OR UPDATE ON supplier_monthly_statement_lines
                FOR EACH ROW EXECUTE FUNCTION {POSTGRES_LINE_MATCH_FUNCTION}()
                """
            )
        )


def _drop_line_fact_match_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {SQLITE_LINE_MATCH_UPDATE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {SQLITE_LINE_MATCH_INSERT_TRIGGER}"))
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS {POSTGRES_LINE_MATCH_TRIGGER} "
                "ON supplier_monthly_statement_lines"
            )
        )
        op.execute(
            sa.text(f"DROP FUNCTION IF EXISTS {POSTGRES_LINE_MATCH_FUNCTION}()")
        )


def _assert_safe_downgrade() -> None:
    bind = op.get_bind()
    fact_count = int(
        bind.execute(sa.text(f"SELECT COUNT(*) FROM {FACT_TABLE}")).scalar_one()
        or 0
    )
    linked_line_count = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM supplier_monthly_statement_lines "
                "WHERE supplier_receipt_price_fact_id IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    frozen_route_count = int(
        bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {STOCK_ITEM_TABLE} "
                f"WHERE {STOCK_ROUTE_COLUMN} IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if fact_count or linked_line_count or frozen_route_count:
        raise RuntimeError(
            "P0-39 supplier receipt price fact downgrade blocked: "
            "immutable receipt price facts, statement references, or frozen "
            "stock procurement routes would be lost"
        )


def _add_statement_line_price_fact_link() -> None:
    table = "supplier_monthly_statement_lines"
    column = sa.Column("supplier_receipt_price_fact_id", sa.Integer(), nullable=True)
    predicate = (
        "supplier_receipt_price_fact_id IS NULL OR "
        "(source_type = 'paperboard' AND incoming_receipt_item_id IS NOT NULL)"
    )
    if op.get_bind().dialect.name == "sqlite":
        _drop_receipt_reverse_guard()
        with op.batch_alter_table(table, recreate="always") as batch_op:
            batch_op.add_column(column)
            batch_op.create_foreign_key(
                LINE_FACT_FK,
                FACT_TABLE,
                ["supplier_receipt_price_fact_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch_op.create_check_constraint(LINE_FACT_CHECK, predicate)
            batch_op.create_index(
                LINE_FACT_INDEX, ["supplier_receipt_price_fact_id"]
            )
        _create_receipt_reverse_guard()
        return
    op.add_column(table, column)
    op.create_foreign_key(
        LINE_FACT_FK,
        table,
        FACT_TABLE,
        ["supplier_receipt_price_fact_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(LINE_FACT_CHECK, table, predicate)
    op.create_index(LINE_FACT_INDEX, table, ["supplier_receipt_price_fact_id"])


def _drop_statement_line_price_fact_link() -> None:
    table = "supplier_monthly_statement_lines"
    if op.get_bind().dialect.name == "sqlite":
        _drop_receipt_reverse_guard()
        with op.batch_alter_table(table, recreate="always") as batch_op:
            batch_op.drop_index(LINE_FACT_INDEX)
            batch_op.drop_constraint(LINE_FACT_CHECK, type_="check")
            batch_op.drop_constraint(LINE_FACT_FK, type_="foreignkey")
            batch_op.drop_column("supplier_receipt_price_fact_id")
        _create_receipt_reverse_guard()
        return
    op.drop_index(LINE_FACT_INDEX, table_name=table)
    op.drop_constraint(LINE_FACT_CHECK, table, type_="check")
    op.drop_constraint(LINE_FACT_FK, table, type_="foreignkey")
    op.drop_column(table, "supplier_receipt_price_fact_id")


def upgrade() -> None:
    _add_stock_route_snapshot()
    op.create_table(
        FACT_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incoming_receipt_item_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("supplier_name_snapshot", sa.String(200), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("material_code_snapshot", sa.String(200), nullable=False),
        sa.Column("source_material_version", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(40), nullable=False),
        sa.Column(
            "purchase_document_number_snapshot", sa.String(80), nullable=False
        ),
        sa.Column("receipt_number_snapshot", sa.String(80), nullable=False),
        sa.Column("receipt_date_snapshot", sa.Date(), nullable=False),
        sa.Column(
            "received_quantity_snapshot", sa.Numeric(18, 6), nullable=False
        ),
        sa.Column("quantity_unit", sa.String(20), nullable=False),
        sa.Column("report_length_mm", sa.Numeric(12, 3), nullable=False),
        sa.Column("report_width_mm", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("price_unit", sa.String(30), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("tax_included", sa.Boolean(), nullable=False),
        sa.Column("tax_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("shipping_fee_mode", sa.String(20), nullable=False),
        sa.Column("fact_origin", sa.String(40), nullable=False),
        sa.Column("match_strategy", sa.String(50), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("adoption_reason", sa.Text(), nullable=True),
        sa.Column("adoption_evidence_reference", sa.String(255), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "fact_origin IN ('receipt_frozen','historical_master_adoption')",
            name="ck_supplier_receipt_price_facts_origin",
        ),
        sa.CheckConstraint(
            "source_kind IN "
            "('supplier_order_item','requisition_item','stock_replenishment_item')",
            name="ck_supplier_receipt_price_facts_source",
        ),
        sa.CheckConstraint(
            "match_strategy IN "
            "('purchase_receipt_fact','stable_material_id',"
            "'supplier_unique_material_code')",
            name="ck_supplier_receipt_price_facts_match",
        ),
        sa.CheckConstraint(
            "unit_price > 0 AND source_material_version >= 1 "
            "AND price_unit IN ('per_sheet','per_square_meter')",
            name="ck_supplier_receipt_price_facts_price",
        ),
        sa.CheckConstraint(
            "received_quantity_snapshot > 0 AND report_length_mm > 0 "
            "AND report_width_mm > 0 AND quantity_unit = '张'",
            name="ck_supplier_receipt_price_facts_receipt",
        ),
        sa.CheckConstraint(
            "currency = 'CNY' AND tax_included AND tax_rate = 0.13 "
            "AND shipping_fee_mode = 'included'",
            name="ck_supplier_receipt_price_facts_tax",
        ),
        sa.CheckConstraint(
            "((fact_origin = 'receipt_frozen' AND adoption_reason IS NULL "
            "AND adoption_evidence_reference IS NULL) OR "
            "(fact_origin = 'historical_master_adoption' AND "
            "adoption_reason IS NOT NULL AND "
            "adoption_reason = '2026-09-02 老板确认采用当前主数据' "
            "AND adoption_evidence_reference IS NOT NULL "
            "AND length(trim(adoption_evidence_reference)) > 0))",
            name="ck_supplier_receipt_price_facts_adoption",
        ),
        sa.CheckConstraint(
            "length(trim(supplier_name_snapshot)) > 0 "
            "AND length(trim(material_code_snapshot)) > 0 "
            "AND length(trim(purchase_document_number_snapshot)) > 0 "
            "AND length(trim(receipt_number_snapshot)) > 0 "
            "AND length(trim(currency)) = 3 "
            "AND length(source_hash) = 64",
            name="ck_supplier_receipt_price_facts_text",
        ),
        sa.ForeignKeyConstraint(
            ["incoming_receipt_item_id"],
            ["incoming_receipt_items.id"],
            name="fk_supplier_receipt_price_facts_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["supplier_master_records.id"],
            name="fk_supplier_receipt_price_facts_supplier",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["materials.id"],
            name="fk_supplier_receipt_price_facts_material",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_supplier_receipt_price_facts_creator",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "incoming_receipt_item_id",
            name="uq_supplier_receipt_price_facts_item",
        ),
    )
    op.create_index(
        "ix_supplier_receipt_price_facts_supplier",
        FACT_TABLE,
        ["supplier_id", "created_at"],
    )
    op.create_index(
        "ix_supplier_receipt_price_facts_material",
        FACT_TABLE,
        ["material_id", "created_at"],
    )

    _add_statement_line_price_fact_link()
    _create_immutable_guards()
    _create_line_fact_match_guard()


def downgrade() -> None:
    _assert_safe_downgrade()
    _drop_line_fact_match_guard()
    _drop_immutable_guards()

    _drop_statement_line_price_fact_link()
    op.drop_index(
        "ix_supplier_receipt_price_facts_material", table_name=FACT_TABLE
    )
    op.drop_index(
        "ix_supplier_receipt_price_facts_supplier", table_name=FACT_TABLE
    )
    op.drop_table(FACT_TABLE)
    _drop_stock_route_snapshot()
