from __future__ import annotations

import importlib.util
from pathlib import Path


def test_n041_migration_is_linear_and_defines_contract_schema() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "df62v8x9z51_n041_customer_contract_workflow.py"
    )
    spec = importlib.util.spec_from_file_location("n041_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "df62v8x9z51"
    assert module.down_revision == "ce61v8x9z50"
    source = path.read_text(encoding="utf-8")
    for table_name in (
        "contract_daily_sequences",
        "customer_contracts",
        "customer_contract_items",
    ):
        assert table_name in source
    assert "source_contract_id" in source
    assert "ck_customer_contracts_status" in source
    assert "uq_sales_orders_source_contract_id" in source


def test_contract_metadata_has_unique_conversion_boundary() -> None:
    from app.models import Base

    contracts = Base.metadata.tables["customer_contracts"]
    orders = Base.metadata.tables["sales_orders"]
    assert "converted_order_id" in contracts.c
    assert "conversion_idempotency_key" in contracts.c
    assert "source_contract_id" in orders.c
    assert any(
        set(constraint.columns.keys()) == {"source_contract_id"}
        for constraint in orders.constraints
    )
