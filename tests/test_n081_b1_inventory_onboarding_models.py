from __future__ import annotations

from sqlalchemy import CheckConstraint


def test_inventory_onboarding_models_keep_source_and_resolution_separate() -> None:
    from app.models.inventory_onboarding import (
        InventoryOnboardingBatch,
        InventoryOnboardingLine,
    )

    assert InventoryOnboardingBatch.__tablename__ == "inventory_onboarding_batches"
    assert InventoryOnboardingLine.__tablename__ == "inventory_onboarding_lines"

    batch_fields = {
        "batch_number",
        "status",
        "version",
        "source_file_reference",
        "source_file_sha256",
        "source_original_filename",
        "source_content_type",
        "source_size",
        "source_format",
        "source_encoding",
        "resolved_floor",
        "resolved_area_code",
        "dry_run_fingerprint",
        "dry_run_summary_json",
        "dry_run_by",
        "dry_run_at",
        "submit_idempotency_key",
        "submitted_by",
        "submitted_at",
        "created_by",
        "created_at",
        "updated_by",
        "updated_at",
    }
    assert batch_fields <= set(InventoryOnboardingBatch.__table__.columns.keys())

    source_fields = {
        "batch_id",
        "source_sheet_name",
        "source_row_number",
        "source_row_hash",
        "raw_row_text",
        "original_values_json",
    }
    resolved_fields = {
        "stocktake_date",
        "stocktaker_name",
        "inventory_type",
        "ownership_type",
        "location_id",
        "location_code_snapshot",
        "floor_snapshot",
        "warehouse_name_snapshot",
        "area_code_snapshot",
        "pallet_code",
        "customer_id",
        "customer_code_snapshot",
        "customer_name_snapshot",
        "product_id",
        "inventory_code_snapshot",
        "product_name_snapshot",
        "material_id",
        "material_code_snapshot",
        "quantity",
        "unit",
        "source_type",
        "stock_date",
        "stock_date_accuracy",
        "stock_date_original_text",
        "supplier_name",
        "layer_count",
        "flute_type",
        "board_length_mm",
        "board_width_mm",
        "sheet_type",
        "component_type",
        "pieces_per_box",
        "stock_yield_per_sheet",
        "crease_type",
        "crease_left_mm",
        "crease_middle_mm",
        "crease_right_mm",
        "cutting_note",
        "remarks",
        "action_decision",
        "existing_lot_id",
        "existing_pallet_id",
        "match_status",
        "error_codes_json",
        "warning_codes_json",
        "match_evidence_json",
        "version",
    }
    line_columns = set(InventoryOnboardingLine.__table__.columns.keys())
    assert source_fields <= line_columns
    assert resolved_fields <= line_columns


def test_inventory_onboarding_constraints_and_foreign_keys_are_fail_closed() -> None:
    from app.models.inventory_onboarding import (
        InventoryOnboardingBatch,
        InventoryOnboardingLine,
    )

    batch_checks = {
        constraint.name
        for constraint in InventoryOnboardingBatch.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert {
        "ck_inventory_onboarding_batches_status",
        "ck_inventory_onboarding_batches_version",
        "ck_inventory_onboarding_batches_source_sha256",
        "ck_inventory_onboarding_batches_source_encoding",
        "ck_inventory_onboarding_batches_dry_run_state",
        "ck_inventory_onboarding_batches_submit_state",
    } <= batch_checks

    line_checks = {
        constraint.name
        for constraint in InventoryOnboardingLine.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert {
        "ck_inventory_onboarding_lines_source_type",
        "ck_inventory_onboarding_lines_date_accuracy",
        "ck_inventory_onboarding_lines_inventory_type",
        "ck_inventory_onboarding_lines_type_unit",
        "ck_inventory_onboarding_lines_action",
        "ck_inventory_onboarding_lines_match_status",
        "ck_inventory_onboarding_lines_route_target",
    } <= line_checks

    targets = {
        foreign_key.target_fullname
        for foreign_key in InventoryOnboardingLine.__table__.foreign_keys
    }
    assert {
        "inventory_onboarding_batches.id",
        "warehouse_locations.id",
        "customers.id",
        "products.id",
        "materials.id",
        "inventory_lots.id",
        "inventory_pallets.id",
        "users.id",
    } <= targets


def test_b1_models_do_not_define_a_second_inventory_balance() -> None:
    from app.models.inventory_onboarding import InventoryOnboardingLine

    columns = set(InventoryOnboardingLine.__table__.columns.keys())
    assert {
        "quantity_available",
        "quantity_reserved",
        "quantity_consumed",
        "quantity_damaged",
        "quantity_scrapped",
    }.isdisjoint(columns)
    assert InventoryOnboardingLine.__table__.c.source_type.server_default is not None
