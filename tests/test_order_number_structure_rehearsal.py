from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from scripts.migration.order_number_structure_rehearsal import (
    connect_rw,
    create_order_with_items,
    create_rehearsal_copy,
    delete_order_item,
    ensure_rehearsal_schema,
    group_orders,
    integrity_snapshot,
    search_orders,
)


def _copy_main_db(tmp_path: Path) -> sqlite3.Connection:
    source = Path("data/carton_erp.sqlite3").resolve()
    target = tmp_path / "rehearsal.sqlite3"
    create_rehearsal_copy(source, target)
    return connect_rw(target)


def _first_customer_and_products(conn: sqlite3.Connection) -> tuple[int, int, int]:
    row = conn.execute(
        """
        SELECT customer_id, COUNT(*) AS product_count
        FROM products
        WHERE deleted_at IS NULL
        GROUP BY customer_id
        HAVING COUNT(*) >= 2
        ORDER BY customer_id
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        customer_id = int(row["customer_id"])
        product_ids = [
            int(item[0])
            for item in conn.execute(
                "SELECT id FROM products WHERE customer_id = ? AND deleted_at IS NULL ORDER BY id LIMIT 2",
                (customer_id,),
            ).fetchall()
        ]
        return customer_id, product_ids[0], product_ids[1]

    customer_id = int(conn.execute("SELECT id FROM customers ORDER BY id LIMIT 1").fetchone()[0])
    material_row = conn.execute(
        "SELECT id, code FROM materials ORDER BY id LIMIT 1"
    ).fetchone()
    material_id = int(material_row["id"]) if material_row is not None else None
    conn.execute(
        """
        INSERT INTO products (
            customer_id, product_code, customer_material_code, product_name,
            material_id, legacy_material_text, length_mm, width_mm, height_mm,
            box_category, is_active
        ) VALUES (?, 'REHEARSAL-A', 'REHEARSAL-A', '演练产品A', ?, 'K=A', 500, 300, 200, 'normal', 1)
        """,
        (customer_id, material_id),
    )
    first_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO products (
            customer_id, product_code, customer_material_code, product_name,
            material_id, legacy_material_text, length_mm, width_mm, height_mm,
            box_category, is_active
        ) VALUES (?, 'REHEARSAL-B', 'REHEARSAL-B', '演练产品B', ?, 'A=B', 520, 320, 220, 'normal', 1)
        """,
        (customer_id, material_id),
    )
    second_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return customer_id, first_id, second_id


def test_rehearsal_schema_adds_expected_fields_indexes_and_tables(tmp_path: Path) -> None:
    conn = _copy_main_db(tmp_path)
    result = ensure_rehearsal_schema(conn)
    item_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(sales_order_items)")
    }
    item_indexes = {
        row["name"] for row in conn.execute("PRAGMA index_list(sales_order_items)")
    }
    order_indexes = {
        row["name"] for row in conn.execute("PRAGMA index_list(sales_orders)")
    }
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }

    assert "item_order_number" in item_columns
    assert "item_sequence" in item_columns
    assert "ux_sales_order_items_item_order_number" in item_indexes
    assert "ix_sales_order_items_snapshot_product_code" in item_indexes
    assert "ix_sales_order_items_snapshot_product_name" in item_indexes
    assert "ix_sales_orders_customer_po" in order_indexes
    assert "ix_sales_orders_customer_po_group" in order_indexes
    assert "order_item_number_sequences" in tables


