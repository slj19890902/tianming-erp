"""add composite physical purchase groups and group receipt lineage

Revision ID: jq75v8x9z64
Revises: jp74v8x9z63
Create Date: 2026-09-03

The migration is structural only.  Existing requisition, receipt, inventory,
and cost rows are deliberately not backfilled into the new authoritative
group lineage.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "jq75v8x9z64"
down_revision = "jp74v8x9z63"
branch_labels = None
depends_on = None


GROUP_TABLE = "composite_physical_purchase_groups"
SOURCE_TABLE = "composite_physical_purchase_group_sources"
RECEIPT_TABLE = "composite_physical_group_receipts"
ALLOCATION_TABLE = "composite_physical_group_receipt_source_allocations"
REVERSAL_TABLE = "composite_physical_group_receipt_reversals"
INCOMING_TABLE = "incoming_receipt_items"
COST_TABLE = "finance_delivery_material_cost_facts"


def _drop_sqlite_triggers_referencing(table_name: str) -> list[str]:
    """Temporarily remove triggers that SQLite batch rebuilds would lose."""

    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return []
    rows = connection.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND sql IS NOT NULL "
            "AND (tbl_name = :table_name "
            "OR instr(lower(sql), lower(:table_name)) > 0) "
            "ORDER BY name"
        ),
        {"table_name": table_name},
    ).all()
    statements: list[str] = []
    for name, sql in rows:
        statements.append(str(sql))
        quoted_name = str(name).replace('"', '""')
        op.execute(f'DROP TRIGGER IF EXISTS "{quoted_name}"')
    return statements


def _restore_sqlite_triggers(statements: list[str]) -> None:
    for statement in statements:
        op.execute(statement)


def _create_group_table() -> None:
    op.create_table(
        'composite_physical_purchase_groups',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column('group_key', sa.String(length=160), nullable=False),
        sa.Column('requisition_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('component_product_id', sa.Integer(), nullable=False),
        sa.Column('supplier_requisition_order_item_id', sa.Integer(), nullable=True),
        sa.Column('material_id', sa.Integer(), nullable=False),
        sa.Column('material_version_snapshot', sa.Integer(), nullable=False),
        sa.Column('material_code_snapshot', sa.String(length=200), nullable=False),
        sa.Column('layer_count_snapshot', sa.Integer(), nullable=False),
        sa.Column('flute_type_snapshot', sa.String(length=50), nullable=False),
        sa.Column('report_length_mm', sa.Integer(), nullable=False),
        sa.Column('report_width_mm', sa.Integer(), nullable=False),
        sa.Column('crease_type_snapshot', sa.String(length=20), nullable=True),
        sa.Column('crease_left_mm', sa.Integer(), nullable=True),
        sa.Column('crease_middle_mm', sa.Integer(), nullable=True),
        sa.Column('crease_right_mm', sa.Integer(), nullable=True),
        sa.Column('sheet_type_snapshot', sa.String(length=30), nullable=False),
        sa.Column('cutting_mode_snapshot', sa.String(length=30), nullable=False),
        sa.Column('yield_per_sheet', sa.Integer(), nullable=False),
        sa.Column('is_die_cut_snapshot', sa.Boolean(), nullable=False),
        sa.Column('mold_tool_id', sa.Integer(), nullable=True),
        sa.Column('mold_max_yield_per_sheet_snapshot', sa.Integer(), nullable=True),
        sa.Column('source_count', sa.Integer(), nullable=False),
        sa.Column('total_required_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('total_inventory_reserved_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('net_required_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('spare_sheet_quantity', sa.Integer(), nullable=False),
        sa.Column('order_purpose_sheet_quantity', sa.Integer(), nullable=False),
        sa.Column('reserve_sheet_quantity', sa.Integer(), nullable=False),
        sa.Column('purchase_sheet_quantity', sa.Integer(), nullable=False),
        sa.Column('cutting_remainder_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('physical_snapshot_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False, server_default='active'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('idempotency_key', sa.String(length=160), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['component_product_id'], ['products.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['material_id'], ['materials.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['mold_tool_id'], ['mold_tools.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['requisition_id'], ['material_requisitions.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['supplier_requisition_order_item_id'], ['supplier_requisition_order_items.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ondelete='SET NULL'),
        sa.CheckConstraint('(crease_left_mm IS NULL OR crease_left_mm >= 0) AND (crease_middle_mm IS NULL OR crease_middle_mm >= 0) AND (crease_right_mm IS NULL OR crease_right_mm >= 0)', name='ck_cppg_crease_dimensions'),
        sa.CheckConstraint('cutting_remainder_piece_quantity = (order_purpose_sheet_quantity - spare_sheet_quantity) * yield_per_sheet - net_required_piece_quantity AND cutting_remainder_piece_quantity >= 0 AND cutting_remainder_piece_quantity < yield_per_sheet', name='ck_cppg_cutting_remainder'),
        sa.CheckConstraint('((is_die_cut_snapshot IS TRUE AND mold_max_yield_per_sheet_snapshot IS NOT NULL AND mold_max_yield_per_sheet_snapshot > 0 AND yield_per_sheet <= mold_max_yield_per_sheet_snapshot) OR (is_die_cut_snapshot IS FALSE AND (mold_max_yield_per_sheet_snapshot IS NULL OR mold_max_yield_per_sheet_snapshot > 0)))', name='ck_cppg_die_cut_yield'),
        sa.CheckConstraint('length(trim(group_key)) > 0 AND length(trim(material_code_snapshot)) > 0 AND length(trim(flute_type_snapshot)) > 0 AND length(trim(cutting_mode_snapshot)) > 0 AND length(physical_snapshot_hash) = 64 AND length(trim(idempotency_key)) > 0 AND length(request_hash) = 64', name='ck_cppg_frozen_text'),
        sa.CheckConstraint('material_version_snapshot >= 1 AND layer_count_snapshot > 0 AND report_length_mm > 0 AND report_width_mm > 0 AND yield_per_sheet > 0', name='ck_cppg_physical_numbers'),
        sa.CheckConstraint('source_count > 0 AND total_required_piece_quantity > 0 AND total_inventory_reserved_piece_quantity >= 0 AND total_inventory_reserved_piece_quantity <= total_required_piece_quantity AND net_required_piece_quantity = total_required_piece_quantity - total_inventory_reserved_piece_quantity', name='ck_cppg_piece_balance'),
        sa.CheckConstraint('reserve_sheet_quantity >= 0 AND purchase_sheet_quantity = order_purpose_sheet_quantity + reserve_sheet_quantity', name='ck_cppg_purchase_balance'),
        sa.CheckConstraint("sheet_type_snapshot IN ('raw_board','net_sheet','creased_sheet')", name='ck_cppg_sheet_type'),
        sa.CheckConstraint('spare_sheet_quantity >= 0 AND order_purpose_sheet_quantity >= spare_sheet_quantity AND ((net_required_piece_quantity = 0 AND order_purpose_sheet_quantity = spare_sheet_quantity) OR (net_required_piece_quantity > 0 AND order_purpose_sheet_quantity > spare_sheet_quantity AND (order_purpose_sheet_quantity - spare_sheet_quantity) * yield_per_sheet >= net_required_piece_quantity AND (order_purpose_sheet_quantity - spare_sheet_quantity - 1) * yield_per_sheet < net_required_piece_quantity))', name='ck_cppg_single_rounding'),
        sa.CheckConstraint("status IN ('active','partially_received','received','reversed','voided')", name='ck_cppg_status'),
        sa.CheckConstraint('version >= 1', name='ck_cppg_version'),
        sa.UniqueConstraint('group_key', name='uq_cppg_group_key'),
        sa.UniqueConstraint('idempotency_key', name='uq_cppg_idempotency'),
        sa.UniqueConstraint('requisition_id', 'physical_snapshot_hash', name='uq_cppg_requisition_physical'),
        sa.UniqueConstraint('supplier_requisition_order_item_id', name='uq_cppg_supplier_item'),
    )
    op.create_index(
        'ix_cppg_customer_component_status',
        'composite_physical_purchase_groups',
        ['customer_id', 'component_product_id', 'status'],
        unique=False,
    )
    op.create_index(
        'ix_cppg_physical_hash',
        'composite_physical_purchase_groups',
        ['physical_snapshot_hash'],
        unique=False,
    )
    op.create_index(
        'ix_cppg_requisition_status',
        'composite_physical_purchase_groups',
        ['requisition_id', 'status'],
        unique=False,
    )

def _create_source_table() -> None:
    op.create_table(
        'composite_physical_purchase_group_sources',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column('composite_physical_purchase_group_id', sa.Integer(), nullable=False),
        sa.Column('source_key', sa.String(length=160), nullable=False),
        sa.Column('requisition_item_id', sa.Integer(), nullable=False),
        sa.Column('requisition_item_bom_source_id', sa.Integer(), nullable=False),
        sa.Column('purchase_purpose_source_snapshot_id', sa.Integer(), nullable=True),
        sa.Column('order_item_id', sa.Integer(), nullable=False),
        sa.Column('sales_order_item_bom_component_id', sa.Integer(), nullable=False),
        sa.Column('source_sequence', sa.Integer(), nullable=False),
        sa.Column('source_count_snapshot', sa.Integer(), nullable=False),
        sa.Column('is_last_source', sa.Boolean(), nullable=False),
        sa.Column('demand_basis', sa.String(length=30), nullable=False),
        sa.Column('component_type_snapshot', sa.String(length=20), nullable=False),
        sa.Column('parent_set_quantity', sa.Integer(), nullable=False),
        sa.Column('quantity_per_set', sa.Integer(), nullable=False),
        sa.Column('required_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('inventory_reserved_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('net_required_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('spare_sheet_quantity', sa.Integer(), nullable=False),
        sa.Column('allocated_order_purpose_sheet_quantity', sa.Integer(), nullable=False),
        sa.Column('cumulative_allocated_sheet_quantity_before', sa.Integer(), nullable=False),
        sa.Column('cumulative_allocated_sheet_quantity_after', sa.Integer(), nullable=False),
        sa.Column('group_order_purpose_sheet_quantity_snapshot', sa.Integer(), nullable=False),
        sa.Column('source_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(['composite_physical_purchase_group_id'], ['composite_physical_purchase_groups.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['order_item_id'], ['sales_order_items.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['purchase_purpose_source_snapshot_id'], ['purchase_purpose_source_snapshots.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['requisition_item_bom_source_id'], ['requisition_item_bom_sources.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['requisition_item_id'], ['material_requisition_items.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['sales_order_item_bom_component_id'], ['sales_order_item_bom_components.id'], ondelete='RESTRICT'),
        sa.CheckConstraint("component_type_snapshot IN ('whole','cover','base')", name='ck_cppgs_component_type'),
        sa.CheckConstraint("demand_basis IN ('order_sets','order_specific_pieces')", name='ck_cppgs_demand_basis'),
        sa.CheckConstraint('length(trim(source_key)) > 0 AND length(source_fingerprint) = 64', name='ck_cppgs_frozen_text'),
        sa.CheckConstraint('inventory_reserved_piece_quantity >= 0 AND inventory_reserved_piece_quantity <= required_piece_quantity AND net_required_piece_quantity = required_piece_quantity - inventory_reserved_piece_quantity', name='ck_cppgs_piece_balance'),
        sa.CheckConstraint("parent_set_quantity > 0 AND quantity_per_set > 0 AND required_piece_quantity > 0 AND ((demand_basis = 'order_sets' AND required_piece_quantity = parent_set_quantity * quantity_per_set) OR demand_basis = 'order_specific_pieces')", name='ck_cppgs_required_formula'),
        sa.CheckConstraint('source_count_snapshot > 0 AND source_sequence >= 1 AND source_sequence <= source_count_snapshot AND ((is_last_source IS TRUE AND source_sequence = source_count_snapshot) OR (is_last_source IS FALSE AND source_sequence < source_count_snapshot))', name='ck_cppgs_sequence'),
        sa.CheckConstraint('spare_sheet_quantity >= 0 AND allocated_order_purpose_sheet_quantity >= spare_sheet_quantity AND cumulative_allocated_sheet_quantity_before >= 0 AND cumulative_allocated_sheet_quantity_after = cumulative_allocated_sheet_quantity_before + allocated_order_purpose_sheet_quantity AND cumulative_allocated_sheet_quantity_after <= group_order_purpose_sheet_quantity_snapshot AND group_order_purpose_sheet_quantity_snapshot >= 0', name='ck_cppgs_sheet_balance'),
        sa.CheckConstraint('(source_sequence <> 1 OR cumulative_allocated_sheet_quantity_before = 0) AND (is_last_source IS FALSE OR cumulative_allocated_sheet_quantity_after = group_order_purpose_sheet_quantity_snapshot)', name='ck_cppgs_sheet_boundaries'),
        sa.UniqueConstraint('requisition_item_bom_source_id', name='uq_cppgs_bom_source'),
        sa.UniqueConstraint('composite_physical_purchase_group_id', 'source_sequence', name='uq_cppgs_group_sequence'),
        sa.UniqueConstraint('composite_physical_purchase_group_id', 'source_key', name='uq_cppgs_group_source_key'),
        sa.UniqueConstraint('purchase_purpose_source_snapshot_id', name='uq_cppgs_purpose_snapshot'),
    )
    op.create_index(
        'ix_cppgs_group',
        'composite_physical_purchase_group_sources',
        ['composite_physical_purchase_group_id'],
        unique=False,
    )
    op.create_index(
        'ix_cppgs_order_bom',
        'composite_physical_purchase_group_sources',
        ['order_item_id', 'sales_order_item_bom_component_id'],
        unique=False,
    )
    op.create_index(
        'ix_cppgs_requisition_item',
        'composite_physical_purchase_group_sources',
        ['requisition_item_id'],
        unique=False,
    )

def _create_receipt_table() -> None:
    op.create_table(
        'composite_physical_group_receipts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column('composite_physical_purchase_group_id', sa.Integer(), nullable=False),
        sa.Column('incoming_receipt_item_id', sa.Integer(), nullable=False),
        sa.Column('receipt_sequence', sa.Integer(), nullable=False),
        sa.Column('purchase_receipt_fact_id', sa.Integer(), nullable=False),
        sa.Column('component_inventory_lot_id', sa.Integer(), nullable=True),
        sa.Column('reserve_inventory_lot_id', sa.Integer(), nullable=True),
        sa.Column('group_version_snapshot', sa.Integer(), nullable=False),
        sa.Column('received_sheet_quantity', sa.Integer(), nullable=False),
        sa.Column('order_purpose_received_sheet_quantity', sa.Integer(), nullable=False),
        sa.Column('reserve_received_sheet_quantity', sa.Integer(), nullable=False),
        sa.Column('cumulative_received_sheet_quantity_before', sa.Integer(), nullable=False),
        sa.Column('cumulative_received_sheet_quantity_after', sa.Integer(), nullable=False),
        sa.Column('cumulative_order_purpose_sheet_quantity_before', sa.Integer(), nullable=False),
        sa.Column('cumulative_order_purpose_sheet_quantity_after', sa.Integer(), nullable=False),
        sa.Column('cumulative_reserve_sheet_quantity_before', sa.Integer(), nullable=False),
        sa.Column('cumulative_reserve_sheet_quantity_after', sa.Integer(), nullable=False),
        sa.Column('yield_per_sheet_snapshot', sa.Integer(), nullable=False),
        sa.Column('component_output_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('cumulative_component_output_piece_quantity_before', sa.Integer(), nullable=False),
        sa.Column('cumulative_component_output_piece_quantity_after', sa.Integer(), nullable=False),
        sa.Column('actual_unit_price_per_sheet', sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column('component_unit_material_cost', sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column('order_purpose_material_cost', sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column('reserve_material_cost', sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column('total_material_cost', sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column('currency_snapshot', sa.String(length=3), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='posted'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('idempotency_key', sa.String(length=160), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(['component_inventory_lot_id'], ['inventory_lots.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['composite_physical_purchase_group_id'], ['composite_physical_purchase_groups.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['incoming_receipt_item_id'], ['incoming_receipt_items.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['purchase_receipt_fact_id'], ['purchase_receipt_facts.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['reserve_inventory_lot_id'], ['inventory_lots.id'], ondelete='RESTRICT'),
        sa.CheckConstraint('((order_purpose_received_sheet_quantity > 0 AND component_output_piece_quantity > 0 AND component_inventory_lot_id IS NOT NULL AND component_unit_material_cost IS NOT NULL AND component_unit_material_cost > 0) OR (order_purpose_received_sheet_quantity = 0 AND component_output_piece_quantity = 0 AND component_inventory_lot_id IS NULL AND component_unit_material_cost IS NULL))', name='ck_cpgr_component_lot'),
        sa.CheckConstraint('yield_per_sheet_snapshot > 0 AND component_output_piece_quantity = order_purpose_received_sheet_quantity * yield_per_sheet_snapshot AND cumulative_component_output_piece_quantity_before >= 0 AND cumulative_component_output_piece_quantity_after = cumulative_component_output_piece_quantity_before + component_output_piece_quantity', name='ck_cpgr_component_output'),
        sa.CheckConstraint('actual_unit_price_per_sheet > 0 AND order_purpose_material_cost >= 0 AND reserve_material_cost >= 0 AND order_purpose_material_cost = actual_unit_price_per_sheet * order_purpose_received_sheet_quantity AND reserve_material_cost = actual_unit_price_per_sheet * reserve_received_sheet_quantity AND total_material_cost = order_purpose_material_cost + reserve_material_cost AND total_material_cost = actual_unit_price_per_sheet * received_sheet_quantity AND ((order_purpose_received_sheet_quantity = 0 AND order_purpose_material_cost = 0) OR (order_purpose_received_sheet_quantity > 0 AND order_purpose_material_cost > 0)) AND ((reserve_received_sheet_quantity = 0 AND reserve_material_cost = 0) OR (reserve_received_sheet_quantity > 0 AND reserve_material_cost > 0))', name='ck_cpgr_cost_balance'),
        sa.CheckConstraint('cumulative_received_sheet_quantity_before >= 0 AND cumulative_order_purpose_sheet_quantity_before >= 0 AND cumulative_reserve_sheet_quantity_before >= 0 AND cumulative_received_sheet_quantity_before = cumulative_order_purpose_sheet_quantity_before + cumulative_reserve_sheet_quantity_before AND cumulative_order_purpose_sheet_quantity_after = cumulative_order_purpose_sheet_quantity_before + order_purpose_received_sheet_quantity AND cumulative_reserve_sheet_quantity_after = cumulative_reserve_sheet_quantity_before + reserve_received_sheet_quantity AND cumulative_received_sheet_quantity_after = cumulative_received_sheet_quantity_before + received_sheet_quantity AND cumulative_received_sheet_quantity_after = cumulative_order_purpose_sheet_quantity_after + cumulative_reserve_sheet_quantity_after', name='ck_cpgr_cumulative_sheets'),
        sa.CheckConstraint('length(trim(currency_snapshot)) = 3 AND length(trim(idempotency_key)) > 0 AND length(request_hash) = 64', name='ck_cpgr_frozen_text'),
        sa.CheckConstraint('received_sheet_quantity > 0 AND order_purpose_received_sheet_quantity >= 0 AND reserve_received_sheet_quantity >= 0 AND received_sheet_quantity = order_purpose_received_sheet_quantity + reserve_received_sheet_quantity', name='ck_cpgr_receipt_balance'),
        sa.CheckConstraint('((reserve_received_sheet_quantity > 0 AND reserve_inventory_lot_id IS NOT NULL) OR (reserve_received_sheet_quantity = 0 AND reserve_inventory_lot_id IS NULL)) AND (component_inventory_lot_id IS NULL OR reserve_inventory_lot_id IS NULL OR component_inventory_lot_id <> reserve_inventory_lot_id)', name='ck_cpgr_reserve_lot'),
        sa.CheckConstraint("status IN ('posted','reversed')", name='ck_cpgr_status'),
        sa.CheckConstraint('receipt_sequence >= 1 AND group_version_snapshot >= 1 AND version >= 1', name='ck_cpgr_versions_sequence'),
        sa.UniqueConstraint('component_inventory_lot_id', name='uq_cpgr_component_lot'),
        sa.UniqueConstraint('composite_physical_purchase_group_id', 'receipt_sequence', name='uq_cpgr_group_sequence'),
        sa.UniqueConstraint('idempotency_key', name='uq_cpgr_idempotency'),
        sa.UniqueConstraint('incoming_receipt_item_id', name='uq_cpgr_incoming_receipt_item'),
        sa.UniqueConstraint('reserve_inventory_lot_id', name='uq_cpgr_reserve_lot'),
    )
    op.create_index(
        'ix_cpgr_created_at',
        'composite_physical_group_receipts',
        ['created_at'],
        unique=False,
    )
    op.create_index(
        'ix_cpgr_group_status',
        'composite_physical_group_receipts',
        ['composite_physical_purchase_group_id', 'status'],
        unique=False,
    )
    op.create_index(
        'ix_cpgr_purchase_receipt_fact',
        'composite_physical_group_receipts',
        ['purchase_receipt_fact_id'],
        unique=False,
    )

def _create_allocation_table() -> None:
    op.create_table(
        'composite_physical_group_receipt_source_allocations',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column('composite_physical_group_receipt_id', sa.Integer(), nullable=False),
        sa.Column('composite_physical_purchase_group_source_id', sa.Integer(), nullable=False),
        sa.Column('allocation_sequence', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('order_item_id', sa.Integer(), nullable=False),
        sa.Column('sales_order_item_bom_component_id', sa.Integer(), nullable=False),
        sa.Column('inventory_reservation_id', sa.Integer(), nullable=False),
        sa.Column('source_net_required_piece_quantity_snapshot', sa.Integer(), nullable=False),
        sa.Column('allocated_reserved_component_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('source_cumulative_reserved_piece_quantity_before', sa.Integer(), nullable=False),
        sa.Column('source_cumulative_reserved_piece_quantity_after', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('idempotency_key', sa.String(length=160), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('reversed_by', sa.Integer(), nullable=True),
        sa.Column('reversed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['composite_physical_group_receipt_id'], ['composite_physical_group_receipts.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['composite_physical_purchase_group_source_id'], ['composite_physical_purchase_group_sources.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['inventory_reservation_id'], ['inventory_reservations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['order_id'], ['sales_orders.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['order_item_id'], ['sales_order_items.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['reversed_by'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['sales_order_item_bom_component_id'], ['sales_order_item_bom_components.id'], ondelete='RESTRICT'),
        sa.CheckConstraint('source_cumulative_reserved_piece_quantity_before >= 0 AND source_cumulative_reserved_piece_quantity_after = source_cumulative_reserved_piece_quantity_before + allocated_reserved_component_piece_quantity AND source_cumulative_reserved_piece_quantity_after <= source_net_required_piece_quantity_snapshot', name='ck_cpgrsa_cumulative_balance'),
        sa.CheckConstraint('length(trim(idempotency_key)) > 0 AND length(request_hash) = 64', name='ck_cpgrsa_frozen_text'),
        sa.CheckConstraint('allocation_sequence >= 1 AND source_net_required_piece_quantity_snapshot > 0 AND allocated_reserved_component_piece_quantity > 0', name='ck_cpgrsa_positive_quantities'),
        sa.CheckConstraint("((status = 'active' AND reversed_by IS NULL AND reversed_at IS NULL) OR (status = 'reversed' AND reversed_by IS NOT NULL AND reversed_at IS NOT NULL))", name='ck_cpgrsa_reversal_audit'),
        sa.CheckConstraint("status IN ('active','reversed') AND version >= 1", name='ck_cpgrsa_status_version'),
        sa.UniqueConstraint('idempotency_key', name='uq_cpgrsa_idempotency'),
        sa.UniqueConstraint('inventory_reservation_id', name='uq_cpgrsa_inventory_reservation'),
        sa.UniqueConstraint('composite_physical_group_receipt_id', 'allocation_sequence', name='uq_cpgrsa_receipt_sequence'),
        sa.UniqueConstraint('composite_physical_group_receipt_id', 'composite_physical_purchase_group_source_id', name='uq_cpgrsa_receipt_source'),
    )
    op.create_index(
        'ix_cpgrsa_group_source',
        'composite_physical_group_receipt_source_allocations',
        ['composite_physical_purchase_group_source_id'],
        unique=False,
    )
    op.create_index(
        'ix_cpgrsa_order_bom',
        'composite_physical_group_receipt_source_allocations',
        ['order_item_id', 'sales_order_item_bom_component_id'],
        unique=False,
    )

def _create_reversal_table() -> None:
    op.create_table(
        'composite_physical_group_receipt_reversals',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column('composite_physical_group_receipt_id', sa.Integer(), nullable=False),
        sa.Column('receipt_version_before', sa.Integer(), nullable=False),
        sa.Column('receipt_version_after', sa.Integer(), nullable=False),
        sa.Column('reversed_sheet_quantity', sa.Integer(), nullable=False),
        sa.Column('reversed_component_output_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('reversed_reserved_component_piece_quantity', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('idempotency_key', sa.String(length=160), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('reversed_by', sa.Integer(), nullable=False),
        sa.Column('reversed_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(['composite_physical_group_receipt_id'], ['composite_physical_group_receipts.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['reversed_by'], ['users.id'], ondelete='RESTRICT'),
        sa.CheckConstraint('length(trim(reason)) > 0 AND length(trim(idempotency_key)) > 0 AND length(request_hash) = 64', name='ck_cpgrr_frozen_text'),
        sa.CheckConstraint('reversed_sheet_quantity > 0 AND reversed_component_output_piece_quantity >= 0 AND reversed_reserved_component_piece_quantity >= 0 AND reversed_reserved_component_piece_quantity <= reversed_component_output_piece_quantity', name='ck_cpgrr_quantities'),
        sa.CheckConstraint('receipt_version_before >= 1 AND receipt_version_after = receipt_version_before + 1', name='ck_cpgrr_version_transition'),
        sa.UniqueConstraint('idempotency_key', name='uq_cpgrr_idempotency'),
        sa.UniqueConstraint('composite_physical_group_receipt_id', name='uq_cpgrr_receipt'),
    )
    op.create_index(
        'ix_cpgrr_reversed_at',
        'composite_physical_group_receipt_reversals',
        ['reversed_at'],
        unique=False,
    )


def _upgrade_incoming_receipt_source() -> None:
    trigger_sql = _drop_sqlite_triggers_referencing(INCOMING_TABLE)
    with op.batch_alter_table(INCOMING_TABLE, recreate="auto") as batch_op:
        batch_op.drop_constraint(
            "ck_incoming_receipt_items_exactly_one_source", type_="check"
        )
        batch_op.add_column(
            sa.Column(
                "composite_physical_purchase_group_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_incoming_receipt_items_composite_physical_group",
            GROUP_TABLE,
            ["composite_physical_purchase_group_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_incoming_receipt_items_exactly_one_source",
            "(order_id IS NOT NULL AND order_item_id IS NOT NULL "
            "AND stock_replenishment_item_id IS NULL "
            "AND composite_physical_purchase_group_id IS NULL) "
            "OR (order_id IS NULL AND order_item_id IS NULL "
            "AND requisition_id IS NULL AND requisition_item_id IS NULL "
            "AND supplier_order_id IS NULL AND supplier_order_item_id IS NULL "
            "AND stock_replenishment_item_id IS NOT NULL "
            "AND composite_physical_purchase_group_id IS NULL) "
            "OR (order_id IS NULL AND order_item_id IS NULL "
            "AND requisition_id IS NULL AND requisition_item_id IS NULL "
            "AND supplier_order_id IS NULL AND supplier_order_item_id IS NULL "
            "AND stock_replenishment_item_id IS NULL "
            "AND composite_physical_purchase_group_id IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_incoming_receipt_items_composite_physical_group",
            ["composite_physical_purchase_group_id", "status"],
        )
    _restore_sqlite_triggers(trigger_sql)


def _upgrade_delivery_cost_source() -> None:
    trigger_sql = _drop_sqlite_triggers_referencing(COST_TABLE)
    with op.batch_alter_table(COST_TABLE, recreate="auto") as batch_op:
        batch_op.alter_column(
            "incoming_receipt_purpose_allocation_id",
            existing_type=sa.Integer(),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "composite_physical_group_receipt_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_finance_delivery_material_cost_facts_composite_receipt",
            RECEIPT_TABLE,
            ["composite_physical_group_receipt_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_finance_delivery_material_cost_facts_receipt_source",
            "((incoming_receipt_purpose_allocation_id IS NOT NULL "
            "AND composite_physical_group_receipt_id IS NULL) OR "
            "(incoming_receipt_purpose_allocation_id IS NULL "
            "AND composite_physical_group_receipt_id IS NOT NULL))",
        )
        batch_op.create_index(
            "ix_finance_delivery_material_cost_facts_composite_receipt",
            ["composite_physical_group_receipt_id"],
        )
    _restore_sqlite_triggers(trigger_sql)


def upgrade() -> None:
    _create_group_table()
    _create_source_table()
    _upgrade_incoming_receipt_source()
    _create_receipt_table()
    _create_allocation_table()
    _create_reversal_table()
    _upgrade_delivery_cost_source()


def _assert_no_group_facts() -> None:
    connection = op.get_bind()
    counts = {
        table_name: int(
            connection.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}"))
            .scalar_one()
        )
        for table_name in (
            GROUP_TABLE,
            SOURCE_TABLE,
            RECEIPT_TABLE,
            ALLOCATION_TABLE,
            REVERSAL_TABLE,
        )
    }
    counts["incoming_group_links"] = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {INCOMING_TABLE} "
                "WHERE composite_physical_purchase_group_id IS NOT NULL"
            )
        ).scalar_one()
    )
    counts["delivery_cost_group_links"] = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {COST_TABLE} "
                "WHERE composite_physical_group_receipt_id IS NOT NULL"
            )
        ).scalar_one()
    )
    if any(counts.values()):
        raise RuntimeError(
            "P1-150B downgrade blocked: composite physical group facts exist; "
            "restore the verified pre-upgrade backup instead"
        )


def _downgrade_delivery_cost_source() -> None:
    trigger_sql = _drop_sqlite_triggers_referencing(COST_TABLE)
    with op.batch_alter_table(COST_TABLE, recreate="auto") as batch_op:
        batch_op.drop_index(
            "ix_finance_delivery_material_cost_facts_composite_receipt"
        )
        batch_op.drop_constraint(
            "ck_finance_delivery_material_cost_facts_receipt_source",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_finance_delivery_material_cost_facts_composite_receipt",
            type_="foreignkey",
        )
        batch_op.drop_column("composite_physical_group_receipt_id")
        batch_op.alter_column(
            "incoming_receipt_purpose_allocation_id",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
        )
    _restore_sqlite_triggers(trigger_sql)


def _downgrade_incoming_receipt_source() -> None:
    trigger_sql = _drop_sqlite_triggers_referencing(INCOMING_TABLE)
    with op.batch_alter_table(INCOMING_TABLE, recreate="auto") as batch_op:
        batch_op.drop_index(
            "ix_incoming_receipt_items_composite_physical_group"
        )
        batch_op.drop_constraint(
            "ck_incoming_receipt_items_exactly_one_source", type_="check"
        )
        batch_op.drop_constraint(
            "fk_incoming_receipt_items_composite_physical_group",
            type_="foreignkey",
        )
        batch_op.drop_column("composite_physical_purchase_group_id")
        batch_op.create_check_constraint(
            "ck_incoming_receipt_items_exactly_one_source",
            "(order_id IS NOT NULL AND order_item_id IS NOT NULL "
            "AND stock_replenishment_item_id IS NULL) "
            "OR (order_id IS NULL AND order_item_id IS NULL "
            "AND requisition_id IS NULL AND requisition_item_id IS NULL "
            "AND supplier_order_id IS NULL AND supplier_order_item_id IS NULL "
            "AND stock_replenishment_item_id IS NOT NULL)",
        )
    _restore_sqlite_triggers(trigger_sql)


def downgrade() -> None:
    _assert_no_group_facts()
    _downgrade_delivery_cost_source()
    op.drop_table(REVERSAL_TABLE)
    op.drop_table(ALLOCATION_TABLE)
    op.drop_table(RECEIPT_TABLE)
    _downgrade_incoming_receipt_source()
    op.drop_table(SOURCE_TABLE)
    op.drop_table(GROUP_TABLE)
