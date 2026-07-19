from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only N039 SQLite inspection.")
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    database = args.database.resolve()
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        phase_b_tables = connection.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type = 'table' AND name IN "
            "('sales_order_item_bom_demand_adjustments', "
            "'bom_component_direct_delivery_allocations')"
        ).fetchone()[0]
        adjustment_count = (
            connection.execute(
                "SELECT count(*) FROM sales_order_item_bom_demand_adjustments"
            ).fetchone()[0]
            if phase_b_tables == 2
            else 0
        )
        direct_allocation_count = (
            connection.execute(
                "SELECT count(*) FROM bom_component_direct_delivery_allocations"
            ).fetchone()[0]
            if phase_b_tables == 2
            else 0
        )
        print(f"database={database}")
        print(f"alembic={version}")
        print(f"integrity={integrity}")
        print(f"foreign_key_errors={foreign_key_errors}")
        print(f"phase_b_tables={phase_b_tables}")
        print(f"demand_adjustments={adjustment_count}")
        print(f"direct_delivery_allocations={direct_allocation_count}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
