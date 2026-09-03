"""add immutable ground-layout plan retirement facts

Revision ID: jp74v8x9z63
Revises: jo73v8x9z62
Create Date: 2026-09-03

Published ground plans and slots remain immutable.  This table records that a
preserved plan is no longer operational when its empty warehouse area is
archived.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "jp74v8x9z63"
down_revision = "jo73v8x9z62"
branch_labels = None
depends_on = None


_UPDATE_TRIGGER = "trg_ground_plan_retirements_immutable_update"
_DELETE_TRIGGER = "trg_ground_plan_retirements_immutable_delete"
_INSERT_TRIGGER = "trg_ground_plan_retirements_validate_insert"
_FUNCTION = "fn_ground_plan_retirements_immutable"


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER {_INSERT_TRIGGER}
            BEFORE INSERT ON warehouse_ground_layout_plan_retirements
            WHEN NOT EXISTS (
              SELECT 1 FROM warehouse_ground_layout_plans AS plan
              WHERE plan.id = NEW.plan_id
                AND plan.area_id = NEW.area_id
                AND plan.status = 'published'
            )
            BEGIN
              SELECT RAISE(ABORT, 'retirement must match a published ground plan area');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_UPDATE_TRIGGER}
            BEFORE UPDATE ON warehouse_ground_layout_plan_retirements
            BEGIN
              SELECT RAISE(ABORT, 'ground layout plan retirement is immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_DELETE_TRIGGER}
            BEFORE DELETE ON warehouse_ground_layout_plan_retirements
            BEGIN
              SELECT RAISE(ABORT, 'ground layout plan retirement is immutable');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE FUNCTION {_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
              IF TG_OP = 'INSERT' THEN
                IF NOT EXISTS (
                  SELECT 1 FROM warehouse_ground_layout_plans AS plan
                  WHERE plan.id = NEW.plan_id
                    AND plan.area_id = NEW.area_id
                    AND plan.status = 'published'
                ) THEN
                  RAISE EXCEPTION 'retirement must match a published ground plan area';
                END IF;
                RETURN NEW;
              END IF;
              RAISE EXCEPTION 'ground layout plan retirement is immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {_UPDATE_TRIGGER} BEFORE INSERT OR UPDATE OR DELETE "
            "ON warehouse_ground_layout_plan_retirements "
            f"FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()"
        )


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_INSERT_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {_DELETE_TRIGGER}")
    elif dialect == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER} "
            "ON warehouse_ground_layout_plan_retirements"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")


def upgrade() -> None:
    op.create_table(
        "warehouse_ground_layout_plan_retirements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("area_id", sa.Integer(), nullable=False),
        sa.Column("operation_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("retired_by", sa.Integer(), nullable=False),
        sa.Column(
            "retired_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(operation_key)) > 0 AND length(request_hash) = 64 "
            "AND length(trim(snapshot_json)) > 0",
            name="ck_warehouse_ground_layout_plan_retirements_request",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["warehouse_ground_layout_plans.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["area_id"], ["warehouse_areas.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["retired_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id", name="uq_warehouse_ground_layout_plan_retirements_plan"
        ),
        sa.UniqueConstraint(
            "operation_key",
            name="uq_warehouse_ground_layout_plan_retirements_operation",
        ),
    )
    op.create_index(
        "ix_warehouse_ground_layout_plan_retirements_area",
        "warehouse_ground_layout_plan_retirements",
        ["area_id", "retired_at"],
    )
    _create_immutability_guards()


def downgrade() -> None:
    count = int(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM warehouse_ground_layout_plan_retirements"
            )
        )
        .scalar()
        or 0
    )
    if count:
        raise RuntimeError(
            "P1-153 已存在地堆排位退役事实，禁止降级丢失审计历史。"
        )
    _drop_immutability_guards()
    op.drop_index(
        "ix_warehouse_ground_layout_plan_retirements_area",
        table_name="warehouse_ground_layout_plan_retirements",
    )
    op.drop_table("warehouse_ground_layout_plan_retirements")
