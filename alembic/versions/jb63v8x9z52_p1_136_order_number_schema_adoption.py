"""adopt the order-item numbering schema into the Alembic chain

Revision ID: jb63v8x9z52
Revises: ja62v8x9z51
Create Date: 2026-09-01

The order-numbering objects were historically installed by a guarded admin
script, so a running database can already contain all or part of this schema.
This migration validates every existing object before issuing DDL, adopts a
complete legacy structure without rewriting business facts, and only fills in
missing schema objects.  Existing order items are never renumbered.
"""

from __future__ import annotations

from collections.abc import Mapping

from alembic import op
import sqlalchemy as sa


revision = "jb63v8x9z52"
down_revision = "ja62v8x9z51"
branch_labels = None
depends_on = None


ORDER_TABLE = "sales_orders"
ITEM_TABLE = "sales_order_items"
SEQUENCE_TABLE = "order_item_number_sequences"
ADOPTION_STATE_TABLE = "p1_136_order_number_schema_adoption_state"

ORDER_INDEXES: Mapping[str, tuple[bool, tuple[str, ...]]] = {
    "ix_sales_orders_customer_po": (False, ("customer_po",)),
    "ix_sales_orders_customer_po_group": (
        False,
        ("customer_id", "customer_po"),
    ),
}
ITEM_INDEXES: Mapping[str, tuple[bool, tuple[str, ...]]] = {
    "ux_sales_order_items_item_order_number": (
        True,
        ("item_order_number",),
    ),
    "ix_sales_order_items_snapshot_product_code": (
        False,
        ("snapshot_product_code",),
    ),
    "ix_sales_order_items_snapshot_product_name": (
        False,
        ("snapshot_product_name",),
    ),
}


def _columns(inspector: sa.Inspector, table_name: str) -> dict[str, dict]:
    return {
        str(column["name"]): column
        for column in inspector.get_columns(table_name)
    }


def _require_base_tables(inspector: sa.Inspector) -> None:
    tables = set(inspector.get_table_names())
    missing = {ORDER_TABLE, ITEM_TABLE} - tables
    if missing:
        raise RuntimeError(
            "P1-136 upgrade blocked: required base tables are missing: "
            + ", ".join(sorted(missing))
        )


