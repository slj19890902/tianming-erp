"""add supplier-specific settlement cycles, payment batches and utility expense

Revision ID: jo73v8x9z62
Revises: jn72v8x9z61
Create Date: 2026-09-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "jo73v8x9z62"
down_revision = "jn72v8x9z61"
branch_labels = None
depends_on = None


_ADJUSTMENT_UPDATE_TRIGGER = "trg_supplier_monthly_adjustments_immutable_update"
_ADJUSTMENT_DELETE_TRIGGER = "trg_supplier_monthly_adjustments_immutable_delete"
_ADJUSTMENT_FUNCTION = "fn_supplier_monthly_adjustments_immutable"
_RECEIPT_REVERSE_TRIGGER = "trg_supplier_statement_receipt_reverse_guard"
_STATEMENT_REVISION_INSERT_TRIGGER = "trg_supplier_statement_revision_validate_insert"
_STATEMENT_REVISION_UPDATE_TRIGGER = "trg_supplier_statement_revision_immutable_update"
_STATEMENT_REVISION_FUNCTION = "fn_supplier_statement_revision_validate"
_ADJUSTMENT_VALIDATE_TRIGGER = "trg_supplier_monthly_adjustment_validate_insert"
_ADJUSTMENT_VALIDATE_FUNCTION = "fn_supplier_monthly_adjustment_validate"
_PAYMENT_BATCH_INSERT_TRIGGER = "trg_supplier_payment_batch_validate_insert"
_PAYMENT_BATCH_UPDATE_TRIGGER = "trg_supplier_payment_batch_immutable_update"
_PAYMENT_BATCH_FUNCTION = "fn_supplier_payment_batch_validate"
_CREDIT_VALIDATE_INSERT_TRIGGER = "trg_supplier_credit_lot_validate_insert"
_CREDIT_VALIDATE_UPDATE_TRIGGER = "trg_supplier_credit_lot_validate_update"
_CREDIT_VALIDATE_FUNCTION = "fn_supplier_credit_lot_validate"
_PAYMENT_VALIDATE_INSERT_TRIGGER = "trg_supplier_monthly_payment_validate_insert"
_PAYMENT_VALIDATE_UPDATE_TRIGGER = "trg_supplier_monthly_payment_validate_update"
_PAYMENT_VALIDATE_FUNCTION = "fn_supplier_monthly_payment_validate"
_ACCEPTANCE_VALIDATE_UPDATE_TRIGGER = "trg_finance_acceptance_supplier_link_update"
_ACCEPTANCE_VALIDATE_FUNCTION = "fn_finance_acceptance_supplier_link_validate"
_UTILITY_EXPENSE_GUARD_TRIGGER = "trg_finance_utility_expense_month_guard"
_UTILITY_READING_GUARD_TRIGGER = "trg_finance_utility_reading_month_guard"
_UTILITY_EXPENSE_UPDATE_GUARD_TRIGGER = "trg_finance_utility_expense_month_guard_update"
_UTILITY_READING_UPDATE_GUARD_TRIGGER = "trg_finance_utility_reading_month_guard_update"
_UTILITY_GUARD_FUNCTION = "fn_finance_utility_month_guard"
_UTILITY_MODE_UPDATE_TRIGGER = "trg_finance_utility_month_mode_update"
_UTILITY_MODE_DELETE_TRIGGER = "trg_finance_utility_month_mode_delete"
_UTILITY_MODE_FUNCTION = "fn_finance_utility_month_mode_immutable"


def _drop_receipt_reverse_guard() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_RECEIPT_REVERSE_TRIGGER}")


def _create_receipt_reverse_guard() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute(
        f"""
        CREATE TRIGGER {_RECEIPT_REVERSE_TRIGGER}
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