def test_order_and_item_numbers_increment_and_do_not_reuse_after_delete(tmp_path: Path) -> None:
    conn = _copy_main_db(tmp_path)
    ensure_rehearsal_schema(conn)
    customer_id, product_a, product_b = _first_customer_and_products(conn)

    order = create_order_with_items(
        conn,
        customer_id=customer_id,
        customer_po="PO-REHEARSAL-001",
        order_date=date(2026, 6, 21),
        delivery_date=date(2026, 6, 22),
        created_by=None,
        item_specs=[
            {
                "product_id": product_a,
                "quantity": 10,
                "unit_price": Decimal("3.50"),
                "snapshot_product_code": "PX-001",
                "snapshot_product_name": "演练纸箱A",
                "snapshot_spec": "500×300×200mm",
                "snapshot_material": "K=A",
            },
            {
                "product_id": product_b,
                "quantity": 20,
                "unit_price": Decimal("2.20"),
                "snapshot_product_code": "PX-002",
                "snapshot_product_name": "演练纸箱B",
                "snapshot_spec": "520×320×220mm",
                "snapshot_material": "A=B",
            },
            {
                "product_id": product_b,
                "quantity": 30,
                "unit_price": Decimal("1.80"),
                "snapshot_product_code": "PX-003",
                "snapshot_product_name": "演练纸箱C",
                "snapshot_spec": "530×330×230mm",
                "snapshot_material": "B=C",
            },
        ],
    )

    assert order.order_number.startswith("TM20260621")
    rows = conn.execute(
        "SELECT id, item_order_number, item_sequence FROM sales_order_items WHERE order_id = ? ORDER BY id",
        (order.order_id,),
    ).fetchall()
    assert [row["item_order_number"] for row in rows] == [
        f"{order.order_number}-001",
        f"{order.order_number}-002",
        f"{order.order_number}-003",
    ]

    delete_order_item(conn, int(rows[1]["id"]))
    order_again = create_order_with_items(
        conn,
        customer_id=customer_id,
        customer_po="PO-REHEARSAL-002",
        order_date=date(2026, 6, 21),
        delivery_date=date(2026, 6, 23),
        created_by=None,
        item_specs=[
            {
                "product_id": product_a,
                "quantity": 5,
                "unit_price": Decimal("4.10"),
                "snapshot_product_code": "PX-004",
                "snapshot_product_name": "演练纸箱D",
                "snapshot_spec": "540×340×240mm",
                "snapshot_material": "K=K",
            }
        ],
    )
    assert order_again.order_number.startswith("TM20260621")
    assert int(order_again.order_number[-3:]) == int(order.order_number[-3:]) + 1

    conn.execute(
        """
        INSERT INTO sales_order_items (
            order_id, product_id, quantity, delivered_quantity, is_force_closed,
            unit_price, subtotal, material_status, snapshot_product_name,
            snapshot_product_code, snapshot_spec, snapshot_material,
            inventory_deducted_qty, requisition_status, special_process,
            item_order_number, item_sequence
        ) VALUES (?, ?, 1, 0, 0, 1, 1, 'pending', '补充明细', 'PX-005', 'x', 'x', 0, '未报料', '无', ?, ?)
        """,
        (order.order_id, product_a, f"{order.order_number}-004", 4),
    )
    sequence_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(order_item_number_sequences)")
    }
    sequence_column = (
        "last_item_sequence" if "last_item_sequence" in sequence_columns else "last_item_seq"
    )
    conn.execute(
        f"""
        INSERT INTO order_item_number_sequences (order_id, {sequence_column})
        VALUES (?, 4)
        ON CONFLICT(order_id) DO UPDATE SET {sequence_column} = excluded.{sequence_column}
        """,
        (order.order_id,),
    )
    conn.commit()
    final_rows = conn.execute(
        "SELECT item_order_number FROM sales_order_items WHERE order_id = ? ORDER BY item_sequence",
        (order.order_id,),
    ).fetchall()
    assert [row["item_order_number"] for row in final_rows] == [
        f"{order.order_number}-001",
        f"{order.order_number}-003",
        f"{order.order_number}-004",
    ]