def _validate_item_columns(
    connection: sa.Connection,
    inspector: sa.Inspector,
) -> tuple[bool, bool]:
    columns = _columns(inspector, ITEM_TABLE)
    number_column = columns.get("item_order_number")
    sequence_column = columns.get("item_sequence")

    if number_column is not None:
        number_type = number_column["type"]
        length = getattr(number_type, "length", None)
        if not isinstance(number_type, (sa.String, sa.Text)) or (
            length is not None and length < 64
        ):
            raise RuntimeError(
                "P1-136 upgrade blocked: sales_order_items.item_order_number "
                "must be TEXT or VARCHAR(64+)."
            )
        if not bool(number_column.get("nullable")):
            raise RuntimeError(
                "P1-136 upgrade blocked: sales_order_items.item_order_number "
                "must be nullable."
            )

    if sequence_column is not None:
        if not isinstance(sequence_column["type"], sa.Integer):
            raise RuntimeError(
                "P1-136 upgrade blocked: sales_order_items.item_sequence "
                "must be INTEGER."
            )
        if not bool(sequence_column.get("nullable")):
            raise RuntimeError(
                "P1-136 upgrade blocked: sales_order_items.item_sequence "
                "must be nullable."
            )

    has_number = number_column is not None
    has_sequence = sequence_column is not None
    if has_number:
        duplicate = connection.execute(
            sa.text(
                f"SELECT item_order_number, COUNT(*) AS item_count "
                f"FROM {ITEM_TABLE} "
                "WHERE item_order_number IS NOT NULL "
                "GROUP BY item_order_number HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).mappings().one_or_none()
        if duplicate is not None:
            raise RuntimeError(
                "P1-136 upgrade blocked: duplicate item_order_number exists: "
                f"{duplicate['item_order_number']!r}."
            )

    if has_sequence:
        invalid_sequence = connection.execute(
            sa.text(
                f"SELECT id, item_sequence FROM {ITEM_TABLE} "
                "WHERE item_sequence IS NOT NULL AND item_sequence <= 0 LIMIT 1"
            )
        ).mappings().one_or_none()
        if invalid_sequence is not None:
            raise RuntimeError(
                "P1-136 upgrade blocked: item_sequence must be positive; "
                f"item {invalid_sequence['id']} is invalid."
            )

    if has_number and has_sequence:
        unpaired = connection.execute(
            sa.text(
                f"SELECT id FROM {ITEM_TABLE} "
                "WHERE (item_order_number IS NULL AND item_sequence IS NOT NULL) "
                "OR (item_order_number IS NOT NULL AND item_sequence IS NULL) "
                "LIMIT 1"
            )
        ).scalar_one_or_none()
        if unpaired is not None:
            raise RuntimeError(
                "P1-136 upgrade blocked: item_order_number and item_sequence "
                f"must be paired; item {unpaired} is inconsistent."
            )
    elif has_number:
        populated = connection.execute(
            sa.text(
                f"SELECT id FROM {ITEM_TABLE} "
                "WHERE item_order_number IS NOT NULL LIMIT 1"
            )
        ).scalar_one_or_none()
        if populated is not None:
            raise RuntimeError(
                "P1-136 upgrade blocked: item_sequence is missing while "
                f"numbered item {populated} already exists."
            )
    elif has_sequence:
        populated = connection.execute(
            sa.text(
                f"SELECT id FROM {ITEM_TABLE} "
                "WHERE item_sequence IS NOT NULL LIMIT 1"
            )
        ).scalar_one_or_none()
        if populated is not None:
            raise RuntimeError(
                "P1-136 upgrade blocked: item_order_number is missing while "
                f"sequenced item {populated} already exists."
            )

    return has_number, has_sequence


def _validate_sequence_table(
    connection: sa.Connection,
    inspector: sa.Inspector,
    *,
    item_sequence_exists: bool,
) -> bool:
    if SEQUENCE_TABLE not in set(inspector.get_table_names()):
        return False

    columns = _columns(inspector, SEQUENCE_TABLE)
    required = {"order_id", "last_item_sequence", "updated_at"}
    if not required <= columns.keys():
        raise RuntimeError(
            "P1-136 upgrade blocked: order_item_number_sequences has an "
            "incomplete column contract."
        )
    if not isinstance(columns["order_id"]["type"], sa.Integer):
        raise RuntimeError(
            "P1-136 upgrade blocked: order_item_number_sequences.order_id "
            "must be INTEGER."
        )
    if not isinstance(columns["last_item_sequence"]["type"], sa.Integer):
        raise RuntimeError(
            "P1-136 upgrade blocked: last_item_sequence must be INTEGER."
        )
    if bool(columns["last_item_sequence"].get("nullable")):
        raise RuntimeError(
            "P1-136 upgrade blocked: last_item_sequence must be non-null."
        )
    timestamp_type = columns["updated_at"]["type"]
    if not isinstance(timestamp_type, (sa.DateTime, sa.String, sa.Text)):
        raise RuntimeError(
            "P1-136 upgrade blocked: updated_at must be DATETIME or TEXT."
        )
    if bool(columns["updated_at"].get("nullable")):
        raise RuntimeError(
            "P1-136 upgrade blocked: updated_at must be non-null."
        )

    primary_key = tuple(
        inspector.get_pk_constraint(SEQUENCE_TABLE).get("constrained_columns")
        or ()
    )
    if primary_key != ("order_id",):
        raise RuntimeError(
            "P1-136 upgrade blocked: order_item_number_sequences.order_id "
            "must be the sole primary key."
        )
    foreign_keys = inspector.get_foreign_keys(SEQUENCE_TABLE)
    matching_foreign_keys = [
        foreign_key
        for foreign_key in foreign_keys
        if tuple(foreign_key.get("constrained_columns") or ()) == ("order_id",)
        and foreign_key.get("referred_table") == ORDER_TABLE
        and tuple(foreign_key.get("referred_columns") or ()) == ("id",)
        and str((foreign_key.get("options") or {}).get("ondelete", "")).upper()
        == "CASCADE"
    ]
    if len(matching_foreign_keys) != 1 or len(foreign_keys) != 1:
        raise RuntimeError(
            "P1-136 upgrade blocked: order_item_number_sequences must have "
            "one CASCADE foreign key to sales_orders.id."
        )

    invalid_floor = connection.execute(
        sa.text(
            f"SELECT order_id FROM {SEQUENCE_TABLE} "
            "WHERE last_item_sequence < 0 LIMIT 1"
        )
    ).scalar_one_or_none()
    if invalid_floor is not None:
        raise RuntimeError(
            "P1-136 upgrade blocked: sequence floor cannot be negative; "
            f"order {invalid_floor} is invalid."
        )
    orphan = connection.execute(
        sa.text(
            f"SELECT sequence.order_id FROM {SEQUENCE_TABLE} AS sequence "
            f"LEFT JOIN {ORDER_TABLE} AS orders ON orders.id = sequence.order_id "
            "WHERE orders.id IS NULL LIMIT 1"
        )
    ).scalar_one_or_none()
    if orphan is not None:
        raise RuntimeError(
            "P1-136 upgrade blocked: sequence row references a missing order: "
            f"{orphan}."
        )
    if item_sequence_exists:
        low_floor = connection.execute(
            sa.text(
                f"SELECT sequence.order_id, sequence.last_item_sequence, "
                "MAX(items.item_sequence) AS max_item_sequence "
                f"FROM {SEQUENCE_TABLE} AS sequence "
                f"JOIN {ITEM_TABLE} AS items ON items.order_id = sequence.order_id "
                "WHERE items.item_sequence IS NOT NULL "
                "GROUP BY sequence.order_id, sequence.last_item_sequence "
                "HAVING sequence.last_item_sequence < MAX(items.item_sequence) "
                "LIMIT 1"
            )
        ).mappings().one_or_none()
        if low_floor is not None:
            raise RuntimeError(
                "P1-136 upgrade blocked: sequence floor is below existing "
                f"item_sequence for order {low_floor['order_id']}."
            )
    return True


def _validate_indexes(inspector: sa.Inspector) -> None:
    expected_by_name = {
        **{
            name: (ORDER_TABLE, unique, columns)
            for name, (unique, columns) in ORDER_INDEXES.items()
        },
        **{
            name: (ITEM_TABLE, unique, columns)
            for name, (unique, columns) in ITEM_INDEXES.items()
        },
    }
    actual_by_name: dict[str, tuple[str, bool, tuple[str, ...]]] = {}
    for table_name in inspector.get_table_names():
        for index in inspector.get_indexes(table_name):
            name = str(index["name"])
            actual_by_name[name] = (
                table_name,
                bool(index.get("unique")),
                tuple(index.get("column_names") or ()),
            )
    for name, expected in expected_by_name.items():
        actual = actual_by_name.get(name)
        if actual is not None and actual != expected:
            raise RuntimeError(
                f"P1-136 upgrade blocked: index {name} has an incompatible "
                f"contract; expected {expected}, found {actual}."
            )


def _preexisting_object_names(
    inspector: sa.Inspector,
    *,
    has_number: bool,
    has_sequence: bool,
    has_sequence_table: bool,
) -> set[str]:
    object_names: set[str] = set()
    if has_number:
        object_names.add(f"column:{ITEM_TABLE}.item_order_number")
    if has_sequence:
        object_names.add(f"column:{ITEM_TABLE}.item_sequence")
    if has_sequence_table:
        object_names.add(f"table:{SEQUENCE_TABLE}")
    expected_indexes = set(ORDER_INDEXES) | set(ITEM_INDEXES)
    for table_name in (ORDER_TABLE, ITEM_TABLE):
        for index in inspector.get_indexes(table_name):
            name = str(index["name"])
            if name in expected_indexes:
                object_names.add(f"index:{name}")
    return object_names


def _create_missing_indexes(connection: sa.Connection) -> None:
    inspector = sa.inspect(connection)
    for table_name, contracts in (
        (ORDER_TABLE, ORDER_INDEXES),
        (ITEM_TABLE, ITEM_INDEXES),
    ):
        existing = {index["name"] for index in inspector.get_indexes(table_name)}
        for name, (unique, columns) in contracts.items():
            if name not in existing:
                op.create_index(name, table_name, list(columns), unique=unique)


def _assert_final_contract(connection: sa.Connection) -> None:
    inspector = sa.inspect(connection)
    _require_base_tables(inspector)
    has_number, has_sequence = _validate_item_columns(connection, inspector)
    if not has_number or not has_sequence:
        raise RuntimeError(
            "P1-136 upgrade failed: order-item numbering columns are incomplete."
        )
    if not _validate_sequence_table(
        connection,
        inspector,
        item_sequence_exists=True,
    ):
        raise RuntimeError(
            "P1-136 upgrade failed: order-item sequence table is missing."
        )
    _validate_indexes(inspector)
    expected_names = set(ORDER_INDEXES) | set(ITEM_INDEXES)
    actual_names = {
        str(index["name"])
        for table_name in (ORDER_TABLE, ITEM_TABLE)
        for index in inspector.get_indexes(table_name)
    }
    if not expected_names <= actual_names:
        raise RuntimeError(
            "P1-136 upgrade failed: expected order-numbering indexes are missing."
        )


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    _require_base_tables(inspector)
    if ADOPTION_STATE_TABLE in set(inspector.get_table_names()):
        raise RuntimeError(
            "P1-136 upgrade blocked: an unexpected adoption-state table "
            "already exists."
        )
    has_number, has_sequence = _validate_item_columns(connection, inspector)
    has_sequence_table = _validate_sequence_table(
        connection,
        inspector,
        item_sequence_exists=has_sequence,
    )
    _validate_indexes(inspector)
    preexisting_objects = _preexisting_object_names(
        inspector,
        has_number=has_number,
        has_sequence=has_sequence,
        has_sequence_table=has_sequence_table,
    )

    item_columns = _columns(inspector, ITEM_TABLE)
    required_index_columns = {
        "customer_po": _columns(inspector, ORDER_TABLE),
        "customer_id": _columns(inspector, ORDER_TABLE),
        "snapshot_product_code": item_columns,
        "snapshot_product_name": item_columns,
    }
    for column_name, columns in required_index_columns.items():
        if column_name not in columns:
            raise RuntimeError(
                "P1-136 upgrade blocked: required index column is missing: "
                f"{column_name}."
            )

    if not has_number:
        op.add_column(
            ITEM_TABLE,
            sa.Column("item_order_number", sa.String(length=64), nullable=True),
        )
    if not has_sequence:
        op.add_column(
            ITEM_TABLE,
            sa.Column("item_sequence", sa.Integer(), nullable=True),
        )

    if not has_sequence_table:
        op.create_table(
            SEQUENCE_TABLE,
            sa.Column("order_id", sa.Integer(), primary_key=True),
            sa.Column(
                "last_item_sequence",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.ForeignKeyConstraint(
                ["order_id"],
                [f"{ORDER_TABLE}.id"],
                ondelete="CASCADE",
            ),
        )
        op.execute(
            sa.text(
                f"INSERT INTO {SEQUENCE_TABLE} "
                "(order_id, last_item_sequence, updated_at) "
                f"SELECT order_id, MAX(item_sequence), CURRENT_TIMESTAMP "
                f"FROM {ITEM_TABLE} WHERE item_sequence IS NOT NULL "
                "GROUP BY order_id"
            )
        )

    _create_missing_indexes(connection)
    _assert_final_contract(connection)
    op.create_table(
        ADOPTION_STATE_TABLE,
        sa.Column("object_name", sa.String(length=200), primary_key=True),
    )
    if preexisting_objects:
        connection.execute(
            sa.text(
                f"INSERT INTO {ADOPTION_STATE_TABLE} (object_name) "
                "VALUES (:object_name)"
            ),
            [
                {"object_name": object_name}
                for object_name in sorted(preexisting_objects)
            ],
        )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if ADOPTION_STATE_TABLE not in set(inspector.get_table_names()):
        raise RuntimeError(
            "P1-136 downgrade blocked: adoption ownership metadata is missing."
        )
    for table_name in (ORDER_TABLE, ITEM_TABLE, SEQUENCE_TABLE):
        if table_name not in set(inspector.get_table_names()):
            continue
        row_count = int(
            connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {table_name}")
            ).scalar_one()
        )
        if row_count:
            raise RuntimeError(
                "P1-136 downgrade blocked: order numbering facts would be lost."
            )

    preexisting_objects = {
        str(row[0])
        for row in connection.execute(
            sa.text(f"SELECT object_name FROM {ADOPTION_STATE_TABLE}")
        ).all()
    }

    for table_name, contracts in (
        (ITEM_TABLE, ITEM_INDEXES),
        (ORDER_TABLE, ORDER_INDEXES),
    ):
        existing = {
            index["name"]
            for index in sa.inspect(connection).get_indexes(table_name)
        }
        for name in contracts:
            if name in existing and f"index:{name}" not in preexisting_objects:
                op.drop_index(name, table_name=table_name)

    if (
        SEQUENCE_TABLE in set(sa.inspect(connection).get_table_names())
        and f"table:{SEQUENCE_TABLE}" not in preexisting_objects
    ):
        op.drop_table(SEQUENCE_TABLE)

    item_columns = _columns(sa.inspect(connection), ITEM_TABLE)
    columns_to_drop = [
        column_name
        for column_name in ("item_sequence", "item_order_number")
        if column_name in item_columns
        and f"column:{ITEM_TABLE}.{column_name}" not in preexisting_objects
    ]
    if columns_to_drop:
        recreate = "always" if connection.dialect.name == "sqlite" else "auto"
        with op.batch_alter_table(ITEM_TABLE, recreate=recreate) as batch_op:
            for column_name in columns_to_drop:
                batch_op.drop_column(column_name)
    op.drop_table(ADOPTION_STATE_TABLE)
