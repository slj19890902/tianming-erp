from __future__ import annotations

from pathlib import Path


def test_stock_replenishment_models_expose_required_fields() -> None:
    from app.models.stock_replenishment import (
        InventoryStockPolicy,
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )

    policy_columns = InventoryStockPolicy.__table__.columns
    assert InventoryStockPolicy.__tablename__ == "inventory_stock_policies"
    assert "warning_quantity" in policy_columns
    assert "target_quantity" in policy_columns
    assert "default_location_id" in policy_columns
    assert "target_inventory_type" in policy_columns

    assert StockReplenishmentOrder.__tablename__ == "stock_replenishment_orders"
    assert StockReplenishmentOrderItem.__tablename__ == "stock_replenishment_order_items"
    item_columns = StockReplenishmentOrderItem.__table__.columns
    for name in (
        "historical_workbook",
        "historical_sheet",
        "historical_row",
        "location_id",
        "inventory_lot_id",
        "stocked_quantity",
        "material_id",
    ):
        assert name in item_columns


def test_stock_replenishment_migration_is_additive() -> None:
    content = Path(
        "alembic/versions/ad31v7w8x9z21_stock_replenishment_foundation.py"
    ).read_text(encoding="utf-8").lower()

    assert 'down_revision = "ac30v7w8x9y20"' in content
    assert "create_table" in content
    assert "drop_column" not in content
    assert "alter_column" not in content
    assert "sales_order_items" not in content
    assert "sales_deliveries" not in content


def test_mold_and_material_link_migration_is_additive() -> None:
    content = Path(
        "alembic/versions/ae32v7w8x9a22_mold_and_material_links.py"
    ).read_text(encoding="utf-8").lower()
    assert 'down_revision = "ad31v7w8x9z21"' in content
    assert '"mold_tools"' in content
    assert '"mold_tool_id"' in content
    assert '"material_id"' in content
    assert "drop_table" not in content.split("def downgrade", 1)[0]


def test_historical_purchase_entries_preserve_each_source_row() -> None:
    from app.models.historical_purchase import HistoricalPurchaseEntry

    columns = HistoricalPurchaseEntry.__table__.columns
    assert HistoricalPurchaseEntry.__tablename__ == "historical_purchase_entries"
    for name in (
        "source_workbook",
        "source_sheet",
        "source_row",
        "source_fingerprint",
        "normalized_search_text",
        "record_date",
        "product_id",
        "customer_id",
        "report_length_mm",
        "report_width_mm",
    ):
        assert name in columns

    content = Path(
        "alembic/versions/af33v7w8x9b23_historical_purchase_entries.py"
    ).read_text(encoding="utf-8").lower()
    assert 'down_revision = "ae32v7w8x9a22"' in content
    assert '"historical_purchase_entries"' in content
    assert "drop_table" not in content.split("def downgrade", 1)[0]
