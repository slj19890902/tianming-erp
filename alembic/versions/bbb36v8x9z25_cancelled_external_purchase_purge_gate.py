"""controlled purge gate for fully cancelled external purchases

Revision ID: bbb36v8x9z25
Revises: aaa35v8x9z24
Create Date: 2026-08-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "bbb36v8x9z25"
down_revision = "aaa35v8x9z24"
branch_labels = None
depends_on = None


AUTH = "external_packaging_purchase_purge_authorizations"
BATCH = "external_packaging_purchase_batches"
PURCHASE = "external_packaging_purchase_orders"
ITEM = "external_packaging_purchase_items"
CANCEL = "external_packaging_purchase_cancellations"
RECEIPT = "external_packaging_receipts"
RECEIPT_ITEM = "external_packaging_receipt_items"


def _sqlite_drop_delete_guards() -> None:
    for table in (BATCH, PURCHASE, ITEM, CANCEL):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_delete")


def _sqlite_create_guarded_deletes() -> None:
    op.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_{BATCH}_immutable_update
        BEFORE UPDATE ON {BATCH}
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, '{BATCH} rows are immutable');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{AUTH}_safe_insert
        BEFORE INSERT ON {AUTH}
        FOR EACH ROW WHEN
            NOT EXISTS (SELECT 1 FROM {BATCH} b WHERE b.id = NEW.batch_id)
            OR NOT EXISTS (SELECT 1 FROM {PURCHASE} p WHERE p.batch_id = NEW.batch_id)
            OR EXISTS (
                SELECT 1 FROM {PURCHASE} p
                LEFT JOIN {CANCEL} c ON c.purchase_order_id = p.id
                WHERE p.batch_id = NEW.batch_id AND c.id IS NULL
            )
            OR EXISTS (
                SELECT 1 FROM {RECEIPT} r
                JOIN {PURCHASE} p ON p.id = r.purchase_order_id
                WHERE p.batch_id = NEW.batch_id
            )
        BEGIN
            SELECT RAISE(ABORT, 'external purchase batch is not fully cancelled and unreceived');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{ITEM}_immutable_delete
        BEFORE DELETE ON {ITEM}
        FOR EACH ROW WHEN
            NOT EXISTS (
                SELECT 1 FROM {PURCHASE} p
                JOIN {AUTH} a ON a.batch_id = p.batch_id
                WHERE p.id = OLD.purchase_order_id
            )
            OR EXISTS (SELECT 1 FROM {RECEIPT_ITEM} r WHERE r.purchase_item_id = OLD.id)
        BEGIN
            SELECT RAISE(ABORT, '{ITEM} rows are immutable');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{CANCEL}_immutable_delete
        BEFORE DELETE ON {CANCEL}
        FOR EACH ROW WHEN
            NOT EXISTS (
                SELECT 1 FROM {PURCHASE} p
                JOIN {AUTH} a ON a.batch_id = p.batch_id
                WHERE p.id = OLD.purchase_order_id
            )
            OR EXISTS (SELECT 1 FROM {RECEIPT} r WHERE r.purchase_order_id = OLD.purchase_order_id)
        BEGIN
            SELECT RAISE(ABORT, '{CANCEL} rows are immutable');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{PURCHASE}_immutable_delete
        BEFORE DELETE ON {PURCHASE}
        FOR EACH ROW WHEN
            NOT EXISTS (SELECT 1 FROM {AUTH} a WHERE a.batch_id = OLD.batch_id)
            OR EXISTS (SELECT 1 FROM {RECEIPT} r WHERE r.purchase_order_id = OLD.id)
        BEGIN
            SELECT RAISE(ABORT, '{PURCHASE} rows are immutable');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{BATCH}_immutable_delete
        BEFORE DELETE ON {BATCH}
        FOR EACH ROW WHEN NOT EXISTS (SELECT 1 FROM {AUTH} a WHERE a.batch_id = OLD.id)
        BEGIN
            SELECT RAISE(ABORT, '{BATCH} rows are immutable');
        END
        """
    )


def _sqlite_restore_strict_deletes() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{AUTH}_safe_insert")
    _sqlite_drop_delete_guards()
    op.execute(f"DROP TRIGGER IF EXISTS trg_{BATCH}_immutable_update")
    for table in (BATCH, PURCHASE, ITEM, CANCEL):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )


