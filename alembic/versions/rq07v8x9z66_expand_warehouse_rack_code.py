"""Allow stable multi-letter warehouse rack identities.

Revision ID: rq07v8x9z66
Revises: rp06v8x9z65
Create Date: 2026-09-07

The migration changes the schema width and its matching address constraint.
Existing rack identities and inventory/location rows are retained unchanged.
"""

from alembic import op
import sqlalchemy as sa


revision = "rq07v8x9z66"
down_revision = "rp06v8x9z65"
branch_labels = None
depends_on = None


_RACK_ADDRESS_CHECK_V1 = (
    "address_kind != 'rack_slot' OR "
    "(address_area_id IS NOT NULL AND rack_code >= 'A' AND rack_code <= 'Z' "
    "AND length(rack_code) = 1 AND level_no >= 1 AND level_no <= 99 "
    "AND slot_no >= 1 AND slot_no <= 99)"
)
_RACK_ADDRESS_CHECK_V2 = (
    "address_kind != 'rack_slot' OR "
    "(address_area_id IS NOT NULL AND rack_code >= 'A' AND rack_code <= 'ZZZZ' "
    "AND length(rack_code) BETWEEN 1 AND 4 AND level_no >= 1 AND level_no <= 99 "
    "AND slot_no >= 1 AND slot_no <= 99)"
)


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


def _rack_address_trigger_sql(sql: str, *, target_width: int) -> str:
    if target_width == 4:
        return sql.replace(
            "length(NEW.rack_code) = 1",
            "length(NEW.rack_code) BETWEEN 1 AND 4",
        ).replace("NEW.rack_code <= 'Z'", "NEW.rack_code <= 'ZZZZ'")
    return sql.replace(
        "length(NEW.rack_code) BETWEEN 1 AND 4",
        "length(NEW.rack_code) = 1",
    ).replace("NEW.rack_code <= 'ZZZZ'", "NEW.rack_code <= 'Z'")


def _alter_rack_code_width(
    *,
    source_width: int,
    target_width: int,
    target_check: str,
) -> None:
    bind = op.get_bind()
    rack_constraint_exists = any(
        item.get("name") == "ck_warehouse_locations_rack_address"
        for item in sa.inspect(bind).get_check_constraints("warehouse_locations")
    )
    if bind.dialect.name != "sqlite":
        if rack_constraint_exists:
            op.drop_constraint(
                "ck_warehouse_locations_rack_address",
                "warehouse_locations",
                type_="check",
            )
        op.alter_column(
            "warehouse_locations",
            "rack_code",
            existing_type=sa.String(length=source_width),
            type_=sa.String(length=target_width),
            existing_nullable=True,
        )
        op.create_check_constraint(
            "ck_warehouse_locations_rack_address",
            "warehouse_locations",
            target_check,
        )
        return
    triggers = _sqlite_location_triggers()
    for name, _sql in triggers:
        op.execute(f'DROP TRIGGER "{name.replace(chr(34), chr(34) * 2)}"')
    try:
        with op.batch_alter_table("warehouse_locations") as batch_op:
            if rack_constraint_exists:
                batch_op.drop_constraint(
                    "ck_warehouse_locations_rack_address",
                    type_="check",
                )
            batch_op.alter_column(
                "rack_code",
                existing_type=sa.String(length=source_width),
                type_=sa.String(length=target_width),
                existing_nullable=True,
            )
            batch_op.create_check_constraint(
                "ck_warehouse_locations_rack_address",
                target_check,
            )
    except Exception:
        for _name, sql in triggers:
            op.execute(sql)
        raise
    structured_address_triggers = {
        "trg_warehouse_locations_structured_address_insert",
        "trg_warehouse_locations_structured_address_update",
    }
    for name, sql in triggers:
        if name in structured_address_triggers:
            sql = _rack_address_trigger_sql(sql, target_width=target_width)
        op.execute(sql)


def upgrade() -> None:
    _alter_rack_code_width(
        source_width=1,
        target_width=4,
        target_check=_RACK_ADDRESS_CHECK_V2,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT 1 FROM warehouse_locations "
            "WHERE rack_code IS NOT NULL AND length(rack_code) > 1 LIMIT 1"
        )
    ).first():
        raise RuntimeError("Multi-letter rack identities exist; downgrade refused")
    _alter_rack_code_width(
        source_width=4,
        target_width=1,
        target_check=_RACK_ADDRESS_CHECK_V1,
    )