def _create_statement_revision_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_STATEMENT_REVISION_INSERT_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {_STATEMENT_REVISION_UPDATE_TRIGGER}")
        op.execute(
            f"""
            CREATE TRIGGER {_STATEMENT_REVISION_INSERT_TRIGGER}
            BEFORE INSERT ON supplier_monthly_statements
            FOR EACH ROW
            WHEN NEW.generation_origin = 'legacy' OR (
                NEW.document_revision = 1
                AND (
                    NEW.supersedes_statement_id IS NOT NULL
                    OR NEW.generation_origin = 'regenerate'
                )
            ) OR (
                NEW.document_revision > 1
                AND (
                    NEW.supersedes_statement_id IS NULL
                    OR NEW.generation_origin NOT IN ('legacy', 'regenerate')
                    OR NOT EXISTS (
                        SELECT 1
                        FROM supplier_monthly_statements AS previous
                        WHERE previous.id = NEW.supersedes_statement_id
                          AND previous.supplier_id = NEW.supplier_id
                          AND previous.settlement_month = NEW.settlement_month
                          AND previous.currency = NEW.currency
                          AND previous.tax_basis = NEW.tax_basis
                          AND previous.document_revision + 1 = NEW.document_revision
                    )
                )
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'supplier statement revision must be first version or exact same-key N+1'
                );
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_STATEMENT_REVISION_UPDATE_TRIGGER}
            BEFORE UPDATE OF
                supplier_id, settlement_month, currency, tax_basis,
                document_revision, generation_origin, supersedes_statement_id
            ON supplier_monthly_statements
            FOR EACH ROW
            WHEN NEW.supplier_id IS NOT OLD.supplier_id
              OR NEW.settlement_month IS NOT OLD.settlement_month
              OR NEW.currency IS NOT OLD.currency
              OR NEW.tax_basis IS NOT OLD.tax_basis
              OR NEW.document_revision IS NOT OLD.document_revision
              OR NEW.generation_origin IS NOT OLD.generation_origin
              OR NEW.supersedes_statement_id IS NOT OLD.supersedes_statement_id
            BEGIN
                SELECT RAISE(ABORT, 'supplier statement revision identity is immutable');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_STATEMENT_REVISION_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE' AND ROW(
                    NEW.supplier_id,
                    NEW.settlement_month,
                    NEW.currency,
                    NEW.tax_basis,
                    NEW.document_revision,
                    NEW.generation_origin,
                    NEW.supersedes_statement_id
                ) IS DISTINCT FROM ROW(
                    OLD.supplier_id,
                    OLD.settlement_month,
                    OLD.currency,
                    OLD.tax_basis,
                    OLD.document_revision,
                    OLD.generation_origin,
                    OLD.supersedes_statement_id
                ) THEN
                    RAISE EXCEPTION 'supplier statement revision identity is immutable';
                END IF;
                IF NEW.generation_origin = 'legacy' THEN
                    RAISE EXCEPTION
                        'legacy generation origin is reserved for migrated statements';
                END IF;
                IF NEW.document_revision = 1 THEN
                    IF NEW.supersedes_statement_id IS NOT NULL
                       OR NEW.generation_origin = 'regenerate' THEN
                        RAISE EXCEPTION
                            'supplier statement revision must start at version 1';
                    END IF;
                ELSIF NEW.supersedes_statement_id IS NULL
                   OR NEW.generation_origin NOT IN ('legacy', 'regenerate')
                   OR NOT EXISTS (
                       SELECT 1
                       FROM supplier_monthly_statements AS previous
                       WHERE previous.id = NEW.supersedes_statement_id
                         AND previous.supplier_id = NEW.supplier_id
                         AND previous.settlement_month = NEW.settlement_month
                         AND previous.currency = NEW.currency
                         AND previous.tax_basis = NEW.tax_basis
                         AND previous.document_revision + 1 = NEW.document_revision
                   ) THEN
                    RAISE EXCEPTION
                        'supplier statement revision must be exact same-key N+1';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {_STATEMENT_REVISION_INSERT_TRIGGER} "
            "BEFORE INSERT OR UPDATE ON supplier_monthly_statements "
            f"FOR EACH ROW EXECUTE FUNCTION {_STATEMENT_REVISION_FUNCTION}()"
        )


def _drop_statement_revision_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_STATEMENT_REVISION_INSERT_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {_STATEMENT_REVISION_UPDATE_TRIGGER}")
    elif dialect == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {_STATEMENT_REVISION_INSERT_TRIGGER} "
            "ON supplier_monthly_statements"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {_STATEMENT_REVISION_FUNCTION}()")


def _create_adjustment_validation() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_ADJUSTMENT_VALIDATE_TRIGGER}")
        op.execute(
            f"""
            CREATE TRIGGER {_ADJUSTMENT_VALIDATE_TRIGGER}
            BEFORE INSERT ON supplier_monthly_adjustments
            FOR EACH ROW
            WHEN NOT (
                NEW.is_post_confirmation = 0
                AND NEW.statement_version_before IS NULL
                AND NEW.amount_before IS NULL
                AND NEW.amount_after IS NULL
            ) AND (
              NEW.statement_version_before IS NULL
              OR NEW.amount_before IS NULL
              OR NEW.amount_after IS NULL
              OR (
                  NEW.statement_line_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM supplier_monthly_statement_lines AS line
                      WHERE line.id = NEW.statement_line_id
                        AND line.statement_id = NEW.statement_id
                  )
              )
              OR NOT EXISTS (
                  SELECT 1
                  FROM supplier_monthly_statements AS statement
                  WHERE statement.id = NEW.statement_id
                    AND statement.version = NEW.statement_version_before + 1
                    AND (
                        (
                            NEW.is_post_confirmation = 1
                            AND statement.status IN (
                                'confirmed_pending_invoice',
                                'invoiced_pending_payment',
                                'partial_payment',
                                'paid'
                            )
                            AND statement.confirmed_amount IS NOT NULL
                            AND abs(statement.confirmed_amount - NEW.amount_after) < 0.005
                        ) OR (
                            NEW.is_post_confirmation = 0
                            AND statement.status IN ('draft', 'difference')
                            AND abs(statement.adjusted_amount - NEW.amount_after) < 0.005
                        )
                    )
              ))
            BEGIN
                SELECT RAISE(ABORT, 'supplier adjustment audit does not match statement');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_ADJUSTMENT_VALIDATE_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                IF NOT (
                    NOT NEW.is_post_confirmation
                    AND NEW.statement_version_before IS NULL
                    AND NEW.amount_before IS NULL
                    AND NEW.amount_after IS NULL
                ) AND (
                   NEW.statement_version_before IS NULL
                   OR NEW.amount_before IS NULL
                   OR NEW.amount_after IS NULL
                   OR (
                       NEW.statement_line_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1
                           FROM supplier_monthly_statement_lines AS line
                           WHERE line.id = NEW.statement_line_id
                             AND line.statement_id = NEW.statement_id
                       )
                   )
                   OR NOT EXISTS (
                       SELECT 1
                       FROM supplier_monthly_statements AS statement
                       WHERE statement.id = NEW.statement_id
                         AND statement.version = NEW.statement_version_before + 1
                         AND (
                             (
                                 NEW.is_post_confirmation
                                 AND statement.status IN (
                                     'confirmed_pending_invoice',
                                     'invoiced_pending_payment',
                                     'partial_payment',
                                     'paid'
                                 )
                                 AND statement.confirmed_amount IS NOT NULL
                                 AND abs(statement.confirmed_amount - NEW.amount_after) < 0.005
                             ) OR (
                                 NOT NEW.is_post_confirmation
                                 AND statement.status IN ('draft', 'difference')
                                 AND abs(statement.adjusted_amount - NEW.amount_after) < 0.005
                             )
                         )
                   )) THEN
                    RAISE EXCEPTION
                        'supplier adjustment audit does not match statement';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {_ADJUSTMENT_VALIDATE_TRIGGER} "
            "BEFORE INSERT ON supplier_monthly_adjustments "
            f"FOR EACH ROW EXECUTE FUNCTION {_ADJUSTMENT_VALIDATE_FUNCTION}()"
        )


def _drop_adjustment_validation() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_ADJUSTMENT_VALIDATE_TRIGGER}")
    elif dialect == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {_ADJUSTMENT_VALIDATE_TRIGGER} "
            "ON supplier_monthly_adjustments"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {_ADJUSTMENT_VALIDATE_FUNCTION}()")


