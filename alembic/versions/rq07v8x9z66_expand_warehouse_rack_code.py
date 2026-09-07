"""Allow stable multi-letter warehouse rack identities.

Revision ID: rq07v8x9z66
Revises: rp06v8x9z65
Create Date: 2026-09-07

The migration changes only the schema width. Existing rack identities and
inventory/location rows are retained unchanged.
"""

from alembic import op
import sqlalchemy as sa


revision = "rq07v8x9z66"
down_revision = "rp06v8x9z65"
branch_labels = None
depends_on = None


def _sqlite_location_triggers() -> list[tuple[str, str]]:
    """Keep inventory guards intact while SQLite rebuilds this table."""
    return [
        (str(row.name), str(row.sql))
        for row in op.get_bind().execute(
            sa.text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND sql LIKE '%warehouse_locations%'"
            )
        ).mappings()
        if row.name and row.sql
    ]


def _alter_rack_code_width(*, source_width: int, target_width: int) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.alter_column(
            "warehouse_locations",
            "rack_code",
            existing_type=sa.String(length=source_width),
            type_=sa.String(length=target_width),
            existing_nullable=True,
        )
        return
    triggers = _sqlite_location_triggers()
    for name, _sql in triggers:
        op.execute(f'DROP TRIGGER "{name.replace(chr(34), chr(34) * 2)}"')
    try:
        with op.batch_alter_table("warehouse_locations") as batch_op:
            batch_op.alter_column(
                "rack_code",
                existing_type=sa.String(length=source_width),
                type_=sa.String(length=target_width),
                existing_nullable=True,
            )
    except Exception:
        for _name, sql in triggers:
            op.execute(sql)
        raise
    for _name, sql in triggers:
        op.execute(sql)


def upgrade() -> None:
    _alter_rack_code_width(source_width=1, target_width=4)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT 1 FROM warehouse_locations "
            "WHERE rack_code IS NOT NULL AND length(rack_code) > 1 LIMIT 1"
        )
    ).first():
        raise RuntimeError("Multi-letter rack identities exist; downgrade refused")
    _alter_rack_code_width(source_width=4, target_width=1)