def test_grouping_rules_and_search_rules_hold_on_rehearsal_copy(tmp_path: Path) -> None:
    conn = _copy_main_db(tmp_path)
    ensure_rehearsal_schema(conn)
    customer_rows = conn.execute(
        "SELECT id, name FROM customers ORDER BY id LIMIT 2"
    ).fetchall()
    assert len(customer_rows) == 2
    customer_a, customer_b = int(customer_rows[0]["id"]), int(customer_rows[1]["id"])
    product_rows = conn.execute(
        "SELECT id, customer_id, product_code, product_name FROM products WHERE deleted_at IS NULL ORDER BY customer_id, id LIMIT 4"
    ).fetchall()
    product_map: dict[int, list[sqlite3.Row]] = {}
    for row in product_rows:
        product_map.setdefault(int(row["customer_id"]), []).append(row)
    assert customer_a in product_map and customer_b in product_map

    order_a1 = create_order_with_items(
        conn,
        customer_id=customer_a,
        customer_po="PO-SAME-001",
        order_date=date(2026, 6, 21),
        delivery_date=None,
        created_by=None,
        item_specs=[
            {
                "product_id": int(product_map[customer_a][0]["id"]),
                "quantity": 8,
                "unit_price": Decimal("2.50"),
                "snapshot_product_code": "A-001",
                "snapshot_product_name": "客户A产品1",
                "snapshot_spec": "400×300×200mm",
                "snapshot_material": "K=A",
            }
        ],
    )
    order_a2 = create_order_with_items(
        conn,
        customer_id=customer_a,
        customer_po="PO-SAME-001",
        order_date=date(2026, 6, 21),
        delivery_date=None,
        created_by=None,
        item_specs=[
            {
                "product_id": int(product_map[customer_a][0]["id"]),
                "quantity": 9,
                "unit_price": Decimal("2.60"),
                "snapshot_product_code": "A-002",
                "snapshot_product_name": "客户A产品2",
                "snapshot_spec": "410×310×210mm",
                "snapshot_material": "A=B",
            }
        ],
    )
    order_b = create_order_with_items(
        conn,
        customer_id=customer_b,
        customer_po="PO-SAME-001",
        order_date=date(2026, 6, 21),
        delivery_date=None,
        created_by=None,
        item_specs=[
            {
                "product_id": int(product_map[customer_b][0]["id"]),
                "quantity": 6,
                "unit_price": Decimal("3.10"),
                "snapshot_product_code": "B-001",
                "snapshot_product_name": "客户B产品1",
                "snapshot_spec": "420×320×220mm",
                "snapshot_material": "B=C",
            }
        ],
    )
    empty_po = create_order_with_items(
        conn,
        customer_id=customer_a,
        customer_po=None,
        order_date=date(2026, 6, 21),
        delivery_date=None,
        created_by=None,
        item_specs=[
            {
                "product_id": int(product_map[customer_a][0]["id"]),
                "quantity": 1,
                "unit_price": Decimal("9.99"),
                "snapshot_product_code": "A-EMPTY",
                "snapshot_product_name": "空PO产品",
                "snapshot_spec": "430×330×230mm",
                "snapshot_material": "K=K",
            }
        ],
    )

    rows = conn.execute(
        "SELECT id, order_number, customer_id, customer_po FROM sales_orders WHERE order_number LIKE 'TM20260621%' ORDER BY id"
    ).fetchall()
    grouped = group_orders(rows)
    assert grouped[f"group:{customer_a}::PO-SAME-001"] == [order_a1.order_id, order_a2.order_id]
    assert grouped[f"group:{customer_b}::PO-SAME-001"] == [order_b.order_id]
    assert grouped[f"single:{empty_po.order_number}"] == [empty_po.order_id]

    by_customer_po = search_orders(conn, keyword="PO-SAME-001")
    search_grouped = group_orders(by_customer_po)
    assert len(search_grouped[f"group:{customer_a}::PO-SAME-001"]) == 2
    assert len(search_grouped[f"group:{customer_b}::PO-SAME-001"]) == 1
    assert search_orders(conn, keyword=order_a1.order_number)[0]["order_number"] == order_a1.order_number
    item_number = conn.execute(
        "SELECT item_order_number FROM sales_order_items WHERE order_id = ? ORDER BY id LIMIT 1",
        (order_a2.order_id,),
    ).fetchone()[0]
    assert search_orders(conn, keyword=item_number)[0]["order_number"] == order_a2.order_number
    assert search_orders(conn, customer_name=str(customer_rows[0]["name"]))
    assert search_orders(conn, order_date="2026-06-21")
    assert search_orders(conn, product_keyword="A-002")
    assert integrity_snapshot(conn)["integrity_check"] == "ok"
    assert integrity_snapshot(conn)["foreign_key_check"] == []
