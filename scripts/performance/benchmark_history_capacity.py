from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter


def _timed_ms(connection: sqlite3.Connection, sql: str, params: tuple) -> float:
    samples: list[float] = []
    for _ in range(12):
        started = perf_counter()
        connection.execute(sql, params).fetchall()
        samples.append((perf_counter() - started) * 1000)
    return round(statistics.median(samples), 4)


def _plan(connection: sqlite3.Connection, sql: str, params: tuple) -> list[str]:
    return [
        str(row[3])
        for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", params)
    ]


def _benchmark_size(root: Path, size: int) -> dict:
    database_path = root / f"history-capacity-{size}.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE sales_orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            product_code TEXT NOT NULL,
            customer_material_code TEXT NOT NULL
        );
        CREATE TABLE sales_order_items (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            snapshot_product_code TEXT
        );
        CREATE TABLE production_completions (
            id INTEGER PRIMARY KEY,
            order_item_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            completed_at TEXT NOT NULL
        );
        CREATE INDEX ix_sales_orders_created_at_id
            ON sales_orders(created_at, id);
        CREATE INDEX ix_sales_orders_updated_at_id
            ON sales_orders(updated_at, id);
        CREATE INDEX ix_sales_order_items_snapshot_product_code
            ON sales_order_items(snapshot_product_code);
        CREATE INDEX ix_sales_order_items_order_id
            ON sales_order_items(order_id);
        CREATE INDEX ix_sales_order_items_product_id
            ON sales_order_items(product_id);
        CREATE INDEX ix_products_product_code ON products(product_code);
        CREATE INDEX ix_products_customer_material_code
            ON products(customer_material_code);
        CREATE INDEX ix_production_completions_completed_at_id
            ON production_completions(completed_at, id);
        CREATE INDEX ix_production_completions_status_completed_at_id
            ON production_completions(status, completed_at, id);
        CREATE INDEX ix_production_completions_order_item_id
            ON production_completions(order_item_id);
        """
    )
    now = datetime(2026, 8, 29, 12, 0, 0)
    target_position = max(size - 17, 1)
    with connection:
        connection.executemany(
            "INSERT INTO products(id, product_code, customer_material_code) "
            "VALUES (?, ?, ?)",
            (
                (
                    row_id,
                    "TARGET-ERP-001" if row_id == target_position else f"SKU-{row_id:08d}",
                    f"MAT-{row_id:08d}",
                )
                for row_id in range(1, size + 1)
            ),
        )
        connection.executemany(
            "INSERT INTO sales_orders(id, customer_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (
                    row_id,
                    (row_id % 200) + 1,
                    "completed" if row_id <= size - 100 else "pending_delivery",
                    (now - timedelta(minutes=size - row_id)).isoformat(" "),
                    None,
                )
                for row_id in range(1, size + 1)
            ),
        )
        connection.executemany(
            "INSERT INTO sales_order_items(id, order_id, product_id, snapshot_product_code) "
            "VALUES (?, ?, ?, ?)",
            (
                (
                    row_id,
                    row_id,
                    row_id,
                    "TARGET-ERP-001" if row_id == target_position else f"SKU-{row_id:08d}",
                )
                for row_id in range(1, size + 1)
            ),
        )
        connection.executemany(
            "INSERT INTO production_completions(id, order_item_id, status, completed_at) "
            "VALUES (?, ?, ?, ?)",
            (
                (
                    row_id,
                    row_id,
                    "posted" if row_id % 20 else "reversed",
                    (now - timedelta(seconds=size - row_id)).isoformat(" "),
                )
                for row_id in range(1, size + 1)
            ),
        )

    recent_cutoff = (now - timedelta(days=10)).isoformat(" ")
    recent_sql = (
        "SELECT id FROM sales_orders "
        "WHERE created_at >= ? ORDER BY created_at DESC, id DESC LIMIT 50"
    )
    exact_sql = (
        "SELECT id FROM sales_orders WHERE id IN ("
        "SELECT order_id FROM sales_order_items WHERE snapshot_product_code = ? "
        "UNION SELECT order_id FROM sales_order_items WHERE product_id IN ("
        "SELECT id FROM products WHERE product_code = ?) "
        "UNION SELECT order_id FROM sales_order_items WHERE product_id IN ("
        "SELECT id FROM products WHERE customer_material_code = ?)"
        ") ORDER BY created_at DESC, id DESC LIMIT 50"
    )
    cursor_at = (now - timedelta(seconds=size // 2)).isoformat(" ")
    cursor_id = max(size // 2, 1)
    cursor_sql = (
        "SELECT id FROM production_completions "
        "WHERE (completed_at, id) < (?, ?) "
        "ORDER BY completed_at DESC, id DESC LIMIT 51"
    )
    completion_exact_sql = (
        "SELECT id FROM production_completions WHERE order_item_id IN ("
        "SELECT id FROM sales_order_items WHERE snapshot_product_code = ? "
        "UNION SELECT id FROM sales_order_items WHERE product_id IN ("
        "SELECT id FROM products WHERE product_code = ?) "
        "UNION SELECT id FROM sales_order_items WHERE product_id IN ("
        "SELECT id FROM products WHERE customer_material_code = ?)"
        ") ORDER BY completed_at DESC, id DESC LIMIT 50"
    )
    offset_sql = (
        "SELECT id FROM production_completions "
        "ORDER BY completed_at DESC, id DESC LIMIT 51 OFFSET ?"
    )
    exact_params = ("TARGET-ERP-001",) * 3
    result = {
        "rows": size,
        "recent_order_ms": _timed_ms(connection, recent_sql, (recent_cutoff,)),
        "exact_product_code_ms": _timed_ms(connection, exact_sql, exact_params),
        "completion_cursor_midpoint_ms": _timed_ms(
            connection,
            cursor_sql,
            (cursor_at, cursor_id),
        ),
        "completion_exact_product_code_ms": _timed_ms(
            connection,
            completion_exact_sql,
            exact_params,
        ),
        "completion_offset_midpoint_ms": _timed_ms(
            connection,
            offset_sql,
            (size // 2,),
        ),
        "plans": {
            "recent_order": _plan(connection, recent_sql, (recent_cutoff,)),
            "exact_product_code": _plan(connection, exact_sql, exact_params),
            "completion_cursor": _plan(
                connection,
                cursor_sql,
                (cursor_at, cursor_id),
            ),
            "completion_exact_product_code": _plan(
                connection,
                completion_exact_sql,
                exact_params,
            ),
        },
    }
    connection.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark P1-125 indexed history lookup structures on generated data."
    )
    parser.add_argument(
        "--sizes",
        type=int,
        nargs="+",
        default=[10_000, 50_000, 100_000],
    )
    args = parser.parse_args()
    if any(size <= 0 for size in args.sizes):
        parser.error("all sizes must be positive")
    with tempfile.TemporaryDirectory(prefix="p1-125-history-capacity-") as temp_dir:
        results = [_benchmark_size(Path(temp_dir), size) for size in args.sizes]
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
