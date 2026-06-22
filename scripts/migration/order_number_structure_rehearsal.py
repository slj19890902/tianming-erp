from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path


@dataclass(slots=True)
class RehearsalOrder:
    order_id: int
    order_number: str
    customer_id: int
    customer_po: str | None


def create_rehearsal_copy(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def connect_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA index_list({table})")}


def ensure_rehearsal_schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
    added_columns: list[str] = []
    added_tables: list[str] = []
    added_indexes: list[str] = []

    item_columns = table_columns(conn, "sales_order_items")
    if "item_order_number" not in item_columns:
        conn.execute("ALTER TABLE sales_order_items ADD COLUMN item_order_number VARCHAR(64)")
        added_columns.append("sales_order_items.item_order_number")
    if "item_sequence" not in item_columns:
        conn.execute("ALTER TABLE sales_order_items ADD COLUMN item_sequence INTEGER")
        added_columns.append("sales_order_items.item_sequence")

    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "order_item_number_sequences" not in tables:
        conn.execute(
            """
            CREATE TABLE order_item_number_sequences (
                order_id INTEGER PRIMARY KEY,
                last_item_seq INTEGER NOT NULL,
                FOREIGN KEY(order_id) REFERENCES sales_orders(id) ON DELETE CASCADE
            )
            """
        )
        added_tables.append("order_item_number_sequences")

    existing_order_indexes = index_names(conn, "sales_orders")
    existing_item_indexes = index_names(conn, "sales_order_items")

    order_index_sql = {
        "ix_sales_orders_customer_po": "CREATE INDEX ix_sales_orders_customer_po ON sales_orders (customer_po)",
        "ix_sales_orders_customer_po_group": "CREATE INDEX ix_sales_orders_customer_po_group ON sales_orders (customer_id, customer_po)",
    }
    item_index_sql = {
        "ux_sales_order_items_item_order_number": "CREATE UNIQUE INDEX ux_sales_order_items_item_order_number ON sales_order_items (item_order_number)",
        "ix_sales_order_items_snapshot_product_code": "CREATE INDEX ix_sales_order_items_snapshot_product_code ON sales_order_items (snapshot_product_code)",
        "ix_sales_order_items_snapshot_product_name": "CREATE INDEX ix_sales_order_items_snapshot_product_name ON sales_order_items (snapshot_product_name)",
    }

    for name, sql in order_index_sql.items():
        if name not in existing_order_indexes:
            conn.execute(sql)
            added_indexes.append(name)
    for name, sql in item_index_sql.items():
        if name not in existing_item_indexes:
            conn.execute(sql)
            added_indexes.append(name)

    conn.commit()
    return {
        "added_columns": added_columns,
        "added_tables": added_tables,
        "added_indexes": added_indexes,
    }


def next_order_number(conn: sqlite3.Connection, order_date: date) -> str:
    day = order_date.isoformat()
    conn.execute(
        """
        INSERT INTO order_daily_sequences (sequence_date, last_value)
        VALUES (?, 1)
        ON CONFLICT(sequence_date)
        DO UPDATE SET last_value = last_value + 1
        """,
        (day,),
    )
    seq = conn.execute(
        "SELECT last_value FROM order_daily_sequences WHERE sequence_date = ?",
        (day,),
    ).fetchone()[0]
    if seq > 999:
        raise ValueError("daily order sequence overflow")
    return f"TM{order_date:%Y%m%d}{seq:03d}"


def next_item_sequence(conn: sqlite3.Connection, order_id: int) -> int:
    columns = table_columns(conn, "order_item_number_sequences")
    sequence_column = (
        "last_item_sequence" if "last_item_sequence" in columns else "last_item_seq"
    )
    conn.execute(
        f"""
        INSERT INTO order_item_number_sequences (order_id, {sequence_column})
        VALUES (?, 1)
        ON CONFLICT(order_id)
        DO UPDATE SET {sequence_column} = {sequence_column} + 1
        """,
        (order_id,),
    )
    seq = conn.execute(
        f"SELECT {sequence_column} FROM order_item_number_sequences WHERE order_id = ?",
        (order_id,),
    ).fetchone()[0]
    return int(seq)


def create_order_with_items(
    conn: sqlite3.Connection,
    *,
    customer_id: int,
    customer_po: str | None,
    order_date: date,
    delivery_date: date | None,
    created_by: int | None,
    item_specs: list[dict[str, object]],
) -> RehearsalOrder:
    order_number = next_order_number(conn, order_date)
    conn.execute(
        """
        INSERT INTO sales_orders (
            order_number, customer_id, customer_po, order_date, delivery_date,
            status, payment_status, total_amount, remark, created_by
        ) VALUES (?, ?, ?, ?, ?, 'pending_production', 'unpaid', 0, ?, ?)
        """,
        (
            order_number,
            customer_id,
            customer_po,
            order_date.isoformat(),
            delivery_date.isoformat() if delivery_date else None,
            "副本结构演练订单",
            created_by,
        ),
    )
    order_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    total = Decimal("0")
    for item in item_specs:
        seq = next_item_sequence(conn, order_id)
        item_order_number = f"{order_number}-{seq:03d}"
        quantity = int(item["quantity"])
        unit_price = Decimal(str(item["unit_price"]))
        subtotal = quantity * unit_price
        total += subtotal
        conn.execute(
            """
            INSERT INTO sales_order_items (
                order_id, product_id, quantity, delivered_quantity, is_force_closed,
                unit_price, subtotal, material_status, snapshot_product_name,
                snapshot_product_code, snapshot_spec, snapshot_material,
                inventory_deducted_qty, requisition_status, special_process,
                item_order_number, item_sequence
            ) VALUES (?, ?, ?, 0, 0, ?, ?, 'pending', ?, ?, ?, ?, 0, '未报料', '无', ?, ?)
            """,
            (
                order_id,
                int(item["product_id"]),
                quantity,
                str(unit_price),
                str(subtotal),
                item["snapshot_product_name"],
                item["snapshot_product_code"],
                item.get("snapshot_spec"),
                item.get("snapshot_material"),
                item_order_number,
                seq,
            ),
        )
    conn.execute(
        "UPDATE sales_orders SET total_amount = ? WHERE id = ?",
        (str(total), order_id),
    )
    conn.commit()
    return RehearsalOrder(
        order_id=order_id,
        order_number=order_number,
        customer_id=customer_id,
        customer_po=customer_po,
    )


def delete_order_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM sales_order_items WHERE id = ?", (item_id,))
    conn.commit()


def group_orders(rows: list[sqlite3.Row]) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for row in rows:
        customer_po = row["customer_po"]
        if customer_po is None or str(customer_po).strip() == "":
            key = f"single:{row['order_number']}"
        else:
            key = f"group:{row['customer_id']}::{customer_po}"
        grouped.setdefault(key, []).append(int(row["id"]))
    return grouped


def search_orders(
    conn: sqlite3.Connection,
    *,
    keyword: str | None = None,
    customer_name: str | None = None,
    order_date: str | None = None,
    product_keyword: str | None = None,
) -> list[sqlite3.Row]:
    sql = """
    SELECT DISTINCT so.id, so.order_number, so.customer_id, so.customer_po, so.order_date, c.name AS customer_name
    FROM sales_orders so
    JOIN customers c ON c.id = so.customer_id
    LEFT JOIN sales_order_items soi ON soi.order_id = so.id
    WHERE 1 = 1
    """
    params: list[object] = []
    if keyword:
        sql += """
        AND (
            c.name LIKE ?
            OR so.customer_po LIKE ?
            OR so.order_number LIKE ?
            OR soi.item_order_number LIKE ?
        )
        """
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    if customer_name:
        sql += " AND c.name LIKE ?"
        params.append(f"%{customer_name}%")
    if order_date:
        sql += " AND so.order_date = ?"
        params.append(order_date)
    if product_keyword:
        sql += """
        AND (
            soi.snapshot_product_code LIKE ?
            OR soi.snapshot_product_name LIKE ?
        )
        """
        like = f"%{product_keyword}%"
        params.extend([like, like])
    sql += " ORDER BY so.order_date DESC, so.id DESC"
    return conn.execute(sql, params).fetchall()


def integrity_snapshot(conn: sqlite3.Connection) -> dict[str, object]:
    return {
        "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_key_check": conn.execute("PRAGMA foreign_key_check").fetchall(),
    }
