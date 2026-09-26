"""Validate new stock at the real transaction boundary, after source costs freeze.

Transfers/returns preserve prior lot facts. This never scans or reprices old lots.
"""
import json
import re
from decimal import Decimal, InvalidOperation
from sqlalchemy import event
from app.core.receipt_price_guard import ReceiptPriceGuardSession
from sqlalchemy.sql.elements import TextClause


@event.listens_for(ReceiptPriceGuardSession, 'do_orm_execute')
def reject_untracked_insert(state):
    table = getattr(state.statement, 'table', None)
    raw = state.statement.text if isinstance(state.statement, TextClause) else ''
    if ((state.is_insert and getattr(table, 'name', None) == 'inventory_lots') or
            (re.search(r'\b(?:insert|replace)\b', raw, re.I) and 'inventory_lots' in raw.lower())):
        from fastapi import HTTPException
        raise HTTPException(422, '新入库必须经过实物资料和成本检查，不能直接批量插入库存')


@event.listens_for(ReceiptPriceGuardSession, 'after_flush')
def track_entries(session, _context):
    from app.models.warehouse_inventory import InventoryLot
    ids = session.info.setdefault('new_inventory_entry_ids', set())
    for row in session.new:
        if isinstance(row, InventoryLot) and (row.source_type not in {'transfer', 'delivery_return'} or row.source_ref_type in {'bom_assembly', 'subkit_conversion', 'stock_preparation_assembly'}):
            ids.add(row.id)


@event.listens_for(ReceiptPriceGuardSession, 'before_commit')
def validate_entries(session):
    if session.in_nested_transaction():
        return
    from app.models.warehouse_inventory import InventoryLot
    from fastapi import HTTPException
    session.flush()
    for lid in session.info.get('new_inventory_entry_ids', ()):
        lot = session.get(InventoryLot, lid, populate_existing=True)
        if lot is None:
            continue
        missing = []
        try:
            cost = Decimal(str(lot.estimated_unit_cost_snapshot or 0))
            if not cost.is_finite() or cost <= 0:
                missing.append('有效材料成本')
            evidence = json.loads(lot.cost_snapshot_detail_json or '{}')
            if not isinstance(evidence, dict) or not evidence or not lot.cost_snapshot_source or not lot.cost_snapshot_at:
                missing.append('冻结成本来源')
        except (InvalidOperation, ValueError, TypeError):
            missing.append('冻结成本来源')
        from app.services.inventory_valuation import frozen_cost
        unit, cost_detail = frozen_cost(lot, session)
        if unit is None:
            missing.append(cost_detail.get('validation_issue') or '可核验的冻结成本')
        detail = lot.finished_detail
        if detail:
            try:
                basis = json.loads(detail.physical_basis_json or '{}')
            except (ValueError, TypeError):
                basis = {}
            if not isinstance(basis, dict):
                basis = {}
            if not basis.get('unit'):
                missing.append('实物单位')
            components = cost_detail.get('components') or []
            has_sheet = any(isinstance(part, dict) and part.get('length_mm') and part.get('width_mm') for part in components)
            rule = cost_detail.get('cost_rule') or {}
            has_sheet = has_sheet or (rule.get('mode') == 'material' and bool(rule.get('length_mm') and rule.get('width_mm')))
            external_spec = cost_detail.get('frozen_specification')
            if isinstance(external_spec, str):
                try:
                    external_spec = json.loads(external_spec)
                except (ValueError, TypeError):
                    external_spec = None
            external_source = lot.cost_snapshot_source == 'direct_external_receipt' or (
                lot.cost_snapshot_source == 'purchase_receipt_actual'
                and lot.source_ref_type in {'external_packaging_receipt_item', 'direct_external_receipt'}
                and cost_detail.get('external_receipt_item_id') == lot.source_ref_id)
            has_external_spec = external_source and isinstance(external_spec, dict) and bool(external_spec)
            if not basis.get('spec') and not (detail.length_mm and detail.width_mm) and not has_sheet and not has_external_spec:
                missing.append('实物规格')
        elif lot.semi_finished_detail:
            sheet = lot.semi_finished_detail
            if not sheet.board_length_mm or not sheet.board_width_mm or not sheet.material_code_snapshot:
                missing.append('片料长宽和材质')
        elif lot.inventory_type != 'assembly_body':
            missing.append('实物明细')
        if missing:
            raise HTTPException(422, detail=dict(code='INVENTORY_ENTRY_INCOMPLETE', lot_number=lot.lot_number,
                missing=missing, message='本次入库尚未保存：请补齐'+'、'.join(missing)+'后继续',
                product_id=detail.product_id if detail else None,
                repair_url='/?page=products&subpage=materials'))


@event.listens_for(ReceiptPriceGuardSession, 'after_transaction_end')
def clear_entries(session, transaction):
    if transaction.parent is None:
        session.info.pop('new_inventory_entry_ids', None)
