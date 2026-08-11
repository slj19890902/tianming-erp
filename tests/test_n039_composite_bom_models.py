from __future__ import annotations

from pathlib import Path

from app.models import Base
from app.models.product_bom import (
    BomComponentDirectDeliveryAllocation,
    RequisitionItemBomSource,
    SalesOrderItemBomDemandAdjustment,
    SalesOrderItemBomComponent,
)
from app.models.production import ProductionCompletion, ProductionTask
from app.models.warehouse_inventory import InventoryReservation, OrderItemSemiRequirement


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    PROJECT_ROOT / "alembic" / "versions" / "cd60v8x9z49_n039_composite_bom_phase_b.py"
)


def _constraint_names(table_name: str) -> set[str]:
    return {
        constraint.name
        for constraint in Base.metadata.tables[table_name].constraints
        if constraint.name
    }


def _index_names(table_name: str) -> set[str]:
    return {index.name for index in Base.metadata.tables[table_name].indexes if index.name}


def test_n039_models_register_append_only_demand_and_direct_kit_facts() -> None:
    assert {
        "sales_order_item_bom_demand_adjustments",
        "bom_component_direct_delivery_allocations",
    } <= set(Base.metadata.tables)
    assert {
        "parent_product_version",
        "component_product_version",
        "snapshot_schema_version",
    } <= set(SalesOrderItemBomComponent.__table__.c.keys())
    assert "calculation_rule_version" in RequisitionItemBomSource.__table__.c
    assert {
        "sales_order_item_bom_component_id",
        "event_type",
        "delta_order_set_quantity",
        "delta_required_piece_quantity",
        "reason",
        "actor_id",
        "idempotency_key",
        "created_at",
    } <= set(SalesOrderItemBomDemandAdjustment.__table__.c.keys())
    assert {
        "delivery_item_id",
        "production_completion_id",
        "sales_order_item_bom_component_id",
        "consumed_quantity",
        "reversed_quantity",
        "status",
        "created_by",
        "reversed_by",
        "created_at",
        "reversed_at",
    } <= set(BomComponentDirectDeliveryAllocation.__table__.c.keys())


def test_n039_constraints_and_indexes_protect_component_boundaries() -> None:
    for table_name in (
        "product_bom_components",
        "sales_order_item_bom_components",
        "requisition_item_bom_sources",
    ):
        checks = [
            str(constraint.sqltext)
            for constraint in Base.metadata.tables[table_name].constraints
            if (getattr(constraint, "name", "") or "").endswith("quantity_per_set")
        ]
        assert checks and all("round(quantity_per_set, 0)" in check for check in checks)

    assert {
        "ck_sales_order_item_bom_demand_adjustments_event_type",
        "ck_sales_order_item_bom_demand_adjustments_not_noop",
        "ck_sales_order_item_bom_demand_adjustments_reason",
        "uq_sales_order_item_bom_demand_adjustments_idempotency",
    } <= _constraint_names("sales_order_item_bom_demand_adjustments")
    assert {
        "ck_bom_component_direct_delivery_allocations_consumed_quantity",
        "ck_bom_component_direct_delivery_allocations_reversed_quantity",
        "ck_bom_component_direct_delivery_allocations_status",
        "uq_bom_component_direct_delivery_allocations_delivery_completion",
    } <= _constraint_names("bom_component_direct_delivery_allocations")
    assert {
        "ix_bom_component_direct_delivery_allocations_snapshot_status",
        "ix_bom_component_direct_delivery_allocations_completion_status",
    } <= _index_names("bom_component_direct_delivery_allocations")

    assert "uq_production_tasks_order_item" not in _constraint_names("production_tasks")
    assert {
        "uq_production_tasks_regular_order_item",
        "uq_production_tasks_bom_component",
    } <= _index_names("production_tasks")
    assert "uq_production_completions_order_item" not in _constraint_names(
        "production_completions"
    )
    assert (
        "uq_production_completions_task_primary_active"
        in _index_names("production_completions")
    )
    for table in (InventoryReservation, OrderItemSemiRequirement, ProductionTask):
        assert "sales_order_item_bom_component_id" in table.__table__.c
    assert "task_id" in ProductionCompletion.__table__.c
    adjustment_fk = next(
        foreign_key
        for foreign_key in SalesOrderItemBomDemandAdjustment.__table__.foreign_keys
        if foreign_key.column.table.name == "sales_order_item_bom_components"
    )
    assert adjustment_fk.ondelete == "CASCADE"


def test_n039_migration_is_linear_immutable_and_fail_closed() -> None:
    source = MIGRATION_PATH.read_bytes().decode("utf-8", errors="strict")
    compile(source, str(MIGRATION_PATH), "exec")

    assert 'revision: str = "cd60v8x9z49"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "cc59v8x9z48"' in source
    assert "sales_order_item_bom_demand_adjustments are immutable" in source
    assert "BEFORE UPDATE ON {ADJUSTMENT_TABLE}" in source
    assert "BEFORE UPDATE OR DELETE ON {ADJUSTMENT_TABLE}" not in source
    assert "calculation rule is immutable" in source
    assert "quantity_per_set = round(quantity_per_set, 0)" in source
    assert "uq_production_tasks_regular_order_item" in source
    assert "uq_production_tasks_bom_component" in source
    assert "uq_production_completions_task" in source
    assert "bom_component_direct_delivery_allocations" in source
    assert "(SELECT COUNT(*) FROM {DIRECT_DELIVERY_ALLOCATION_TABLE})" in source
    assert "(SELECT COUNT(*) FROM order_item_semi_requirements" in source
    assert "N039 复合 BOM Phase B 事实已产生" in source