def _postgres_create_guarded_deletes() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION p1_33c3_immutable_external_purchase()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'external packaging purchase facts are immutable';
            END IF;
            IF TG_TABLE_NAME = '{ITEM}' AND (
                NOT EXISTS (SELECT 1 FROM {PURCHASE} p JOIN {AUTH} a ON a.batch_id=p.batch_id WHERE p.id=OLD.purchase_order_id)
                OR EXISTS (SELECT 1 FROM {RECEIPT_ITEM} r WHERE r.purchase_item_id=OLD.id)
            ) THEN RAISE EXCEPTION 'external packaging purchase facts are immutable'; END IF;
            IF TG_TABLE_NAME = '{PURCHASE}' AND (
                NOT EXISTS (SELECT 1 FROM {AUTH} a WHERE a.batch_id=OLD.batch_id)
                OR EXISTS (SELECT 1 FROM {RECEIPT} r WHERE r.purchase_order_id=OLD.id)
            ) THEN RAISE EXCEPTION 'external packaging purchase facts are immutable'; END IF;
            IF TG_TABLE_NAME = '{BATCH}' AND NOT EXISTS (
                SELECT 1 FROM {AUTH} a WHERE a.batch_id=OLD.id
            ) THEN RAISE EXCEPTION 'external packaging purchase facts are immutable'; END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION p1_54_immutable_external_purchase_cancellation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' OR NOT EXISTS (
                SELECT 1 FROM {PURCHASE} p JOIN {AUTH} a ON a.batch_id=p.batch_id
                WHERE p.id=OLD.purchase_order_id
            ) OR EXISTS (
                SELECT 1 FROM {RECEIPT} r WHERE r.purchase_order_id=OLD.purchase_order_id
            ) THEN
                RAISE EXCEPTION 'external packaging purchase cancellation facts are immutable';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION controlled_external_purchase_purge_authorization()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM {PURCHASE} p WHERE p.batch_id=NEW.batch_id)
               OR EXISTS (SELECT 1 FROM {PURCHASE} p LEFT JOIN {CANCEL} c ON c.purchase_order_id=p.id WHERE p.batch_id=NEW.batch_id AND c.id IS NULL)
               OR EXISTS (SELECT 1 FROM {RECEIPT} r JOIN {PURCHASE} p ON p.id=r.purchase_order_id WHERE p.batch_id=NEW.batch_id)
            THEN RAISE EXCEPTION 'external purchase batch is not fully cancelled and unreceived'; END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_{AUTH}_safe_insert BEFORE INSERT ON {AUTH} "
        "FOR EACH ROW EXECUTE FUNCTION controlled_external_purchase_purge_authorization()"
    )


def upgrade() -> None:
    op.create_table(
        AUTH,
        sa.Column("batch_id", sa.Integer(), primary_key=True),
        sa.Column("authorized_by", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "authorized_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(["batch_id"], [f"{BATCH}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["authorized_by"], ["users.id"], ondelete="SET NULL"),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _sqlite_drop_delete_guards()
        _sqlite_create_guarded_deletes()
    elif dialect == "postgresql":
        _postgres_create_guarded_deletes()


def downgrade() -> None:
    connection = op.get_bind()
    pending = int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {AUTH}")).scalar_one() or 0)
    if pending:
        raise RuntimeError("cannot downgrade while external purchase purge authorization is pending")
    if connection.dialect.name == "sqlite":
        _sqlite_restore_strict_deletes()
    elif connection.dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{AUTH}_safe_insert ON {AUTH}")
        op.execute("DROP FUNCTION IF EXISTS controlled_external_purchase_purge_authorization()")
        # Recreate the original strict functions by delegating to the previous
        # migration's invariant: every UPDATE/DELETE raises.
        op.execute(
            """
            CREATE OR REPLACE FUNCTION p1_33c3_immutable_external_purchase()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'external packaging purchase facts are immutable';
            END; $$
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION p1_54_immutable_external_purchase_cancellation()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'external packaging purchase cancellation facts are immutable';
            END; $$
            """
        )
    op.drop_table(AUTH)
