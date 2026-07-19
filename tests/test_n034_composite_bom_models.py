from __future__ import annotations

from pathlib import Path

from app.models import Base
from app.models.product import Product
from app.models.product_bom import (
    ProductBomComponent,
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.services.master_data_versioning import _PRODUCT_FIELDS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    PROJECT_ROOT / "alembic" / "versions" / "bd57v8x9z47_n034_composite_bom_phase_a.py"
)


def _constraint_names(table_name: str) -> set[str]:
    return {
        constraint.name
        for constraint in Base.metadata.tables[table_name].constraints
        if constraint.name
    }


def test_n034_product_flags_and_bom_models_are_registered() -> None:
    assert {"is_composite", "is_internal_component"} <= set(
        Product.__table__.c.keys()
    )
    assert {
        "product_bom_components",
        "sales_order_item_bom_components",
        "requisition_item_bom_sources",
    } <= set(Base.metadata.tables)

    assert ProductBomComponent.__table__.c.quantity_per_set.type.precision == 14
    assert SalesOrderItemBomComponent.__table__.c.quantity_per_set.type.scale == 4
    assert RequisitionItemBomSource.__table__.c.required_piece_quantity.type.scale == 4
    assert {
        "order_set_quantity",
        "required_piece_quantity",
        "snapshot_component_supplier_name",
        "snapshot_component_layer_count",
        "snapshot_component_flute_type",
    } <= set(SalesOrderItemBomComponent.__table__.c.keys())
    assert {
        "order_set_quantity",
        "quantity_per_set",
        "required_piece_quantity",
        "mold_max_yield_per_sheet",
        "actual_yield_per_sheet",
        "spare_sheet_quantity",
        "calculated_purchase_quantity",
        "direction_note",
    } <= set(RequisitionItemBomSource.__table__.c.keys())
    display_mode_checks = {
        str(constraint.sqltext)
        for constraint in ProductBomComponent.__table__.constraints
        if hasattr(constraint, "sqltext") and "display_mode" in str(constraint.sqltext)
    }
    assert any(
        "internal_only" in check
        and "show_on_delivery" in check
        and "show_on_all_docs" in check
        for check in display_mode_checks
    )
    assert "is_composite" in _PRODUCT_FIELDS
    assert "is_internal_component" not in _PRODUCT_FIELDS

    source_fk = next(
        iter(SalesOrderItemBomComponent.__table__.c.product_bom_component_id.foreign_keys)
    )
    assert source_fk.ondelete == "SET NULL"


def test_n034_model_constraints_and_indexes_cover_bom_boundaries() -> None:
    assert {
        "ck_product_bom_components_distinct_products",
        "ck_product_bom_components_quantity_per_set",
        "ck_product_bom_components_display_mode",
        "ck_product_bom_components_die_cut_mold_required",
        "uq_product_bom_components_parent_component",
        "uq_product_bom_components_parent_display_order",
    } <= _constraint_names("product_bom_components")
    assert {
        "ck_sales_order_item_bom_components_quantity_per_set",
        "ck_sales_order_item_bom_components_order_set_quantity",
        "ck_sales_order_item_bom_components_required_piece_formula",
        "ck_sales_order_item_bom_components_display_mode",
        "uq_sales_order_item_bom_components_item_display_order",
    } <= _constraint_names("sales_order_item_bom_components")
    assert {
        "ck_requisition_item_bom_sources_required_piece_quantity",
        "ck_requisition_item_bom_sources_required_piece_formula",
        "ck_requisition_item_bom_sources_actual_yield_within_maximum",
        "ck_requisition_item_bom_sources_purchase_covers_spares",
        "uq_requisition_item_bom_sources_item_snapshot",
    } <= _constraint_names("requisition_item_bom_sources")
    assert {
        "ix_product_bom_components_parent_display_order",
        "ix_product_bom_components_component_product_id",
        "ix_sales_order_item_bom_components_item_display_order",
        "ix_requisition_item_bom_sources_snapshot_id",
    } <= {
        index.name
        for table in (
            ProductBomComponent.__table__,
            SalesOrderItemBomComponent.__table__,
            RequisitionItemBomSource.__table__,
        )
        for index in table.indexes
    }
    for table in (
        ProductBomComponent.__table__,
        SalesOrderItemBomComponent.__table__,
    ):
        non_die_cut_checks = [
            str(constraint.sqltext)
            for constraint in table.constraints
            if getattr(constraint, "name", "")
            and "non_die_cut_mold_fields" in constraint.name
        ]
        assert non_die_cut_checks
        assert all("spare_sheet_quantity = 0" not in check for check in non_die_cut_checks)


def test_n034_migration_is_linear_fail_closed_and_snapshot_immutable() -> None:
    source = MIGRATION_PATH.read_bytes().decode("utf-8", errors="strict")
    compile(source, str(MIGRATION_PATH), "exec")

    assert 'revision: str = "bd57v8x9z47"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "bc56v8x9z46"' in source
    assert "sales_order_item_bom_components snapshots are immutable" in source
    assert 'ondelete="SET NULL"' in source
    assert "snapshot source may only be unlinked" in source
    assert "SELECT COUNT(*) FROM {table_name}" in source
    assert "is_composite IS TRUE OR is_internal_component IS TRUE" in source
    for column_name in (
        "order_set_quantity",
        "required_piece_quantity",
        "snapshot_component_supplier_name",
        "snapshot_component_layer_count",
        "snapshot_component_flute_type",
        "quantity_per_set",
        "mold_max_yield_per_sheet",
        "actual_yield_per_sheet",
        "spare_sheet_quantity",
        "calculated_purchase_quantity",
        "direction_note",
    ):
        assert f'"{column_name}"' in source
    assert "spare_sheet_quantity = 0" not in source
    assert "child sales-order items" in source
    for forbidden_write in (
        "op.bulk_insert",
        "INSERT INTO sales_order_items",
        "INSERT INTO material_requisition",
        "INSERT INTO production",
        "INSERT INTO inventory",
        "INSERT INTO deliveries",
        "INSERT INTO accounting",
    ):
        assert forbidden_write not in source
