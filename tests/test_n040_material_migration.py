from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "ce61v8x9z50_n040_customer_material_candidates.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("n040_migration", MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_n040_migration_is_linear_and_declares_immutable_history_guards() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "ce61v8x9z50"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "cd60v8x9z49"' in source
    assert "customer_material_candidates" in source
    assert "customer_material_selection_history" in source
    assert "customer material selection history is immutable" in source
    assert "ix_customer_material_selection_history_product" in source
    assert "ix_supplier_requisition_order_items_product_created" in source
    assert "material_code_snapshot" in source
    assert "禁止破坏性降级" in source


def test_n040_models_expose_original_candidate_and_history_facts() -> None:
    from app.models.customer_material import (
        CustomerMaterialCandidate,
        CustomerMaterialSelectionHistory,
    )
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    assert "snapshot_original_material_code" in OrderItem.__table__.columns
    assert {
        "customer_id",
        "normalized_original_material_code",
        "actual_material_id",
        "actual_material_code_snapshot",
        "manual_priority",
        "is_active",
    } <= set(CustomerMaterialCandidate.__table__.columns.keys())
    assert {
        "customer_id",
        "order_item_id",
        "candidate_id",
        "original_material_code_snapshot",
        "original_material_confidence",
        "selected_material_code_snapshot",
        "selected_supplier_name_snapshot",
        "source_type",
        "selection_reason",
        "sync_product",
        "selected_at",
    } <= set(CustomerMaterialSelectionHistory.__table__.columns.keys())
    assert {
        "product_id",
        "material_id",
        "material_code_snapshot",
        "supplier_name_snapshot",
        "layer_count_snapshot",
        "flute_type_snapshot",
    } <= set(SupplierRequisitionOrderItem.__table__.columns.keys())
    history_indexes = {
        index.name for index in CustomerMaterialSelectionHistory.__table__.indexes
    }
    supplier_item_indexes = {
        index.name for index in SupplierRequisitionOrderItem.__table__.indexes
    }
    assert "ix_customer_material_selection_history_product" in history_indexes
    assert "ix_supplier_requisition_order_items_product_created" in supplier_item_indexes


def test_n040_supplier_order_creation_paths_freeze_material_snapshots() -> None:
    source = (PROJECT_ROOT / "app" / "api" / "requisition.py").read_text(
        encoding="utf-8"
    )
    # One helper definition plus all three SupplierRequisitionOrderItem creation paths.
    assert source.count("_supplier_item_snapshot_values(") == 4


def test_n040_downgrade_fails_closed_after_business_facts_exist() -> None:
    migration = _load_migration()

    class ScalarResult:
        def scalar_one(self) -> int:
            return 1

    class Connection:
        def execute(self, _statement):  # noqa: ANN001
            return ScalarResult()

    class Op:
        @staticmethod
        def get_bind():
            return Connection()

    original_op = migration.op
    migration.op = Op()
    try:
        with pytest.raises(RuntimeError, match="禁止破坏性降级"):
            migration._assert_safe_downgrade()
    finally:
        migration.op = original_op