def _create_adjustment_immutability() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_ADJUSTMENT_UPDATE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {_ADJUSTMENT_DELETE_TRIGGER}")
        op.execute(
            f"""
            CREATE TRIGGER {_ADJUSTMENT_UPDATE_TRIGGER}
            BEFORE UPDATE ON supplier_monthly_adjustments
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'supplier monthly adjustment facts are immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_ADJUSTMENT_DELETE_TRIGGER}
            BEFORE DELETE ON supplier_monthly_adjustments
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'supplier monthly adjustment facts are immutable');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_ADJUSTMENT_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'supplier monthly adjustment facts are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {_ADJUSTMENT_UPDATE_TRIGGER} "
            "BEFORE UPDATE ON supplier_monthly_adjustments "
            f"FOR EACH ROW EXECUTE FUNCTION {_ADJUSTMENT_FUNCTION}()"
        )
        op.execute(
            f"CREATE TRIGGER {_ADJUSTMENT_DELETE_TRIGGER} "
            "BEFORE DELETE ON supplier_monthly_adjustments "
            f"FOR EACH ROW EXECUTE FUNCTION {_ADJUSTMENT_FUNCTION}()"
        )


def _drop_adjustment_immutability() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_ADJUSTMENT_UPDATE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {_ADJUSTMENT_DELETE_TRIGGER}")
    elif dialect == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {_ADJUSTMENT_UPDATE_TRIGGER} "
            "ON supplier_monthly_adjustments"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS {_ADJUSTMENT_DELETE_TRIGGER} "
            "ON supplier_monthly_adjustments"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {_ADJUSTMENT_FUNCTION}()")


def _create_financial_integrity_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for trigger in (
            _PAYMENT_BATCH_INSERT_TRIGGER,
            _PAYMENT_BATCH_UPDATE_TRIGGER,
            _CREDIT_VALIDATE_INSERT_TRIGGER,
            _CREDIT_VALIDATE_UPDATE_TRIGGER,
            _PAYMENT_VALIDATE_INSERT_TRIGGER,
            _PAYMENT_VALIDATE_UPDATE_TRIGGER,
            _ACCEPTANCE_VALIDATE_UPDATE_TRIGGER,
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        op.execute(
            f"""
            CREATE TRIGGER {_PAYMENT_BATCH_INSERT_TRIGGER}
            BEFORE INSERT ON supplier_payment_batches
            FOR EACH ROW
            WHEN NOT EXISTS (
                SELECT 1
                FROM supplier_monthly_statements AS statement
                WHERE statement.id = NEW.statement_id
                  AND statement.supplier_id = NEW.supplier_id
                  AND statement.status IN (
                      'confirmed_pending_invoice',
                      'invoiced_pending_payment',
                      'partial_payment'
                  )
                  AND statement.confirmed_amount IS NOT NULL
                  AND NEW.settled_amount
                      <= statement.confirmed_amount - statement.paid_amount + 0.004
                  AND NEW.settled_amount
                      <= statement.invoice_allocated_amount - statement.paid_amount + 0.004
            ) OR (
                NEW.acceptance_note_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM finance_acceptance_notes AS note
                    WHERE note.id = NEW.acceptance_note_id
                      AND note.status = 'held'
                      AND abs(note.amount - NEW.acceptance_face_amount) < 0.005
                )
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'supplier payment batch does not match statement or acceptance'
                );
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_PAYMENT_BATCH_UPDATE_TRIGGER}
            BEFORE UPDATE ON supplier_payment_batches
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'supplier payment batch facts are immutable');
            END
            """
        )
        credit_condition = """
            NOT EXISTS (
                SELECT 1
                FROM supplier_monthly_statements AS statement
                WHERE statement.id = NEW.source_statement_id
                  AND statement.supplier_id = NEW.supplier_id
            ) OR (
                NEW.source_type = 'acceptance_overpayment'
                AND NOT EXISTS (
                    SELECT 1
                    FROM supplier_payment_batches AS batch
                    WHERE batch.id = NEW.source_payment_batch_id
                      AND batch.statement_id = NEW.source_statement_id
                      AND batch.supplier_id = NEW.supplier_id
                      AND batch.acceptance_note_id = NEW.source_acceptance_note_id
                      AND abs(batch.credit_created_amount - NEW.original_amount) < 0.005
                )
            ) OR (
                NEW.source_type = 'statement_adjustment'
                AND NOT EXISTS (
                    SELECT 1
                    FROM supplier_monthly_adjustments AS adjustment
                    WHERE adjustment.statement_id = NEW.source_statement_id
                      AND adjustment.is_post_confirmation = 1
                      AND adjustment.amount < 0
                      AND NEW.original_amount <= -adjustment.amount + 0.004
                )
            )
        """
        op.execute(
            f"""
            CREATE TRIGGER {_CREDIT_VALIDATE_INSERT_TRIGGER}
            BEFORE INSERT ON supplier_credit_lots
            FOR EACH ROW
            WHEN {credit_condition}
            BEGIN
                SELECT RAISE(ABORT, 'supplier credit source does not match supplier');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_CREDIT_VALIDATE_UPDATE_TRIGGER}
            BEFORE UPDATE ON supplier_credit_lots
            FOR EACH ROW
            WHEN {credit_condition}
            BEGIN
                SELECT RAISE(ABORT, 'supplier credit source does not match supplier');
            END
            """
        )
        payment_condition = """
            (
                NEW.payment_batch_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM supplier_payment_batches AS batch
                    WHERE batch.id = NEW.payment_batch_id
                      AND batch.statement_id = NEW.statement_id
                      AND (
                          (
                              NEW.payment_method = 'bank'
                              AND NEW.acceptance_note_id IS NULL
                              AND NEW.supplier_credit_id IS NULL
                              AND abs(batch.bank_amount - NEW.amount) < 0.005
                          ) OR (
                              NEW.payment_method = 'acceptance'
                              AND batch.acceptance_note_id = NEW.acceptance_note_id
                              AND NEW.supplier_credit_id IS NULL
                              AND abs(batch.acceptance_applied_amount - NEW.amount) < 0.005
                          ) OR (
                              NEW.payment_method = 'credit'
                              AND NEW.acceptance_note_id IS NULL
                              AND NEW.supplier_credit_id IS NOT NULL
                              AND NEW.amount <= batch.credit_applied_amount + 0.004
                          )
                      )
                )
            ) OR (
                NEW.payment_method = 'credit'
                AND (
                    NEW.payment_batch_id IS NULL
                    OR NOT EXISTS (
                        SELECT 1
                        FROM supplier_credit_lots AS credit
                        JOIN supplier_monthly_statements AS statement
                          ON statement.id = NEW.statement_id
                        JOIN supplier_payment_batches AS batch
                          ON batch.id = NEW.payment_batch_id
                        WHERE credit.id = NEW.supplier_credit_id
                          AND credit.supplier_id = statement.supplier_id
                          AND credit.supplier_id = batch.supplier_id
                    )
                )
            )
        """
        op.execute(
            f"""
            CREATE TRIGGER {_PAYMENT_VALIDATE_INSERT_TRIGGER}
            BEFORE INSERT ON supplier_monthly_payments
            FOR EACH ROW
            WHEN {payment_condition}
            BEGIN
                SELECT RAISE(ABORT, 'supplier payment does not match batch or credit');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_PAYMENT_VALIDATE_UPDATE_TRIGGER}
            BEFORE UPDATE ON supplier_monthly_payments
            FOR EACH ROW
            WHEN {payment_condition}
            BEGIN
                SELECT RAISE(ABORT, 'supplier payment does not match batch or credit');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_ACCEPTANCE_VALIDATE_UPDATE_TRIGGER}
            BEFORE UPDATE ON finance_acceptance_notes
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1
                FROM supplier_payment_batches AS batch
                WHERE batch.acceptance_note_id = OLD.id
            ) AND (
                NOT EXISTS (
                    SELECT 1
                    FROM supplier_payment_batches AS batch
                    WHERE batch.acceptance_note_id = NEW.id
                      AND batch.supplier_id = NEW.supplier_id
                      AND batch.statement_id = NEW.supplier_statement_id
                      AND abs(batch.acceptance_face_amount - NEW.amount) < 0.005
                      AND NEW.status = 'endorsed'
                ) OR (
                    OLD.status = 'endorsed'
                    AND (
                        NEW.supplier_payment_id IS NULL
                        OR NOT EXISTS (
                            SELECT 1
                            FROM supplier_monthly_payments AS payment
                            JOIN supplier_payment_batches AS batch
                              ON batch.id = payment.payment_batch_id
                            WHERE payment.id = NEW.supplier_payment_id
                              AND payment.acceptance_note_id = NEW.id
                              AND batch.acceptance_note_id = NEW.id
                        )
                    )
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'acceptance supplier link does not match payment batch');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_PAYMENT_BATCH_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    RAISE EXCEPTION 'supplier payment batch facts are immutable';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM supplier_monthly_statements AS statement
                    WHERE statement.id = NEW.statement_id
                      AND statement.supplier_id = NEW.supplier_id
                      AND statement.status IN (
                          'confirmed_pending_invoice',
                          'invoiced_pending_payment',
                          'partial_payment'
                      )
                      AND statement.confirmed_amount IS NOT NULL
                      AND NEW.settled_amount
                          <= statement.confirmed_amount - statement.paid_amount + 0.004
                      AND NEW.settled_amount
                          <= statement.invoice_allocated_amount - statement.paid_amount + 0.004
                ) OR (
                    NEW.acceptance_note_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1
                        FROM finance_acceptance_notes AS note
                        WHERE note.id = NEW.acceptance_note_id
                          AND note.status = 'held'
                          AND abs(note.amount - NEW.acceptance_face_amount) < 0.005
                    )
                ) THEN
                    RAISE EXCEPTION
                        'supplier payment batch does not match statement or acceptance';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {_PAYMENT_BATCH_INSERT_TRIGGER} "
            "BEFORE INSERT OR UPDATE ON supplier_payment_batches "
            f"FOR EACH ROW EXECUTE FUNCTION {_PAYMENT_BATCH_FUNCTION}()"
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_CREDIT_VALIDATE_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM supplier_monthly_statements AS statement
                    WHERE statement.id = NEW.source_statement_id
                      AND statement.supplier_id = NEW.supplier_id
                ) OR (
                    NEW.source_type = 'acceptance_overpayment'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM supplier_payment_batches AS batch
                        WHERE batch.id = NEW.source_payment_batch_id
                          AND batch.statement_id = NEW.source_statement_id
                          AND batch.supplier_id = NEW.supplier_id
                          AND batch.acceptance_note_id = NEW.source_acceptance_note_id
                          AND abs(batch.credit_created_amount - NEW.original_amount) < 0.005
                    )
                ) OR (
                    NEW.source_type = 'statement_adjustment'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM supplier_monthly_adjustments AS adjustment
                        WHERE adjustment.statement_id = NEW.source_statement_id
                          AND adjustment.is_post_confirmation
                          AND adjustment.amount < 0
                          AND NEW.original_amount <= -adjustment.amount + 0.004
                    )
                ) THEN
                    RAISE EXCEPTION 'supplier credit source does not match supplier';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {_CREDIT_VALIDATE_INSERT_TRIGGER} "
            "BEFORE INSERT OR UPDATE ON supplier_credit_lots "
            f"FOR EACH ROW EXECUTE FUNCTION {_CREDIT_VALIDATE_FUNCTION}()"
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_PAYMENT_VALIDATE_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                IF (
                    NEW.payment_batch_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1
                        FROM supplier_payment_batches AS batch
                        WHERE batch.id = NEW.payment_batch_id
                          AND batch.statement_id = NEW.statement_id
                          AND (
                              (
                                  NEW.payment_method = 'bank'
                                  AND NEW.acceptance_note_id IS NULL
                                  AND NEW.supplier_credit_id IS NULL
                                  AND abs(batch.bank_amount - NEW.amount) < 0.005
                              ) OR (
                                  NEW.payment_method = 'acceptance'
                                  AND batch.acceptance_note_id = NEW.acceptance_note_id
                                  AND NEW.supplier_credit_id IS NULL
                                  AND abs(batch.acceptance_applied_amount - NEW.amount) < 0.005
                              ) OR (
                                  NEW.payment_method = 'credit'
                                  AND NEW.acceptance_note_id IS NULL
                                  AND NEW.supplier_credit_id IS NOT NULL
                                  AND NEW.amount <= batch.credit_applied_amount + 0.004
                              )
                          )
                    )
                ) OR (
                    NEW.payment_method = 'credit'
                    AND (
                        NEW.payment_batch_id IS NULL
                        OR NOT EXISTS (
                            SELECT 1
                            FROM supplier_credit_lots AS credit
                            JOIN supplier_monthly_statements AS statement
                              ON statement.id = NEW.statement_id
                            JOIN supplier_payment_batches AS batch
                              ON batch.id = NEW.payment_batch_id
                            WHERE credit.id = NEW.supplier_credit_id
                              AND credit.supplier_id = statement.supplier_id
                              AND credit.supplier_id = batch.supplier_id
                        )
                    )
                ) THEN
                    RAISE EXCEPTION 'supplier payment does not match batch or credit';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {_PAYMENT_VALIDATE_INSERT_TRIGGER} "
            "BEFORE INSERT OR UPDATE ON supplier_monthly_payments "
            f"FOR EACH ROW EXECUTE FUNCTION {_PAYMENT_VALIDATE_FUNCTION}()"
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_ACCEPTANCE_VALIDATE_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM supplier_payment_batches AS batch
                    WHERE batch.acceptance_note_id = OLD.id
                ) AND (
                    NOT EXISTS (
                        SELECT 1
                        FROM supplier_payment_batches AS batch
                        WHERE batch.acceptance_note_id = NEW.id
                          AND batch.supplier_id = NEW.supplier_id
                          AND batch.statement_id = NEW.supplier_statement_id
                          AND abs(batch.acceptance_face_amount - NEW.amount) < 0.005
                          AND NEW.status = 'endorsed'
                    ) OR (
                        OLD.status = 'endorsed'
                        AND (
                            NEW.supplier_payment_id IS NULL
                            OR NOT EXISTS (
                                SELECT 1
                                FROM supplier_monthly_payments AS payment
                                JOIN supplier_payment_batches AS batch
                                  ON batch.id = payment.payment_batch_id
                                WHERE payment.id = NEW.supplier_payment_id
                                  AND payment.acceptance_note_id = NEW.id
                                  AND batch.acceptance_note_id = NEW.id
                            )
                        )
                    )
                ) THEN
                    RAISE EXCEPTION
                        'acceptance supplier link does not match payment batch';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {_ACCEPTANCE_VALIDATE_UPDATE_TRIGGER} "
            "BEFORE UPDATE ON finance_acceptance_notes "
            f"FOR EACH ROW EXECUTE FUNCTION {_ACCEPTANCE_VALIDATE_FUNCTION}()"
        )


def _drop_financial_integrity_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for trigger in (
            _PAYMENT_BATCH_INSERT_TRIGGER,
            _PAYMENT_BATCH_UPDATE_TRIGGER,
            _CREDIT_VALIDATE_INSERT_TRIGGER,
            _CREDIT_VALIDATE_UPDATE_TRIGGER,
            _PAYMENT_VALIDATE_INSERT_TRIGGER,
            _PAYMENT_VALIDATE_UPDATE_TRIGGER,
            _ACCEPTANCE_VALIDATE_UPDATE_TRIGGER,
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    elif dialect == "postgresql":
        for trigger, table in (
            (_PAYMENT_BATCH_INSERT_TRIGGER, "supplier_payment_batches"),
            (_CREDIT_VALIDATE_INSERT_TRIGGER, "supplier_credit_lots"),
            (_PAYMENT_VALIDATE_INSERT_TRIGGER, "supplier_monthly_payments"),
            (_ACCEPTANCE_VALIDATE_UPDATE_TRIGGER, "finance_acceptance_notes"),
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        for function in (
            _PAYMENT_BATCH_FUNCTION,
            _CREDIT_VALIDATE_FUNCTION,
            _PAYMENT_VALIDATE_FUNCTION,
            _ACCEPTANCE_VALIDATE_FUNCTION,
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {function}()")


def _create_utility_month_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for trigger in (
            _UTILITY_EXPENSE_GUARD_TRIGGER,
            _UTILITY_READING_GUARD_TRIGGER,
            _UTILITY_EXPENSE_UPDATE_GUARD_TRIGGER,
            _UTILITY_READING_UPDATE_GUARD_TRIGGER,
            _UTILITY_MODE_UPDATE_TRIGGER,
            _UTILITY_MODE_DELETE_TRIGGER,
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        op.execute(
            f"""
            CREATE TRIGGER {_UTILITY_EXPENSE_GUARD_TRIGGER}
            BEFORE INSERT ON finance_utility_expenses
            FOR EACH ROW
            BEGIN
                INSERT OR IGNORE INTO finance_utility_month_modes(cost_month, mode)
                VALUES (NEW.cost_month, 'combined');
                SELECT CASE
                    WHEN (
                        SELECT mode
                        FROM finance_utility_month_modes
                        WHERE cost_month = NEW.cost_month
                    ) <> 'combined'
                    THEN RAISE(
                        ABORT,
                        'legacy and combined utility facts cannot share one month'
                    )
                END;
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_UTILITY_READING_GUARD_TRIGGER}
            BEFORE INSERT ON finance_utility_readings
            FOR EACH ROW
            BEGIN
                INSERT OR IGNORE INTO finance_utility_month_modes(cost_month, mode)
                VALUES (NEW.cost_month, 'legacy');
                SELECT CASE
                    WHEN (
                        SELECT mode
                        FROM finance_utility_month_modes
                        WHERE cost_month = NEW.cost_month
                    ) <> 'legacy'
                    THEN RAISE(
                        ABORT,
                        'legacy and combined utility facts cannot share one month'
                    )
                END;
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_UTILITY_EXPENSE_UPDATE_GUARD_TRIGGER}
            BEFORE UPDATE OF cost_month ON finance_utility_expenses
            FOR EACH ROW
            BEGIN
                INSERT OR IGNORE INTO finance_utility_month_modes(cost_month, mode)
                VALUES (NEW.cost_month, 'combined');
                SELECT CASE
                    WHEN (
                        SELECT mode
                        FROM finance_utility_month_modes
                        WHERE cost_month = NEW.cost_month
                    ) <> 'combined'
                    THEN RAISE(
                        ABORT,
                        'legacy and combined utility facts cannot share one month'
                    )
                END;
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_UTILITY_READING_UPDATE_GUARD_TRIGGER}
            BEFORE UPDATE OF cost_month ON finance_utility_readings
            FOR EACH ROW
            BEGIN
                INSERT OR IGNORE INTO finance_utility_month_modes(cost_month, mode)
                VALUES (NEW.cost_month, 'legacy');
                SELECT CASE
                    WHEN (
                        SELECT mode
                        FROM finance_utility_month_modes
                        WHERE cost_month = NEW.cost_month
                    ) <> 'legacy'
                    THEN RAISE(
                        ABORT,
                        'legacy and combined utility facts cannot share one month'
                    )
                END;
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_UTILITY_MODE_UPDATE_TRIGGER}
            BEFORE UPDATE ON finance_utility_month_modes
            FOR EACH ROW
            WHEN NEW.cost_month IS NOT OLD.cost_month OR NEW.mode IS NOT OLD.mode
            BEGIN
                SELECT RAISE(ABORT, 'utility month mode facts are immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_UTILITY_MODE_DELETE_TRIGGER}
            BEFORE DELETE ON finance_utility_month_modes
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'utility month mode facts are immutable');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_UTILITY_GUARD_FUNCTION}()
            RETURNS trigger AS $$
            DECLARE
                claimed_mode text;
                requested_mode text;
            BEGIN
                IF TG_TABLE_NAME = 'finance_utility_expenses' THEN
                    requested_mode := 'combined';
                ELSE
                    requested_mode := 'legacy';
                END IF;
                INSERT INTO finance_utility_month_modes(cost_month, mode)
                VALUES (NEW.cost_month, requested_mode)
                ON CONFLICT (cost_month) DO UPDATE
                    SET mode = finance_utility_month_modes.mode
                RETURNING mode INTO claimed_mode;
                IF claimed_mode <> requested_mode THEN
                    RAISE EXCEPTION
                        'legacy and combined utility facts cannot share one month';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {_UTILITY_EXPENSE_GUARD_TRIGGER} "
            "BEFORE INSERT OR UPDATE OF cost_month ON finance_utility_expenses "
            f"FOR EACH ROW EXECUTE FUNCTION {_UTILITY_GUARD_FUNCTION}()"
        )
        op.execute(
            f"CREATE TRIGGER {_UTILITY_READING_GUARD_TRIGGER} "
            "BEFORE INSERT OR UPDATE OF cost_month ON finance_utility_readings "
            f"FOR EACH ROW EXECUTE FUNCTION {_UTILITY_GUARD_FUNCTION}()"
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {_UTILITY_MODE_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'utility month mode facts are immutable';
                END IF;
                IF ROW(NEW.cost_month, NEW.mode)
                   IS DISTINCT FROM ROW(OLD.cost_month, OLD.mode) THEN
                    RAISE EXCEPTION 'utility month mode facts are immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {_UTILITY_MODE_UPDATE_TRIGGER} "
            "BEFORE UPDATE ON finance_utility_month_modes "
            f"FOR EACH ROW EXECUTE FUNCTION {_UTILITY_MODE_FUNCTION}()"
        )
        op.execute(
            f"CREATE TRIGGER {_UTILITY_MODE_DELETE_TRIGGER} "
            "BEFORE DELETE ON finance_utility_month_modes "
            f"FOR EACH ROW EXECUTE FUNCTION {_UTILITY_MODE_FUNCTION}()"
        )


def _drop_utility_month_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for trigger in (
            _UTILITY_EXPENSE_GUARD_TRIGGER,
            _UTILITY_READING_GUARD_TRIGGER,
            _UTILITY_EXPENSE_UPDATE_GUARD_TRIGGER,
            _UTILITY_READING_UPDATE_GUARD_TRIGGER,
            _UTILITY_MODE_UPDATE_TRIGGER,
            _UTILITY_MODE_DELETE_TRIGGER,
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    elif dialect == "postgresql":
        for trigger, table in (
            (_UTILITY_EXPENSE_GUARD_TRIGGER, "finance_utility_expenses"),
            (_UTILITY_READING_GUARD_TRIGGER, "finance_utility_readings"),
            (_UTILITY_MODE_UPDATE_TRIGGER, "finance_utility_month_modes"),
            (_UTILITY_MODE_DELETE_TRIGGER, "finance_utility_month_modes"),
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {_UTILITY_GUARD_FUNCTION}()")
        op.execute(f"DROP FUNCTION IF EXISTS {_UTILITY_MODE_FUNCTION}()")


def _assert_safe_downgrade() -> None:
    bind = op.get_bind()
    used = []
    for table in (
        "supplier_payment_batches",
        "supplier_credit_lots",
        "finance_utility_expenses",
    ):
        count = int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() or 0)
        if count:
            used.append(table)
    non_default_suppliers = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM supplier_master_records "
                "WHERE settlement_day <> 20"
            )
        ).scalar_one()
        or 0
    )
    if non_default_suppliers:
        used.append("supplier_master_records(settlement_day)")
    revisions = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM supplier_monthly_statements "
                "WHERE document_revision <> 1 OR supersedes_statement_id IS NOT NULL "
                "OR settlement_day_snapshot <> 20 OR generation_origin <> 'legacy' "
                "OR source_hash IS NOT NULL OR supersede_reason IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if revisions:
        used.append("supplier_monthly_statements(revisions)")
    audited_adjustments = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM supplier_monthly_adjustments "
                "WHERE statement_version_before IS NOT NULL OR amount_before IS NOT NULL "
                "OR amount_after IS NOT NULL OR is_post_confirmation IS TRUE"
            )
        ).scalar_one()
        or 0
    )
    if audited_adjustments:
        used.append("supplier_monthly_adjustments(audit)")
    linked_payments = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM supplier_monthly_payments "
                "WHERE payment_batch_id IS NOT NULL OR supplier_credit_id IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if linked_payments:
        used.append("supplier_monthly_payments(batch_or_credit)")
    if used:
        raise RuntimeError(
            "P1-149 downgrade blocked: supplier cycle/payment facts would be lost: "
            + ", ".join(used)
        )


def upgrade() -> None:
    with op.batch_alter_table("supplier_master_records") as batch:
        batch.add_column(
            sa.Column("settlement_day", sa.Integer(), nullable=False, server_default="20")
        )
        batch.create_check_constraint(
            "ck_supplier_master_records_settlement_day",
            "settlement_day BETWEEN 1 AND 31",
        )

    _drop_receipt_reverse_guard()
    with op.batch_alter_table("supplier_monthly_statements") as batch:
        batch.add_column(
            sa.Column("document_revision", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column(
                "settlement_day_snapshot", sa.Integer(), nullable=False, server_default="20"
            )
        )
        batch.add_column(
            sa.Column(
                "generation_origin", sa.String(20), nullable=False, server_default="legacy"
            )
        )
        batch.add_column(sa.Column("source_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("supersedes_statement_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("supersede_reason", sa.String(120), nullable=True))
        batch.create_check_constraint(
            "ck_supplier_monthly_statements_document_revision",
            "document_revision >= 1",
        )
        batch.create_check_constraint(
            "ck_supplier_monthly_statements_settlement_day",
            "settlement_day_snapshot BETWEEN 1 AND 31",
        )
        batch.create_check_constraint(
            "ck_supplier_monthly_statements_generation_origin",
            "generation_origin IN ('legacy','automatic','manual','regenerate')",
        )
        batch.create_check_constraint(
            "ck_supplier_monthly_statements_revision_source",
            "((generation_origin = 'regenerate' AND supersedes_statement_id IS NOT NULL "
            "AND document_revision > 1) OR "
            "(generation_origin <> 'regenerate' AND supersedes_statement_id IS NULL "
            "AND document_revision = 1)) "
            "AND (source_hash IS NULL OR length(source_hash) = 64)",
        )
        batch.create_foreign_key(
            "fk_supplier_monthly_statements_supersedes",
            "supplier_monthly_statements",
            ["supersedes_statement_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "uq_supplier_monthly_statements_business_revision",
        "supplier_monthly_statements",
        [
            "supplier_id",
            "settlement_month",
            "currency",
            "tax_basis",
            "document_revision",
        ],
        unique=True,
        sqlite_where=sa.text("generation_origin <> 'legacy'"),
        postgresql_where=sa.text("generation_origin <> 'legacy'"),
    )
    op.create_index(
        "uq_supplier_monthly_statements_superseded_once",
        "supplier_monthly_statements",
        ["supersedes_statement_id"],
        unique=True,
        sqlite_where=sa.text("supersedes_statement_id IS NOT NULL"),
        postgresql_where=sa.text("supersedes_statement_id IS NOT NULL"),
    )
    op.create_index(
        "uq_supplier_monthly_statements_id_supplier",
        "supplier_monthly_statements",
        ["id", "supplier_id"],
        unique=True,
    )
    op.create_index(
        "uq_finance_acceptance_notes_supplier_link",
        "finance_acceptance_notes",
        ["id", "supplier_id", "supplier_statement_id", "amount"],
        unique=True,
    )
    _create_receipt_reverse_guard()
    _create_statement_revision_guards()

    with op.batch_alter_table("supplier_monthly_adjustments") as batch:
        batch.add_column(sa.Column("statement_version_before", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("amount_before", sa.Numeric(18, 2), nullable=True))
        batch.add_column(sa.Column("amount_after", sa.Numeric(18, 2), nullable=True))
        batch.add_column(
            sa.Column(
                "is_post_confirmation", sa.Boolean(), nullable=False, server_default="0"
            )
        )
        batch.create_check_constraint(
            "ck_supplier_monthly_adjustments_audit",
            "(is_post_confirmation IS FALSE "
            "AND statement_version_before IS NULL AND amount_before IS NULL "
            "AND amount_after IS NULL) OR "
            "(statement_version_before >= 1 AND amount_before IS NOT NULL "
            "AND amount_after IS NOT NULL "
            "AND abs(amount_after - (amount_before + amount)) < 0.005)",
        )

    op.create_table(
        "supplier_payment_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("acceptance_note_id", sa.Integer(), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("credit_applied_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("acceptance_face_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("acceptance_applied_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("bank_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("credit_created_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("settled_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("bank_reference", sa.String(200), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()
        ),
        sa.CheckConstraint(
            "credit_applied_amount >= 0 AND acceptance_face_amount >= 0 "
            "AND acceptance_applied_amount >= 0 AND bank_amount >= 0 "
            "AND credit_created_amount >= 0 AND settled_amount > 0",
            name="ck_supplier_payment_batches_amounts",
        ),
        sa.CheckConstraint(
            "acceptance_applied_amount <= acceptance_face_amount",
            name="ck_supplier_payment_batches_acceptance_amount",
        ),
        sa.CheckConstraint(
            "(acceptance_note_id IS NULL AND acceptance_face_amount = 0 "
            "AND acceptance_applied_amount = 0 AND credit_created_amount = 0) OR "
            "(acceptance_note_id IS NOT NULL AND acceptance_face_amount > 0 "
            "AND acceptance_applied_amount > 0)",
            name="ck_supplier_payment_batches_acceptance_link",
        ),
        sa.CheckConstraint(
            "abs(settled_amount - (credit_applied_amount + "
            "acceptance_applied_amount + bank_amount)) < 0.005 "
            "AND abs(credit_created_amount - (acceptance_face_amount - "
            "acceptance_applied_amount)) < 0.005 "
            "AND (credit_created_amount = 0 OR bank_amount = 0)",
            name="ck_supplier_payment_batches_balance",
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"], ["supplier_monthly_statements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["statement_id", "supplier_id"],
            ["supplier_monthly_statements.id", "supplier_monthly_statements.supplier_id"],
            name="fk_supplier_payment_batches_statement_supplier",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["supplier_master_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["acceptance_note_id"], ["finance_acceptance_notes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            [
                "acceptance_note_id",
                "supplier_id",
                "statement_id",
                "acceptance_face_amount",
            ],
            [
                "finance_acceptance_notes.id",
                "finance_acceptance_notes.supplier_id",
                "finance_acceptance_notes.supplier_statement_id",
                "finance_acceptance_notes.amount",
            ],
            name="fk_supplier_payment_batches_acceptance_supplier",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "acceptance_note_id", name="uq_supplier_payment_batches_acceptance"
        ),
        sa.UniqueConstraint(
            "id", "statement_id", name="uq_supplier_payment_batches_id_statement"
        ),
        sa.UniqueConstraint(
            "id",
            "acceptance_note_id",
            "statement_id",
            "acceptance_applied_amount",
            name="uq_supplier_payment_batches_acceptance_payment",
        ),
        sa.UniqueConstraint(
            "id",
            "supplier_id",
            "statement_id",
            "acceptance_note_id",
            "credit_created_amount",
            name="uq_supplier_payment_batches_credit_source",
        ),
    )
    op.create_index(
        "ix_supplier_payment_batches_statement",
        "supplier_payment_batches",
        ["statement_id", "payment_date"],
    )
    op.create_index(
        "ix_supplier_payment_batches_supplier",
        "supplier_payment_batches",
        ["supplier_id", "payment_date"],
    )

    op.create_table(
        "supplier_credit_lots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("supplier_name_snapshot", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_acceptance_note_id", sa.Integer(), nullable=True),
        sa.Column("source_statement_id", sa.Integer(), nullable=False),
        sa.Column("source_payment_batch_id", sa.Integer(), nullable=True),
        sa.Column("original_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("available_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="available"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()
        ),
        sa.CheckConstraint(
            "source_type IN ('acceptance_overpayment','statement_adjustment')",
            name="ck_supplier_credit_lots_source_type",
        ),
        sa.CheckConstraint(
            "original_amount > 0 AND available_amount >= 0 "
            "AND available_amount <= original_amount",
            name="ck_supplier_credit_lots_amounts",
        ),
        sa.CheckConstraint(
            "status IN ('available','partial','exhausted','voided')",
            name="ck_supplier_credit_lots_status",
        ),
        sa.CheckConstraint(
            "(source_type = 'acceptance_overpayment' "
            "AND source_acceptance_note_id IS NOT NULL "
            "AND source_payment_batch_id IS NOT NULL) OR "
            "(source_type = 'statement_adjustment' "
            "AND source_acceptance_note_id IS NULL "
            "AND source_payment_batch_id IS NULL)",
            name="ck_supplier_credit_lots_source_link",
        ),
        sa.CheckConstraint(
            "(status = 'available' AND available_amount = original_amount) OR "
            "(status = 'partial' AND available_amount > 0 "
            "AND available_amount < original_amount) OR "
            "(status = 'exhausted' AND available_amount = 0) OR status = 'voided'",
            name="ck_supplier_credit_lots_balance_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_supplier_credit_lots_version"),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["supplier_master_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_statement_id", "supplier_id"],
            ["supplier_monthly_statements.id", "supplier_monthly_statements.supplier_id"],
            name="fk_supplier_credit_lots_statement_supplier",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_acceptance_note_id"],
            ["finance_acceptance_notes.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_statement_id"],
            ["supplier_monthly_statements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_payment_batch_id"],
            ["supplier_payment_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "source_payment_batch_id",
                "supplier_id",
                "source_statement_id",
                "source_acceptance_note_id",
                "original_amount",
            ],
            [
                "supplier_payment_batches.id",
                "supplier_payment_batches.supplier_id",
                "supplier_payment_batches.statement_id",
                "supplier_payment_batches.acceptance_note_id",
                "supplier_payment_batches.credit_created_amount",
            ],
            name="fk_supplier_credit_lots_acceptance_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "source_acceptance_note_id", name="uq_supplier_credit_lots_source_acceptance"
        ),
    )
    op.create_index(
        "ix_supplier_credit_lots_supplier",
        "supplier_credit_lots",
        ["supplier_id", "status", "id"],
    )

    with op.batch_alter_table("supplier_monthly_payments") as batch:
        batch.drop_constraint("ck_supplier_monthly_payments_acceptance_link", type_="check")
        batch.drop_constraint("ck_supplier_monthly_payments_method", type_="check")
        batch.add_column(sa.Column("payment_batch_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("supplier_credit_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_supplier_monthly_payments_batch",
            "supplier_payment_batches",
            ["payment_batch_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_supplier_monthly_payments_batch_statement",
            "supplier_payment_batches",
            ["payment_batch_id", "statement_id"],
            ["id", "statement_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_supplier_monthly_payments_acceptance_batch",
            "supplier_payment_batches",
            ["payment_batch_id", "acceptance_note_id", "statement_id", "amount"],
            ["id", "acceptance_note_id", "statement_id", "acceptance_applied_amount"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_supplier_monthly_payments_credit",
            "supplier_credit_lots",
            ["supplier_credit_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_supplier_monthly_payments_method",
            "payment_method IN ('bank','acceptance','credit')",
        )
        batch.create_check_constraint(
            "ck_supplier_monthly_payments_acceptance_link",
            "(payment_method = 'bank' AND acceptance_note_id IS NULL "
            "AND supplier_credit_id IS NULL) OR "
            "(payment_method = 'acceptance' AND acceptance_note_id IS NOT NULL "
            "AND supplier_credit_id IS NULL) OR "
            "(payment_method = 'credit' AND acceptance_note_id IS NULL "
            "AND supplier_credit_id IS NOT NULL AND payment_batch_id IS NOT NULL)",
        )

    op.create_table(
        "finance_utility_month_modes",
        sa.Column("cost_month", sa.String(7), primary_key=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.CheckConstraint(
            "mode IN ('legacy','combined')",
            name="ck_finance_utility_month_modes_mode",
        ),
        sa.CheckConstraint(
            "length(cost_month) = 7 AND substr(cost_month, 5, 1) = '-' "
            "AND substr(cost_month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(cost_month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(cost_month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(cost_month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(cost_month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_finance_utility_month_modes_month",
        ),
    )
    op.execute(
        "INSERT INTO finance_utility_month_modes(cost_month, mode) "
        "SELECT DISTINCT cost_month, 'legacy' FROM finance_utility_readings"
    )

    op.create_table(
        "finance_utility_expenses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cost_month", sa.String(7), nullable=False),
        sa.Column("cost_center_id", sa.Integer(), nullable=False),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("invoice_number", sa.String(120), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("invoice_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("paid_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("cost_pool_entry_id", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()
        ),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "total_amount > 0 AND (invoice_amount IS NULL OR invoice_amount >= 0) "
            "AND paid_amount >= 0 AND paid_amount <= total_amount",
            name="ck_finance_utility_expenses_amounts",
        ),
        sa.CheckConstraint(
            "((invoice_number IS NULL AND invoice_date IS NULL AND invoice_amount IS NULL) "
            "OR (invoice_number IS NOT NULL AND length(trim(invoice_number)) > 0 "
            "AND invoice_date IS NOT NULL AND invoice_amount IS NOT NULL)) "
            "AND ((paid_amount = 0 AND payment_date IS NULL) "
            "OR (paid_amount > 0 AND payment_date IS NOT NULL))",
            name="ck_finance_utility_expenses_facts",
        ),
        sa.CheckConstraint("version >= 1", name="ck_finance_utility_expenses_version"),
        sa.CheckConstraint(
            "length(cost_month) = 7 AND substr(cost_month, 5, 1) = '-' "
            "AND substr(cost_month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(cost_month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(cost_month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(cost_month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(cost_month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_finance_utility_expenses_month",
        ),
        sa.ForeignKeyConstraint(
            ["cost_center_id"], ["finance_cost_centers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["cost_pool_entry_id"], ["finance_cost_pool_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("cost_month", name="uq_finance_utility_expenses_month"),
        sa.UniqueConstraint(
            "cost_pool_entry_id", name="uq_finance_utility_expenses_cost_entry"
        ),
    )
    op.create_index(
        "ix_finance_utility_expenses_month", "finance_utility_expenses", ["cost_month"]
    )
    _create_adjustment_validation()
    _create_adjustment_immutability()
    _create_financial_integrity_guards()
    _create_utility_month_guards()


def downgrade() -> None:
    _assert_safe_downgrade()
    _drop_utility_month_guards()
    _drop_financial_integrity_guards()
    _drop_adjustment_validation()
    _drop_adjustment_immutability()
    _drop_statement_revision_guards()
    op.drop_index("ix_finance_utility_expenses_month", table_name="finance_utility_expenses")
    op.drop_table("finance_utility_expenses")
    op.drop_table("finance_utility_month_modes")

    with op.batch_alter_table("supplier_monthly_payments") as batch:
        batch.drop_constraint("ck_supplier_monthly_payments_acceptance_link", type_="check")
        batch.drop_constraint("ck_supplier_monthly_payments_method", type_="check")
        batch.drop_constraint(
            "fk_supplier_monthly_payments_acceptance_batch", type_="foreignkey"
        )
        batch.drop_constraint(
            "fk_supplier_monthly_payments_batch_statement", type_="foreignkey"
        )
        batch.drop_constraint("fk_supplier_monthly_payments_credit", type_="foreignkey")
        batch.drop_constraint("fk_supplier_monthly_payments_batch", type_="foreignkey")
        batch.drop_column("supplier_credit_id")
        batch.drop_column("payment_batch_id")
        batch.create_check_constraint(
            "ck_supplier_monthly_payments_method",
            "payment_method IN ('bank','acceptance')",
        )
        batch.create_check_constraint(
            "ck_supplier_monthly_payments_acceptance_link",
            "(payment_method = 'bank' AND acceptance_note_id IS NULL) OR "
            "(payment_method = 'acceptance' AND acceptance_note_id IS NOT NULL)",
        )

    op.drop_index("ix_supplier_credit_lots_supplier", table_name="supplier_credit_lots")
    op.drop_table("supplier_credit_lots")
    op.drop_index("ix_supplier_payment_batches_supplier", table_name="supplier_payment_batches")
    op.drop_index("ix_supplier_payment_batches_statement", table_name="supplier_payment_batches")
    op.drop_table("supplier_payment_batches")
    op.drop_index(
        "uq_finance_acceptance_notes_supplier_link",
        table_name="finance_acceptance_notes",
    )

    with op.batch_alter_table("supplier_monthly_adjustments") as batch:
        batch.drop_constraint("ck_supplier_monthly_adjustments_audit", type_="check")
        batch.drop_column("is_post_confirmation")
        batch.drop_column("amount_after")
        batch.drop_column("amount_before")
        batch.drop_column("statement_version_before")

    op.drop_index(
        "uq_supplier_monthly_statements_id_supplier",
        table_name="supplier_monthly_statements",
    )
    op.drop_index(
        "uq_supplier_monthly_statements_superseded_once",
        table_name="supplier_monthly_statements",
    )
    op.drop_index(
        "uq_supplier_monthly_statements_business_revision",
        table_name="supplier_monthly_statements",
    )
    _drop_receipt_reverse_guard()
    with op.batch_alter_table("supplier_monthly_statements") as batch:
        batch.drop_constraint("fk_supplier_monthly_statements_supersedes", type_="foreignkey")
        batch.drop_constraint(
            "ck_supplier_monthly_statements_generation_origin", type_="check"
        )
        batch.drop_constraint(
            "ck_supplier_monthly_statements_revision_source", type_="check"
        )
        batch.drop_constraint("ck_supplier_monthly_statements_settlement_day", type_="check")
        batch.drop_constraint(
            "ck_supplier_monthly_statements_document_revision", type_="check"
        )
        batch.drop_column("supersede_reason")
        batch.drop_column("supersedes_statement_id")
        batch.drop_column("source_hash")
        batch.drop_column("generation_origin")
        batch.drop_column("settlement_day_snapshot")
        batch.drop_column("document_revision")
    _create_receipt_reverse_guard()

    with op.batch_alter_table("supplier_master_records") as batch:
        batch.drop_constraint("ck_supplier_master_records_settlement_day", type_="check")
        batch.drop_column("settlement_day")
